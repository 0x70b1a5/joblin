from __future__ import annotations

import datetime as dt
import itertools
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .. import trinkets
from ..models import (
    UTC,
    from_iso,
    now_utc,
    to_iso,
)
from .core import (
    NIGHTLY_HOUR,
    NIGHTLY_MINUTE,
    NO_PINGS,
    bot,
    store,
)
from .helpers import guild_config



# ---------------------------------------------------------------------------
# Leaderboard scoring — puntos (bounties count double) and monthly ⭐ stars
# ---------------------------------------------------------------------------
def _completion_points(rec: dict) -> int:
    """Puntos a logged completion is worth. Bounties record ``points: 2``; older
    records predate the field and count as the normal 1 punto. Requeue marker
    rows (``kind: "requeue"``) record an *action*, not a completion — they are
    worth 0 everywhere, so a 🔄 can never mint a punto."""
    if rec.get("kind") == "requeue":
        return 0
    p = rec.get("points")
    return int(p) if isinstance(p, (int, float)) and p > 0 else 1


def _rec_month(rec: dict) -> str:
    """The local-tz 'YYYY-MM' bucket a completion belongs to (tolerating very old
    records that only carry a 'ts')."""
    return rec.get("month") or str(rec.get("ts", ""))[:7]


def monthly_scores(records: list[dict], guild_id: int) -> dict[str, dict[int, dict]]:
    """Aggregate one guild's completions into
    ``{month: {user_id: {"points", "chores", "claps", "name"}}}`` — "claps" is
    how many 👏 bonuses the user *received* (one log record per clap), and
    "chores" counts only the user's own chore completions. Pitch-in / do-em-up
    / clap rows add their puntos but not a per-user chore: a game round is one
    chore *shared* by everyone who scored in it, folded into the footer total
    by :func:`build_leaderboard` (claps aren't chores at all)."""
    months: dict[str, dict[int, dict]] = {}
    for rec in records:
        if rec.get("guild_id") != guild_id:
            continue
        if rec.get("kind") == "requeue":
            continue  # zero-punto 🔄 markers feed titles only — never the board
        bucket = months.setdefault(_rec_month(rec), {})
        ent = bucket.setdefault(
            rec["user_id"], {"points": 0, "chores": 0, "claps": 0, "name": str(rec["user_id"])}
        )
        ent["points"] += _completion_points(rec)
        ent["chores"] += rec.get("kind") not in ("pitchin", "doemup", "clap")
        ent["claps"] += rec.get("kind") == "clap"
        ent["name"] = rec.get("user_name", ent["name"])
    return months


def star_counts(records: list[dict], guild_id: int, current_month: str) -> dict[int, int]:
    """Stars per user: one for each *past* month they led on puntos (a tie shares
    the star). The current (and any future) month isn't decided yet, so it's
    excluded — the title is still up for grabs until the month closes."""
    stars: dict[int, int] = {}
    for month, bucket in monthly_scores(records, guild_id).items():
        if not month or month >= current_month or not bucket:
            continue
        top = max(ent["points"] for ent in bucket.values())
        if top <= 0:
            continue
        for uid, ent in bucket.items():
            if ent["points"] == top:
                stars[uid] = stars.get(uid, 0) + 1
    return stars


def _guild_bar(cfg: Optional[dict]) -> int:
    """The guild's trinket bar (monthly puntos to earn one), defaulted & sane."""
    try:
        return max(1, int(cfg.get("item_bar")))  # type: ignore[union-attr]
    except (TypeError, ValueError, AttributeError):
        return trinkets.DEFAULT_BAR


# ---------------------------------------------------------------------------
# The bar is a value with a history, so closed months stay closed
# ---------------------------------------------------------------------------
_BAR_EPOCH = "1970-01-01T00:00:00+00:00"


def _bar_history(cfg: Optional[dict]) -> list[tuple[dt.datetime, int]]:
    """The guild's bar-change events as chronological ``(instant, bar)`` pairs.

    A config that predates ``bar_history`` reads as its scalar ``item_bar``
    having been in force forever — reinterpreted like legacy task rules, so
    nothing on disk needs migrating."""
    events: list[tuple[dt.datetime, int]] = []
    for ev in (cfg or {}).get("bar_history") or []:
        try:
            events.append((from_iso(ev["at"]), max(1, int(ev["bar"]))))
        except (KeyError, TypeError, ValueError):
            continue  # skip a mangled event rather than dropping the vitrine
    events.sort(key=lambda pair: pair[0])
    return events or [(from_iso(_BAR_EPOCH), _guild_bar(cfg))]


def _month_close_utc(month: str, tz: ZoneInfo) -> dt.datetime:
    """The UTC instant a ``YYYY-MM`` guild-local month ends (the following
    month's first local midnight)."""
    y, m = int(month[:4]), int(month[5:7])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return dt.datetime(y, m, 1, tzinfo=tz).astimezone(UTC)


def bar_for(cfg: Optional[dict], month: str) -> int:
    """The trinket bar governing ``month``: the last change made before the
    month's guild-local close. A still-open month has no close behind it, so
    every change qualifies and it floats with the latest bar — whatever is in
    force when the month ends is what freezes. Changing the bar therefore
    never redraws a finished month's trinkets."""
    tz = ZoneInfo(cfg["timezone"]) if cfg and cfg.get("timezone") else UTC
    try:
        close = _month_close_utc(month, tz)
    except (TypeError, ValueError):  # user-typed junk month — read it as open
        close = None
    history = _bar_history(cfg)
    bar = history[0][1]  # a month closed before any recorded change reads the earliest
    for at, b in history:
        if close is None or at < close:
            bar = b
    return bar


def record_bar_change(cfg: dict, new_bar: int, now: dt.datetime) -> None:
    """Append a bar change to ``cfg`` (pure dict mutation; call inside a txn).

    The first change seeds the history with the pre-change bar back-dated to
    the epoch, so months that closed before anyone touched the bar keep the
    bar they actually ended under. ``item_bar`` stays the current-value
    mirror (display, and the legacy fallback in ``_bar_history``)."""
    new_bar = max(1, int(new_bar))
    history = cfg.get("bar_history")
    if not isinstance(history, list):
        history = cfg["bar_history"] = []
    if not history:
        history.append({"at": _BAR_EPOCH, "bar": _guild_bar(cfg)})
    if history[-1].get("bar") != new_bar:  # a no-op "change" would only clutter
        history.append({"at": to_iso(now), "bar": new_bar})
    cfg["item_bar"] = new_bar


# ---------------------------------------------------------------------------
# Spice — ⬆️/⬇️ rank movement and 🔥 first-place streaks, derived from the
# log's timestamps. The frame of reference is the nightly ~23:59 posting:
# every draw compares the live standings against how they stood at the last
# nightly post, so the auto-post captures each full posting-to-posting day
# and a daytime /leaderboard shows the movement brewing since the board last
# went out. Nothing is stored — an undo redraws yesterday's picture the same
# way it redraws the scores.
# ---------------------------------------------------------------------------

# The nightly post lands within ~a minute after 23:59:00 (the 30s scheduler
# tick), so within this grace the frame hasn't rolled yet — the post itself
# measures the day it is closing rather than its own final seconds.
_POST_GRACE = dt.timedelta(minutes=2)


def _month_events(records: list[dict], guild_id: int,
                  month: str) -> list[tuple[dt.datetime, int, int]]:
    """One guild-month's completions as chronological ``(instant, user_id,
    puntos)`` — the time series behind :func:`rank_spice`. A record too old to
    carry a parsable ``ts`` reads as the epoch: ancient, hence present in
    every backward look, which is where it belongs."""
    events: list[tuple[dt.datetime, int, int]] = []
    for rec in records:
        if rec.get("guild_id") != guild_id or _rec_month(rec) != month:
            continue
        if rec.get("kind") == "requeue":
            continue  # 0-punto markers would still seed users into the ranking
        try:
            at = from_iso(rec["ts"])
        except (KeyError, TypeError, ValueError):
            at = from_iso(_BAR_EPOCH)
        events.append((at, rec["user_id"], _completion_points(rec)))
    events.sort(key=lambda ev: ev[0])
    return events


def _points_at(events: list[tuple[dt.datetime, int, int]],
               cutoff: Optional[dt.datetime] = None) -> dict[int, int]:
    """Puntos per user counting only events at or before ``cutoff`` (None = all)."""
    pts: dict[int, int] = {}
    for at, uid, p in events:
        if cutoff is not None and at > cutoff:
            break  # events are chronological
        pts[uid] = pts.get(uid, 0) + p
    return pts


def _ranks(pts: dict[int, int]) -> dict[int, int]:
    """Competition rank per user (1 = most puntos; equals share a rank), so a
    reshuffle among tied names never reads as movement."""
    return {uid: 1 + sum(q > p for q in pts.values()) for uid, p in pts.items()}


def _nightly_post_at(day: dt.date, tz: ZoneInfo) -> dt.datetime:
    """The UTC instant of ``day``'s nightly posting (guild-local 23:59)."""
    return dt.datetime(day.year, day.month, day.day, NIGHTLY_HOUR, NIGHTLY_MINUTE,
                       tzinfo=tz).astimezone(UTC)


def _last_post_day(now: dt.datetime, tz: ZoneInfo) -> dt.date:
    """The guild-local date whose nightly posting most recently passed,
    grace-adjusted — so the post firing seconds after 23:59 still frames the
    day it is closing, and by ~00:01 the frame has rolled to the new day."""
    local = (now - _POST_GRACE).astimezone(tz)
    day = local.date()
    if (local.hour, local.minute) < (NIGHTLY_HOUR, NIGHTLY_MINUTE):
        day -= dt.timedelta(days=1)
    return day


def rank_spice(records: list[dict], guild_id: int, tz: ZoneInfo,
               now: dt.datetime) -> dict[int, str]:
    """Emoji trim for the current month's ranking lines: whoever leads wears
    🔥×N for their run of consecutive nightly posts on top (the still-open day
    included; ties co-wear it, like star ties share the star), and everyone
    else ⬆️/⬇️ when their rank moved since the last nightly post. Returns
    ``{user_id: trim}``; a steady rank — or one with no prior post to move
    from — stays bare. The monthly board starts fresh, so streaks and arrows
    never reach past the 1st."""
    month = now.astimezone(tz).strftime("%Y-%m")
    events = _month_events(records, guild_id, month)
    if not events:
        return {}
    month_start = dt.datetime(int(month[:4]), int(month[5:7]), 1, tzinfo=tz).astimezone(UTC)
    baseline_day = _last_post_day(now, tz)

    ranks = _ranks(_points_at(events))
    prev_ranks = _ranks(_points_at(events, _nightly_post_at(baseline_day, tz)))

    out: dict[int, str] = {}
    for uid, rank in ranks.items():
        if rank == 1:
            streak, day = 1, baseline_day
            while True:
                close = _nightly_post_at(day, tz)
                if close < month_start:  # also bounds the walk for epoch events
                    break
                pts = _points_at(events, close)
                if pts.get(uid, 0) < max(pts.values(), default=1):
                    break  # someone else (or nobody) was on top at that close
                streak += 1
                day -= dt.timedelta(days=1)
            out[uid] = f"🔥×{streak}"
        elif uid in prev_ranks and prev_ranks[uid] != rank:
            out[uid] = "⬆️" if rank < prev_ranks[uid] else "⬇️"
    return out


def vitrine_for(records: list[dict], guild_id: int, user_id: int, cfg: Optional[dict],
                current_month: str) -> list[dict]:
    """Every trinket a user has earned: one deterministic roll per *whole multiple*
    of the bar their puntos reached, for each *past* month (50 puntos against a
    25-punto bar → two) — each month judged against the bar it closed under
    (:func:`bar_for`). Like stars, it's derived from the log — the current
    month is still in play, so it's excluded. Sorted oldest→newest, idx 0…n−1
    within a month."""
    out: list[dict] = []
    for month, bucket in sorted(monthly_scores(records, guild_id).items()):
        if not month or month >= current_month:
            continue
        ent = bucket.get(user_id)
        if not ent:
            continue
        for idx in range(ent["points"] // bar_for(cfg, month)):  # bar ≥ 1, guaranteed
            out.append(trinkets.roll_for(guild_id, user_id, month, idx))
    return out


# ---------------------------------------------------------------------------
# Title badges — pure titles derived from the completion log (same spirit as
# ⭐ stars: recomputed every draw, never stored). Scope follows the board:
# one month, or all-time when the all-time flag is set.
# ---------------------------------------------------------------------------
PUNCTUAL_GRACE_SECS = 59 * 60  # "within 59 minutes of due" still counts
BADGE_SHARE_MIN_PTS = 10  # Team Player / Lone Wolf need enough volume to compete

# Early Bird / Night Owl: guild-local clock windows over when a ✅ landed, as
# ``[start, end)`` hours with the owl's night wrapping past midnight. They are
# deliberately disjoint — a 01:00 ✅ reads as staying up late, not rising early.
EARLY_BIRD_WINDOW = (4, 9)  # 04:00–08:59
NIGHT_OWL_WINDOW = (21, 4)  # 21:00–03:59

# Fixed display order when one person holds several titles.
BADGE_ORDER = (
    "Punctualist",
    "Early Bird",
    "Night Owl",
    "Bounty Hunter",
    "Pitcher-Inner",
    "Unit Crusher",
    "Crowd Favorite",
    "Jack of All Chores",
    "One-Track Mind",
    "Closer",
    "Recurring Nightmare",
    "The Reanimator",
    "Team Player",
    "Lone Wolf",
    "Archaeologist",
)


def _is_chore(rec: dict) -> bool:
    """A solo chore row (not a pitch-in, do-em-up, clap bonus, or 🔄 marker)."""
    return rec.get("kind") not in ("pitchin", "doemup", "clap", "requeue")


def _in_window(hour: int, window: tuple[int, int]) -> bool:
    """Is ``hour`` inside the ``[start, end)`` clock window (wrapping past
    midnight when ``start > end``)?"""
    start, end = window
    return start <= hour < end if start < end else hour >= start or hour < end


def _empty_badge_stats() -> dict:
    return {
        "punctual": 0,
        "bounty": 0,
        "pitchin": 0,
        "doemup_pts": 0,
        "claps": 0,
        "once": 0,
        "recurring": 0,
        "requeue": 0,
        "early": 0,
        "night": 0,
        "task_counts": {},  # task_id -> n (chores only)
        "game_pts": 0,
        "chore_pts": 0,
        "total_pts": 0,
        "max_late": 0,
        "name": "?",
    }


def badge_stats(records: list[dict], guild_id: int,
                month: Optional[str] = None,
                tz: dt.tzinfo = UTC) -> dict[int, dict]:
    """Per-user counters for title badges over ``guild_id``.

    ``month=None`` means all-time; otherwise only rows in that local-tz month
    count. ``tz`` is the guild's zone, used to read each ✅'s wall-clock hour
    for Early Bird / Night Owl. Pure scan of the log — no store side effects."""
    out: dict[int, dict] = {}
    for rec in records:
        if rec.get("guild_id") != guild_id:
            continue
        if month is not None and _rec_month(rec) != month:
            continue
        uid = rec["user_id"]
        ent = out.get(uid)
        if ent is None:
            ent = out[uid] = _empty_badge_stats()
        ent["name"] = rec.get("user_name", ent["name"])
        pts = _completion_points(rec)
        ent["total_pts"] += pts
        kind = rec.get("kind")

        if kind == "requeue":
            # A 🔄 marker: pts is 0 by definition, so the total_pts add above
            # was a no-op — it counts toward The Reanimator and nothing else.
            ent["requeue"] += 1
            continue
        if kind == "clap":
            ent["claps"] += 1
            continue
        if kind == "pitchin":
            ent["pitchin"] += 1
            ent["game_pts"] += pts
            continue
        if kind == "doemup":
            ent["doemup_pts"] += pts
            ent["game_pts"] += pts
            continue

        # Solo chore (including legacy rows with no kind).
        ent["chore_pts"] += pts
        if kind == "once":
            ent["once"] += 1
        else:
            # "recurring", missing kind, or any other non-game row.
            ent["recurring"] += 1
        if pts == 2:
            ent["bounty"] += 1
        late = rec.get("late_seconds")
        if isinstance(late, (int, float)):
            late_i = int(late)
            if late_i <= PUNCTUAL_GRACE_SECS:
                ent["punctual"] += 1
            if late_i > ent["max_late"]:
                ent["max_late"] = late_i
        # Early Bird / Night Owl read the ✅'s guild-local wall-clock hour. Only
        # solo chores compete: a game payout row stamps the round's close (and a
        # clap its clapper's tap), not the member's own work.
        try:
            hour = from_iso(rec["ts"]).astimezone(tz).hour
        except (KeyError, TypeError, ValueError):
            pass  # a relic too old to carry a parsable ts has no clock to read
        else:
            ent["early"] += _in_window(hour, EARLY_BIRD_WINDOW)
            ent["night"] += _in_window(hour, NIGHT_OWL_WINDOW)
        tid = rec.get("task_id")
        if tid:
            counts = ent["task_counts"]
            counts[tid] = counts.get(tid, 0) + 1
    return out


def _leaders(stats: dict[int, dict], score_of, min_score: float = 1) -> list[int]:
    """User ids tied for the highest score, if that score is at least ``min_score``."""
    if not stats:
        return []
    scores = {uid: score_of(ent) for uid, ent in stats.items()}
    top = max(scores.values())
    if top < min_score:
        return []
    return [uid for uid, s in scores.items() if s == top]


def badge_titles(records: list[dict], guild_id: int,
                 month: Optional[str] = None,
                 tz: dt.tzinfo = UTC) -> dict[int, list[str]]:
    """Title badges held in ``guild_id`` for ``month`` (or all-time if None).

    Ties share a title. Only awarded when the winning metric is positive (and
    Team Player / Lone Wolf also need :data:`BADGE_SHARE_MIN_PTS` total puntos
    in-scope). ``tz`` feeds the clock-window badges (see :func:`badge_stats`).
    Returns ``{user_id: [title, …]}`` in :data:`BADGE_ORDER`."""
    stats = badge_stats(records, guild_id, month, tz)
    held: dict[int, list[str]] = {uid: [] for uid in stats}

    def award(name: str, uids: list[int]) -> None:
        for uid in uids:
            held.setdefault(uid, []).append(name)

    award("Punctualist", _leaders(stats, lambda e: e["punctual"]))
    award("Early Bird", _leaders(stats, lambda e: e["early"]))
    award("Night Owl", _leaders(stats, lambda e: e["night"]))
    award("Bounty Hunter", _leaders(stats, lambda e: e["bounty"]))
    award("Pitcher-Inner", _leaders(stats, lambda e: e["pitchin"]))
    award("Unit Crusher", _leaders(stats, lambda e: e["doemup_pts"]))
    award("Crowd Favorite", _leaders(stats, lambda e: e["claps"]))
    award("Jack of All Chores", _leaders(stats, lambda e: len(e["task_counts"])))
    award("One-Track Mind", _leaders(
        stats, lambda e: max(e["task_counts"].values()) if e["task_counts"] else 0
    ))
    award("Closer", _leaders(stats, lambda e: e["once"]))
    award("Recurring Nightmare", _leaders(stats, lambda e: e["recurring"]))
    award("The Reanimator", _leaders(stats, lambda e: e["requeue"]))
    # Share badges: non-competitors score -1 so they never win; min_score 0
    # rejects the all-ineligible case (top == -1).
    award("Team Player", _leaders(
        stats,
        lambda e: (e["game_pts"] / e["total_pts"])
        if e["total_pts"] >= BADGE_SHARE_MIN_PTS and e["game_pts"] > 0 else -1.0,
        min_score=0.0,
    ))
    award("Lone Wolf", _leaders(
        stats,
        lambda e: (e["chore_pts"] / e["total_pts"])
        if e["total_pts"] >= BADGE_SHARE_MIN_PTS and e["chore_pts"] > 0 else -1.0,
        min_score=0.0,
    ))
    award("Archaeologist", _leaders(stats, lambda e: e["max_late"]))

    # Drop empties; preserve BADGE_ORDER (awards already walk that order).
    return {uid: titles for uid, titles in held.items() if titles}


def _all_time_scores(records: list[dict], guild_id: int) -> dict[int, dict]:
    """Collapse every month into one ``{user_id: {points, chores, claps, name}}``."""
    bucket: dict[int, dict] = {}
    for month_bucket in monthly_scores(records, guild_id).values():
        for uid, ent in month_bucket.items():
            live = bucket.setdefault(
                uid, {"points": 0, "chores": 0, "claps": 0, "name": ent["name"]}
            )
            live["points"] += ent["points"]
            live["chores"] += ent["chores"]
            live["claps"] += ent["claps"]
            live["name"] = ent["name"]
    return bucket


def build_leaderboard(records: list[dict], guild_id: int, cfg: Optional[dict],
                      month: Optional[str] = None,
                      all_time: bool = False) -> tuple[str, bool]:
    """Render the leaderboard message for ``guild_id``.

    Default is the current (or given) month. ``all_time=True`` ranks lifetime
    puntos and derives titles from the full log (``month`` is ignored).

    Returns ``(text, is_empty)`` where ``is_empty`` is True when nothing was
    logged in scope (the slash command shows that variant ephemerally).
    Pure apart from reading the clock — shared by the ``/leaderboard`` command
    and the nightly auto-post in ``backup.py``."""
    tz = ZoneInfo(cfg["timezone"]) if cfg and cfg.get("timezone") else UTC
    now = now_utc()
    current_month = now.astimezone(tz).strftime("%Y-%m")
    if not all_time and month is None:
        month = current_month

    months = monthly_scores(records, guild_id)
    stars = star_counts(records, guild_id, current_month)

    # A one-line star roll-call, shown ONLY on the empty variant: the ranking
    # lines below already wear each holder's ⭐×n, so repeating them in a
    # separate section was redundant. (All-time names so an idle holder shows.)
    names = {uid: ent["name"] for bucket in months.values() for uid, ent in bucket.items()}
    star_line = ""
    if stars:
        holders = sorted(stars.items(), key=lambda kv: (-kv[1], names.get(kv[0], "").lower()))
        star_line = "⭐ **Stars** — " + " · ".join(f"<@{uid}> ×{n}" for uid, n in holders)

    if all_time:
        bucket = _all_time_scores(records, guild_id)
        title_scope: Optional[str] = None  # all-time badges
        scope_label = "all time"
    else:
        bucket = months.get(month or current_month, {})
        title_scope = month or current_month
        scope_label = title_scope

    if not bucket:
        if all_time:
            msg = "No chores logged yet. Get to work! 🚜"
        else:
            bar = bar_for(cfg, scope_label)
            msg = (f"No chores logged for **{scope_label}** yet. Get to work! 🚜\n"
                   + trinkets.zone_blurb(scope_label, bar,
                                         past=scope_label < current_month))
        if star_line:
            msg += "\n\n" + star_line
        return msg, True

    titles = badge_titles(records, guild_id, title_scope, tz)
    ranking = sorted(bucket.items(), key=lambda kv: (-kv[1]["points"], kv[1]["name"].lower()))
    # Spice rides only the live month — closed months and all-time have no
    # "since the last nightly post" to move against.
    spice = (
        rank_spice(records, guild_id, tz, now)
        if not all_time and scope_label == current_month else {}
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, ent) in enumerate(ranking):
        badge = (medals[i] if i < 3 else f"`{i + 1}.`") + spice.get(uid, "")
        star = f" ⭐×{stars[uid]}" if stars.get(uid) else ""
        clap = f" · 👏×{ent['claps']}" if ent.get("claps") else ""
        pts = ent["points"]
        lines.append(f"{badge} **{pts} punto{'' if pts == 1 else 's'}** — <@{uid}>{star}{clap}")
        held = titles.get(uid) or []
        if held:
            lines.append(f"*{' · '.join(held)}*")

    total_pts = sum(ent["points"] for ent in bucket.values())
    # A game round is one chore shared by all its scorers, not one per payout
    # row. Rows from the same close carry the same game id + timestamp, so
    # distinct pairs count rounds (each round of a recurring game is its own).
    game_rounds = {
        (rec.get("kind"), rec.get("task_id"), rec.get("ts"))
        for rec in records
        if rec.get("guild_id") == guild_id
        and (all_time or _rec_month(rec) == scope_label)
        and rec.get("kind") in ("pitchin", "doemup")
    }
    total_chores = sum(ent["chores"] for ent in bucket.values()) + len(game_rounds)
    if all_time:
        when = "all time"
    elif scope_label == current_month:
        when = "this month"
    else:
        when = f"in {scope_label}"
    footer = (
        f"_{total_chores} chore{'' if total_chores == 1 else 's'} · "
        f"{total_pts} punto{'' if total_pts == 1 else 's'} {when}._"
    )
    if not all_time and scope_label == current_month:
        footer += "\n⭐ Whoever tops the board when the month ends earns a star."

    if all_time:
        header = "🏆 **Chore leaderboard — all time**\n"
    else:
        bar = bar_for(cfg, scope_label)
        zone_note = trinkets.zone_blurb(scope_label, bar, past=scope_label < current_month)
        header = f"🏆 **Chore leaderboard — {scope_label}**\n{zone_note}\n"
    msg = header + "\n".join(lines)
    msg += "\n\n" + footer
    return msg, False


@bot.tree.command(name="leaderboard", description="Monthly chore puntos, titles & ⭐ stars")
@app_commands.describe(
    month="Month as YYYY-MM (defaults to the current month)",
    all_time="Show all-time puntos & titles instead of a single month",
)
async def leaderboard(interaction: discord.Interaction, month: Optional[str] = None,
                      all_time: bool = False) -> None:
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    records = store.read_completions()
    msg, empty = build_leaderboard(
        records, interaction.guild_id, cfg, month, all_time=all_time
    )
    await interaction.response.send_message(msg, ephemeral=empty, allowed_mentions=NO_PINGS)


@bot.tree.command(name="covet", description="Gaze upon a collection of trinkets won at month's end")
@app_commands.describe(user="Whose vitrine to view (default: yours)")
async def covet(interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
    target = user or interaction.user
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    tz = ZoneInfo(cfg["timezone"]) if cfg and cfg.get("timezone") else UTC
    current_month = now_utc().astimezone(tz).strftime("%Y-%m")
    bar = bar_for(cfg, current_month)

    records = store.read_completions()
    items = vitrine_for(records, interaction.guild_id, target.id, cfg, current_month)

    whose = "Your" if target.id == interaction.user.id else f"{target.display_name}'s"
    header = f"🖼️ **{whose} vitrine** — {len(items)} trinket{'' if len(items) == 1 else 's'}"

    # Group by month, newest first: a header (with a ×N count when that month
    # yielded several) over its indented items. `items` is already month-sorted,
    # so consecutive grouping is sound. Rendered flat as (line, is_trinket) pairs
    # — body is always a prefix of these — then greedily trimmed to stay under
    # Discord's 2000-char message limit.
    blocks: list[list[tuple[str, bool]]] = []
    for month, grp in itertools.groupby(items, key=lambda t: t["month"]):
        group = list(grp)
        suffix = f"  ×{len(group)}" if len(group) > 1 else ""
        # Lead the header with that month's *featured* zone; each item line then
        # carries its own zone emoji, so an off-season stray stands out at a glance.
        season = trinkets.zone_emoji(trinkets.zone_for_month(month))
        block: list[tuple[str, bool]] = [(f"{season} **{month}**{suffix}", False)]
        block.extend((f"  {trinkets.render_line(t)}", True) for t in group)
        blocks.append(block)
    # Newest month on top, but each header still leads its own items (idx 0…n) —
    # reverse the *group order*, not the flat lines.
    rendered = [pair for block in reversed(blocks) for pair in block]

    body: list[str] = []
    shown = used = 0
    for line, is_trinket in rendered:
        if body and used + len(line) + 1 > 1700:
            break
        body.append(line)
        used += len(line) + 1
        shown += is_trinket
    # Never strand a month header whose items got trimmed away.
    if body and not rendered[len(body) - 1][1]:
        body.pop()
    if not items:
        body.append("_The cabinet stands empty… for now._")
    elif shown < len(items):
        n = len(items) - shown
        body.append(f"… and {n} older trinket{'' if n == 1 else 's'}.")

    # Progress toward this month's (still-pending) trinkets — one per multiple of
    # the bar, so a high scorer is already stacking several.
    ent = monthly_scores(records, interaction.guild_id).get(current_month, {}).get(target.id)
    pts = ent["points"] if ent else 0
    secured = pts // bar
    to_next = bar - pts % bar  # 1…bar: puntos until the next trinket tips over
    zk = trinkets.zone_for_month(current_month)
    z = f"{trinkets.zone_emoji(zk)} {current_month}: **{trinkets.zone_label(zk)}** in season"
    if secured == 0:
        foot = f"{z} — **{pts}/{bar} puntos**, {to_next} to go for your first trinket"
    else:
        foot = (f"{z} — at **{pts} puntos** you've secured "
                f"**{secured} trinket{'' if secured == 1 else 's'}** ✨, "
                f"**{to_next}** more for the next")

    msg = header + "\n" + "\n".join(body) + "\n\n" + foot
    await interaction.response.send_message(msg, allowed_mentions=NO_PINGS)


__all__ = [
    "BADGE_ORDER",
    "BADGE_SHARE_MIN_PTS",
    "EARLY_BIRD_WINDOW",
    "NIGHT_OWL_WINDOW",
    "PUNCTUAL_GRACE_SECS",
    "_completion_points",
    "_guild_bar",
    "_rec_month",
    "badge_stats",
    "badge_titles",
    "bar_for",
    "build_leaderboard",
    "leaderboard",
    "monthly_scores",
    "covet",
    "rank_spice",
    "record_bar_change",
    "star_counts",
    "vitrine_for",
]
