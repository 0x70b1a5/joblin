"""Data model helpers and scheduling math.

Tasks are stored as plain dicts (so they round-trip through JSON with zero
friction). This module centralises the only tricky part: turning a recurrence
rule (a wall-clock time + an interval in days, or a one-off deadline) into a
concrete "next fire" instant in UTC, in a DST-aware way.

Task dict schema
----------------
{
    "id":           str,            # short unique id
    "guild_id":     int,            # discord server id
    "brief":        str,            # short text posted in the channel
    "description":  str | None,     # long text revealed by the info reaction
    "recurring":    bool,           # True => repeats, False => one-off
    "freq":         str,            # "once"|"days"|"weekly"|"monthly" (see below)
    "interval_days":int,            # >=1 for freq "days" (1 == daily), else 0
    "weekdays":     list[int],      # for freq "weekly": 0=Mon … 6=Sun
    "monthdays":    list[int],      # for freq "monthly": 1..31 (clamped per month)
    "time_of_day":  str | None,     # "HH:MM" local wall time if recurring
    "next_due":     str | None,     # ISO-8601 UTC; the next scheduled fire.
                                    #   None while an occurrence is pending.
    "created_by":   int,
    "created_at":   str,            # ISO-8601 UTC
    "pending":      dict | None,    # the in-flight occurrence, or None
}

Recurrence (``freq``)
---------------------
* ``"once"``    — a one-off; ``next_due`` is an absolute instant, nothing else.
* ``"days"``    — every ``interval_days`` days at ``time_of_day`` (1 == daily).
* ``"weekly"``  — on each weekday in ``weekdays`` at ``time_of_day``.
* ``"monthly"`` — on each day-of-month in ``monthdays`` at ``time_of_day``
                  (a day past the month's length clamps to the last day, so 31
                  means "last day").

Legacy tasks (written before ``freq`` existed) carry only ``recurring`` +
``interval_days`` + ``time_of_day``; :func:`recurrence_of` reads them as the
equivalent ``"days"`` / ``"once"`` rule, so nothing needs migrating on disk.

pending dict schema (an occurrence that has fired and awaits action)
--------------------------------------------------------------------
{
    "due_at":       str,            # ISO-8601 UTC, the scheduled fire time
    "remind_at":    str,            # ISO-8601 UTC, when to next nag
    "ffwd_count":   int,            # number of fast-forwards so far
    "channel_id":   int,
    "message_ids":  list[int],      # every message posted for this occurrence
}
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
import uuid
from typing import Optional
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc

# --- Reaction emojis ---------------------------------------------------------
# Compared after stripping the U+FE0F "variation selector" so that, e.g.,
# "ℹ️" and "ℹ" are treated identically regardless of how Discord echoes them.
EMOJI_DONE = "✅"
EMOJI_FFWD = "⏩"
EMOJI_INFO = "ℹ️"
EMOJI_DELETE = "❌"
EMOJI_UNDO = "↩️"  # appears after a ✅/⏩/❌ action so it can be reverted

# Snooze "numpad": tapping ⏩ opens a *separate* panel message that self-reacts
# with these, so the task post itself stays uncluttered. Pick a number; toggle
# the unit with ⏱️ (hours) / 📅 (days); ❌ (EMOJI_DELETE) cancels the panel.
EMOJI_SNOOZE_HOURS = "⏱️"
EMOJI_SNOOZE_DAYS = "📅"
SNOOZE_CHOICES = (1, 2, 3, 4, 6, 8)
DIGIT_EMOJI = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 6: "6️⃣", 8: "8️⃣"}


def emoji_key(emoji: object) -> str:
    """Normalise an emoji (str or PartialEmoji) for comparison."""
    return str(emoji).replace(chr(0xFE0F), "")  # strip the variation selector


# Reverse lookup from a normalised keycap emoji back to its integer.
DIGIT_BY_KEY = {emoji_key(e): n for n, e in DIGIT_EMOJI.items()}


# --- Time helpers ------------------------------------------------------------
def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def to_iso(d: dt.datetime) -> str:
    return d.astimezone(UTC).isoformat()


def from_iso(s: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def discord_ts(d: dt.datetime, style: str = "f") -> str:
    """Render a Discord timestamp tag, which each viewer sees in their own tz.

    Styles: t (short time), f (short date+time), F (long), R (relative).
    """
    return f"<t:{int(d.timestamp())}:{style}>"


_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hhmm(s: str) -> tuple[int, int]:
    m = _HHMM.match(s.strip())
    if not m:
        raise ValueError("expected a time as HH:MM (e.g. 08:00)")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError("time out of range (hours 0-23, minutes 0-59)")
    return h, mi


def normalise_hhmm(s: str) -> str:
    h, mi = parse_hhmm(s)
    return f"{h:02d}:{mi:02d}"


_DATETIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})$")


def parse_oneoff(s: str, tz: ZoneInfo) -> dt.datetime:
    """Parse 'YYYY-MM-DD HH:MM' (local wall time) into a UTC instant."""
    s = s.strip().replace("T", " ")
    m = _DATETIME.match(s)
    if not m:
        raise ValueError("expected a one-off time as 'YYYY-MM-DD HH:MM'")
    y, mo, d, h, mi = map(int, m.groups())
    try:
        local = dt.datetime(y, mo, d, h, mi, tzinfo=tz)
    except ValueError as e:
        raise ValueError(f"invalid date/time: {e}") from e
    return local.astimezone(UTC)


def new_id() -> str:
    return uuid.uuid4().hex[:8]


# --- Recurrence math ---------------------------------------------------------
def compute_first_due(now: dt.datetime, tz: ZoneInfo, time_of_day: str) -> dt.datetime:
    """First fire for a brand-new recurring task: today at HH:MM if that is
    still in the future, otherwise tomorrow at HH:MM."""
    h, mi = parse_hhmm(time_of_day)
    local_now = now.astimezone(tz)
    cand = local_now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if cand <= local_now:
        cand = cand + dt.timedelta(days=1)
    return cand.astimezone(UTC)


def roll_forward(
    prev_due: dt.datetime,
    tz: ZoneInfo,
    time_of_day: str,
    interval_days: int,
    now: dt.datetime,
) -> dt.datetime:
    """Next fire after `prev_due`: advance by `interval_days` (re-pinning the
    wall-clock time each step so DST shifts don't drift it), skipping any
    occurrences already in the past so we never fire a backlog all at once.
    """
    h, mi = parse_hhmm(time_of_day)
    interval = max(1, interval_days)
    local_now = now.astimezone(tz)
    local = prev_due.astimezone(tz).replace(hour=h, minute=mi, second=0, microsecond=0)
    # Always move to at least the next slot, then keep going until it's ahead.
    local = (local + dt.timedelta(days=interval)).replace(
        hour=h, minute=mi, second=0, microsecond=0
    )
    while local <= local_now:
        local = (local + dt.timedelta(days=interval)).replace(
            hour=h, minute=mi, second=0, microsecond=0
        )
    return local.astimezone(UTC)


# ---------------------------------------------------------------------------
# Ergonomic "when" parsing  (the `at` field)
# ---------------------------------------------------------------------------
# A small, dependency-free natural-language resolver so users can type "now",
# "in 2h", "tonight", "tomorrow 8am", "fri 18:00", "Jun 20 14:00" or a full
# "2026-06-20 14:00" instead of memorising one rigid format. It always returns
# a concrete UTC instant; autocomplete echoes that instant back so there's no
# guessing. The wall-clock interpretation is done in the guild's timezone.

_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2, "thursday": 3, "thu": 3, "thur": 3,
    "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_WEEKDAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = {
    **{m.lower(): i for i, m in enumerate(calendar.month_name) if m},
    **{m.lower(): i for i, m in enumerate(calendar.month_abbr) if m},
}
_REL = re.compile(
    r"^(?:in\s+|\+)?(\d+)\s*"
    r"(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks)$"
)
_CLOCK = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?$")
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ t]+(.+))?$")
_MONTH_DAY = re.compile(r"^([a-z]{3,9})\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?(?:\s+(?:at\s+)?(.+))?$")
_DAY_MONTH = re.compile(r"^(\d{1,2})\s+([a-z]{3,9})\.?(?:,?\s+(\d{4}))?(?:\s+(?:at\s+)?(.+))?$")


def parse_clock(s: str) -> tuple[int, int]:
    """Parse a wall-clock time, accepting 24h or am/pm: '8', '8:30', '8am',
    '8:30pm', '20:15'. Returns (hour, minute)."""
    m = _CLOCK.match(s.strip().lower().replace(" ", ""))
    if not m:
        raise ValueError(f"couldn't read a time from {s!r} (try 18:00 or 6pm)")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap in ("pm", "p") and h != 12:
        h += 12
    elif ap in ("am", "a") and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError("time out of range (hours 0-23, minutes 0-59)")
    return h, mi


def _local(y: int, mo: int, d: int, h: int, mi: int, tz: ZoneInfo) -> dt.datetime:
    return dt.datetime(y, mo, d, h, mi, tzinfo=tz).astimezone(UTC)


def _next_clock(now: dt.datetime, tz: ZoneInfo, h: int, mi: int) -> dt.datetime:
    """The next time it is locally ``h:mi`` — today if still ahead, else tomorrow."""
    local_now = now.astimezone(tz)
    cand = local_now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if cand <= local_now:
        cand += dt.timedelta(days=1)
    return cand.astimezone(UTC)


def resolve_when(text: Optional[str], tz: ZoneInfo, now: dt.datetime) -> dt.datetime:
    """Resolve a free-form ``at`` string into a concrete UTC instant.

    Empty / "now" → ``now``. Also understands relative offsets ("in 2h", "+3d"),
    "tonight"/"noon"/"midnight", "today"/"tomorrow [time]", weekday names
    ("fri", "next monday [time]"), month-day dates ("Jun 20 [time]",
    "20 Jun"), ISO dates ("2026-06-20 [time]") and bare times ("18:00", "6pm").
    """
    s = (text or "").strip().lower()
    if s in ("", "now"):
        return now

    rel = _REL.match(s)
    if rel:
        n, unit = int(rel.group(1)), rel.group(2)
        if unit[0] == "m":
            return now + dt.timedelta(minutes=n)
        if unit[0] == "h":
            return now + dt.timedelta(hours=n)
        if unit[0] == "w":
            return now + dt.timedelta(weeks=n)
        return now + dt.timedelta(days=n)  # d / day / days

    if s in ("tonight", "this evening"):
        return _next_clock(now, tz, 20, 0)
    if s == "noon":
        return _next_clock(now, tz, 12, 0)
    if s == "midnight":
        return _next_clock(now, tz, 0, 0)

    iso = _ISO.match(s)
    if iso:
        y, mo, d = int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
        h, mi = parse_clock(iso.group(4)) if iso.group(4) else (9, 0)
        try:
            return _local(y, mo, d, h, mi, tz)
        except ValueError as e:
            raise ValueError(f"invalid date/time: {e}") from e

    parts = s.split()
    if parts[0] in ("today", "tomorrow"):
        base = now.astimezone(tz).date() + dt.timedelta(days=1 if parts[0] == "tomorrow" else 0)
        h, mi = parse_clock(" ".join(parts[1:]).removeprefix("at ").strip()) if len(parts) > 1 else (9, 0)
        return _local(base.year, base.month, base.day, h, mi, tz)

    idx, forced_next = (1, True) if parts[0] in ("next", "this") else (0, False)
    if idx < len(parts) and parts[idx] in _WEEKDAYS:
        wd = _WEEKDAYS[parts[idx]]
        rest = " ".join(parts[idx + 1:]).removeprefix("at ").strip()
        h, mi = parse_clock(rest) if rest else (9, 0)
        local_now = now.astimezone(tz)
        ahead = (wd - local_now.weekday()) % 7
        if forced_next and ahead == 0:
            ahead = 7
        date = local_now.date() + dt.timedelta(days=ahead)
        cand = _local(date.year, date.month, date.day, h, mi, tz)
        if cand <= now:  # today, but the time already passed → next week
            cand = cand + dt.timedelta(days=7)
        return cand

    for rx in (_MONTH_DAY, _DAY_MONTH):
        m = rx.match(s)
        if not m:
            continue
        if rx is _MONTH_DAY:
            mon_name, day = m.group(1), int(m.group(2))
        else:
            day, mon_name = int(m.group(1)), m.group(2)
        if mon_name not in _MONTHS:
            continue
        mo = _MONTHS[mon_name]
        year = int(m.group(3)) if m.group(3) else now.astimezone(tz).year
        h, mi = parse_clock(m.group(4)) if m.group(4) else (9, 0)
        try:
            cand = _local(year, mo, day, h, mi, tz)
        except ValueError as e:
            raise ValueError(f"invalid date/time: {e}") from e
        if not m.group(3) and cand <= now:  # no explicit year and it's past → next year
            cand = _local(year + 1, mo, day, h, mi, tz)
        return cand

    h, mi = parse_clock(s)  # bare time → the next time it's locally h:mi
    return _next_clock(now, tz, h, mi)


def time_of_day_from(text: Optional[str], tz: ZoneInfo, now: dt.datetime) -> str:
    """The wall-clock 'HH:MM' a recurring task should fire at, taken from `at`
    (or the current minute when `at` is omitted)."""
    local = resolve_when(text, tz, now).astimezone(tz)
    return f"{local.hour:02d}:{local.minute:02d}"


# ---------------------------------------------------------------------------
# Recurrence rules  (the `repeat` field)
# ---------------------------------------------------------------------------
def parse_repeat(text: Optional[str]) -> dict:
    """Parse a free-form ``repeat`` string into a rule dict.

    Returns ``{"freq", "interval_days", "weekdays", "monthdays"}``.

    Examples
    --------
    "" / "once"                  → one-off
    "daily" / "every day"        → every 1 day
    "every 2 days" / "2d"        → every 2 days       (bare "2" works too)
    "weekly"                     → every 7 days
    "weekdays" / "weekends"      → Mon-Fri / Sat-Sun
    "mon,thu" / "every tuesday"  → those weekdays
    "monthly" / "monthly on the 1st" / "1st,15th" → those days of the month
    """
    rule = {"freq": "once", "interval_days": 0, "weekdays": [], "monthdays": []}
    s = (text or "").strip().lower()
    if s in ("", "once", "one-off", "oneoff", "no", "none", "never"):
        return rule

    if s in ("daily", "every day", "everyday", "day"):
        return {**rule, "freq": "days", "interval_days": 1}
    if s in ("weekly", "every week", "week"):
        return {**rule, "freq": "days", "interval_days": 7}
    if s in ("fortnightly", "biweekly", "every other week"):
        return {**rule, "freq": "days", "interval_days": 14}
    if s in ("every other day", "alternate days"):
        return {**rule, "freq": "days", "interval_days": 2}

    m = re.match(r"^(?:every|each)?\s*(\d+)\s*(?:d|days?)?$", s)
    if m:  # "every 3 days", "3 days", "3d", or a bare "3"
        n = int(m.group(1))
        if n < 1:
            raise ValueError("interval must be at least 1 day")
        return {**rule, "freq": "days", "interval_days": n}

    if "month" in s or re.search(r"\b\d{1,2}(?:st|nd|rd|th)\b", s) or "last day" in s:
        # `monthdays` may come back empty for a bare "monthly"; the caller fills
        # it with the day-of-month the task is being created on.
        return {**rule, "freq": "monthly", "monthdays": _parse_monthdays(s)}

    weekdays = _parse_weekdays(s)
    if weekdays:
        return {**rule, "freq": "weekly", "weekdays": weekdays}

    raise ValueError(
        "couldn't read that repeat. Try: once · daily · every 2 days · "
        "weekdays · mon,thu · monthly on the 1st"
    )


def _parse_weekdays(s: str) -> list[int]:
    if s in ("weekdays", "weekday"):
        return [0, 1, 2, 3, 4]
    if s in ("weekends", "weekend"):
        return [5, 6]
    found: set[int] = set()
    for tok in re.split(r"[\s,/&]+|\band\b", s):
        tok = tok.strip().removeprefix("every ").strip()
        if tok in ("", "every", "each", "on", "the"):
            continue
        if tok in _WEEKDAYS:
            found.add(_WEEKDAYS[tok])
        else:
            return []  # an unrecognised token → not a weekday list at all
    return sorted(found)


def _parse_monthdays(s: str) -> list[int]:
    found: set[int] = set()
    if "last day" in s or "last of" in s:
        found.add(31)  # clamps to the real last day each month
    for num in re.findall(r"\d{1,2}", s):
        d = int(num)
        if 1 <= d <= 31:
            found.add(d)
    # "monthly" with no explicit day → the day the user is creating it is added
    # by the caller; here we only surface the days actually named.
    return sorted(found)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} & {items[1]}"
    return ", ".join(items[:-1]) + f" & {items[-1]}"


def describe_repeat(rule: dict) -> str:
    """A short human label for a recurrence rule (no time component)."""
    freq = rule["freq"]
    if freq == "once":
        return "one-off"
    if freq == "days":
        n = rule.get("interval_days", 1)
        if n == 1:
            return "every day"
        if n == 7:
            return "weekly"
        if n == 14:
            return "every 2 weeks"
        return f"every {n} days"
    if freq == "weekly":
        wds = sorted(rule.get("weekdays", []))
        if wds == [0, 1, 2, 3, 4]:
            return "weekdays"
        if wds == [5, 6]:
            return "weekends"
        if len(wds) == 7:
            return "every day"
        return _join([_WEEKDAY_ABBR[w] for w in wds])
    if freq == "monthly":
        mds = sorted(rule.get("monthdays", []))
        labels = ["last day" if d == 31 else _ordinal(d) for d in mds]
        return "monthly on the " + _join(labels)
    return freq


def recurrence_of(task: dict) -> dict:
    """Normalise a task (new- or legacy-schema) into a rule dict, also carrying
    its ``time_of_day``. Tolerates old tasks that predate the ``freq`` field."""
    if task.get("freq"):
        return {
            "freq": task["freq"],
            "interval_days": task.get("interval_days", 0),
            "weekdays": task.get("weekdays", []),
            "monthdays": task.get("monthdays", []),
            "time_of_day": task.get("time_of_day"),
        }
    if task.get("recurring"):  # legacy: only "days" recurrence existed
        return {
            "freq": "days",
            "interval_days": task.get("interval_days") or 1,
            "weekdays": [],
            "monthdays": [],
            "time_of_day": task.get("time_of_day"),
        }
    return {"freq": "once", "interval_days": 0, "weekdays": [], "monthdays": [],
            "time_of_day": None}


# --- Unified next-fire dispatch (so adding monthly never touched the bot) ----
def _next_weekly(weekdays: list[int], tz: ZoneInfo, h: int, mi: int,
                 after: dt.datetime) -> dt.datetime:
    """Earliest fire strictly after ``after`` whose weekday ∈ ``weekdays``."""
    base = after.astimezone(tz).date()
    for i in range(0, 15):  # a matching weekday always lands within 7 days
        d = base + dt.timedelta(days=i)
        if d.weekday() in weekdays:
            cand = _local(d.year, d.month, d.day, h, mi, tz)
            if cand > after:
                return cand
    raise ValueError("weekly rule has no weekdays")


def _next_monthly(monthdays: list[int], tz: ZoneInfo, h: int, mi: int,
                  after: dt.datetime) -> dt.datetime:
    """Earliest fire strictly after ``after`` on a day ∈ ``monthdays`` (a day
    beyond the month's length clamps to the last day, so 31 == last day)."""
    local = after.astimezone(tz)
    y, mo = local.year, local.month
    for _ in range(0, 60):  # up to five years of safety margin
        last = calendar.monthrange(y, mo)[1]
        for d in sorted({min(md, last) for md in monthdays}):
            cand = _local(y, mo, d, h, mi, tz)
            if cand > after:
                return cand
        mo += 1
        if mo > 12:
            mo, y = 1, y + 1
    raise ValueError("monthly rule produced no date")


def first_due(rule: dict, tz: ZoneInfo, now: dt.datetime) -> dt.datetime:
    """First fire for a brand-new recurring task (today's slot if still ahead)."""
    tod = rule["time_of_day"]
    h, mi = parse_hhmm(tod)
    if rule["freq"] == "days":
        return compute_first_due(now, tz, tod)
    if rule["freq"] == "weekly":
        return _next_weekly(rule["weekdays"], tz, h, mi, now)
    if rule["freq"] == "monthly":
        return _next_monthly(rule["monthdays"], tz, h, mi, now)
    raise ValueError(f"{rule['freq']} is not a recurring rule")


def next_due(rule: dict, tz: ZoneInfo, prev_due: dt.datetime,
             now: dt.datetime) -> dt.datetime:
    """The fire after ``prev_due``, never replaying a backlog (skips past now)."""
    tod = rule["time_of_day"]
    h, mi = parse_hhmm(tod)
    if rule["freq"] == "days":
        return roll_forward(prev_due, tz, tod, rule["interval_days"], now)
    anchor = max(prev_due, now)
    if rule["freq"] == "weekly":
        return _next_weekly(rule["weekdays"], tz, h, mi, anchor)
    if rule["freq"] == "monthly":
        return _next_monthly(rule["monthdays"], tz, h, mi, anchor)
    raise ValueError(f"{rule['freq']} is not a recurring rule")
