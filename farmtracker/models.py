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
    "recurring":    bool,           # True => interval rule, False => one-off
    "interval_days":int,            # >=1 if recurring (1 == daily), else 0
    "time_of_day":  str | None,     # "HH:MM" local wall time if recurring
    "next_due":     str | None,     # ISO-8601 UTC; the next scheduled fire.
                                    #   None while an occurrence is pending.
    "created_by":   int,
    "created_at":   str,            # ISO-8601 UTC
    "pending":      dict | None,    # the in-flight occurrence, or None
}

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

import datetime as dt
import re
import uuid
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


def emoji_key(emoji: object) -> str:
    """Normalise an emoji (str or PartialEmoji) for comparison."""
    return str(emoji).replace(chr(0xFE0F), "")  # strip the variation selector


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
