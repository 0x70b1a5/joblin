"""The 📜 Daily Log — one embed per guild-day that grows as punto events land.

The log is *derived, never accumulated*: every redraw re-renders the whole day
from ``completions.jsonl`` (the same recompute-from-log spirit as ⭐ stars), so
an ↩️ undo or a late 👏 corrects the picture exactly the way the leaderboard
corrects its scores, and a restart loses nothing. Only the log message's
address persists (``store["daily_log"][guild_id][day] -> {message_id,
channel_id}``, pruned to the last few days).

The "day" is scoring's nightly frame, not calendar midnight: it rolls at the
~23:59 nightly post (grace-adjusted via :func:`scoring._last_post_day`), so the
log and the auto-leaderboard can never disagree about what "today" meant.

Lines are strictly chronological — rows are sorted by their logged instant, and
rows written together (same instant, task, kind: a 🧾 list's tickers, a
pitch-in's scorers, one clap's beneficiaries) collapse into a single line.
Zero-punto marker rows (🔄 requeues, ⏭️ skips) are skipped, like everywhere
else outside their badge tallies.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from ..models import (
    EMOJI_FLEX,
    EMOJI_HANDSHAKE,
    EMOJI_LIST,
    _join,
    discord_ts,
    from_iso,
    now_utc,
    to_iso,
)
from .core import (
    NO_PINGS,
    bot,
    log,
    store,
)
from .helpers import config_ready, guild_config
from .scoring import MARKER_KINDS, _completion_points, _last_post_day

# How many day-frames keep an editable log message on the books (an undo can
# still redraw a closed day's log while its entry survives; older voids just
# leave the stale embed behind — the board-level truth is unaffected).
LOG_KEEP_DAYS = 3
# A touch (a member's reaction/reply on one of our posts) outlives its post's
# sweep window by weeks, then lets go — pruned at the same rare write moment
# a new day's log message is first posted.
TOUCH_KEEP = dt.timedelta(days=30)
# Headroom under Discord's 4096-char embed-description cap.
DESC_LIMIT = 3900


def log_day(at: dt.datetime, tz: ZoneInfo) -> dt.date:
    """The guild-local day-frame open at ``at``: the day after the most recent
    nightly post, grace-adjusted exactly like rank_spice — the two must never
    disagree about an instant near the 23:59 roll."""
    return _last_post_day(at, tz) + dt.timedelta(days=1)


# ---------------------------------------------------------------------------
# Pure rendering (no Discord, no lock) — easy to unit-test
# ---------------------------------------------------------------------------
def _day_groups(records: list[dict], guild_id: int, day: dt.date,
                tz: ZoneInfo) -> list[list[dict]]:
    """The day's loggable rows, grouped into display events. Rows are sorted by
    their instant first — the log reads top-to-bottom as the day happened — and
    rows written together (same instant, task, kind) form one group."""
    rows: list[tuple[dt.datetime, dict]] = []
    for rec in records:
        if rec.get("guild_id") != guild_id or rec.get("kind") in MARKER_KINDS:
            continue
        try:
            at = from_iso(rec["ts"])
        except (KeyError, TypeError, ValueError):
            continue  # a row too old to carry a ts can't be placed in a day
        if log_day(at, tz) != day:
            continue
        rows.append((at, rec))
    rows.sort(key=lambda pair: pair[0])
    groups: dict[tuple, list[dict]] = {}
    for _, rec in rows:
        key = (rec.get("ts"), rec.get("task_id"), rec.get("kind"))
        groups.setdefault(key, []).append(rec)
    return list(groups.values())


def _mentions(group: list[dict]) -> str:
    seen: set = set()
    out: list[str] = []
    for rec in group:
        uid = rec.get("user_id")
        if uid in seen:
            continue
        seen.add(uid)
        out.append(f"<@{uid}>")
    return _join(out)


def _line(group: list[dict]) -> str:
    """One display line for one event's row group, led by its clock time (a
    Discord timestamp, so every viewer reads their own zone)."""
    rec = group[0]
    kind = rec.get("kind")
    t = discord_ts(from_iso(rec["ts"]), "t")
    brief = rec.get("brief") or "—"
    who = _mentions(group)
    if kind == "kaboom":
        return f"{t} 💥 **{brief}** — −{abs(_completion_points(rec))} each: {who}"
    if kind == "puntobomb":
        return f"{t} ✂️ **{brief}** — defused by {who}"
    if kind == "clap":
        return f"{t} 👏 **{brief}** — +1 to {who}"
    if kind == "pitchin":
        each = " each" if len(group) > 1 else ""
        return f"{t} {EMOJI_HANDSHAKE} **{brief}** — +{_completion_points(rec)}{each} to {who}"
    if kind == "doemup":
        parts = _join([f"<@{r.get('user_id')}> +{_completion_points(r)}" for r in group])
        return f"{t} {EMOJI_FLEX} **{brief}** — {parts}"
    if kind == "list":
        each = " — +1 each" if len(group) > 1 else ""
        return f"{t} ✅{EMOJI_LIST} **{brief}** — {who}{each}"
    # Plain chore (once/recurring/legacy no-kind); a bounty carries its 💰.
    bounty = " 💰 +2" if _completion_points(rec) >= 2 else ""
    return f"{t} ✅ **{brief}** — {who}{bounty}"


def daily_log_lines(records: list[dict], guild_id: int, day: dt.date,
                    tz: ZoneInfo) -> list[str]:
    """The day's log, one chronological line per event ([] for a blank day)."""
    return [_line(g) for g in _day_groups(records, guild_id, day, tz)]


def day_puntos(records: list[dict], guild_id: int, day: dt.date,
               tz: ZoneInfo) -> int:
    """The day's net puntos (kabooms keep their sign), for the embed footer."""
    return sum(_completion_points(r)
               for g in _day_groups(records, guild_id, day, tz) for r in g)


def clip_lines(lines: list[str], limit: int = DESC_LIMIT) -> list[str]:
    """Keep the log under the embed cap by dropping the *oldest* lines (the
    newest are the ones being read), announcing how many fell off the top."""
    def total(ls: list[str]) -> int:
        return sum(len(line) + 1 for line in ls)
    if total(lines) <= limit:
        return lines
    kept = list(lines)
    dropped = 0
    while kept and total(kept) > limit - 40:
        kept.pop(0)
        dropped += 1
    plural = "s" if dropped != 1 else ""
    return [f"*…{dropped} earlier line{plural} clipped*"] + kept


def build_daily_log_embed(lines: list[str], day: dt.date,
                          total: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"📜 Daily Log — {day.strftime('%A %d %b')}",
        description="\n".join(clip_lines(lines)),
        colour=discord.Colour.gold(),
    )
    embed.set_footer(text=f"{total} punto{'s' if total != 1 else ''} today")
    return embed


# ---------------------------------------------------------------------------
# Refreshing the posted embed
# ---------------------------------------------------------------------------
# One lock per guild so two completions landing together can't both see "no log
# message yet" and double-post the embed. In-memory is fine: it guards a race,
# not state — after a restart the persisted message id does the remembering.
_locks: dict[int, asyncio.Lock] = {}


def _guild_lock(guild_id: int) -> asyncio.Lock:
    return _locks.setdefault(guild_id, asyncio.Lock())


async def refresh_daily_log(guild_id: int,
                            at: Optional[dt.datetime] = None) -> None:
    """Redraw (or first-post) the guild's 📜 for the day-frame open at ``at``
    (default now). Called after anything that changes the completion log; never
    raises — a chore's own confirmation must not depend on its log line."""
    try:
        async with _guild_lock(guild_id):
            ready = await _guild_day_context(guild_id, at or now_utc())
            if ready is not None:
                await _refresh_day(guild_id, *ready)
    except Exception:
        log.exception("daily log refresh failed for guild %s", guild_id)


async def refresh_all_daily_logs(guild_id: int) -> None:
    """Redraw every day-log message still on the books for a guild — the undo
    path's tool, since a voided row may belong to a day that already rolled."""
    try:
        async with _guild_lock(guild_id):
            ready = await _guild_day_context(guild_id, now_utc())
            if ready is None:
                return
            tz, open_day = ready
            snap = await store.snapshot()
            days = {open_day}
            for key in (snap.get("daily_log", {}).get(str(guild_id)) or {}):
                try:
                    days.add(dt.date.fromisoformat(key))
                except ValueError:
                    continue
            for day in sorted(days):
                await _refresh_day(guild_id, tz, day)
    except Exception:
        log.exception("daily log refresh failed for guild %s", guild_id)


async def _guild_day_context(
    guild_id: int, at: dt.datetime
) -> Optional[tuple[ZoneInfo, dt.date]]:
    snap = await store.snapshot()
    cfg = guild_config(snap, guild_id)
    if not config_ready(cfg):
        return None
    try:
        tz = ZoneInfo(cfg["timezone"])
    except Exception:
        return None
    return tz, log_day(at, tz)


async def _refresh_day(guild_id: int, tz: ZoneInfo, day: dt.date) -> None:
    snap = await store.snapshot()
    cfg = guild_config(snap, guild_id)
    if not config_ready(cfg):
        return
    key = day.isoformat()
    entry = (snap.get("daily_log", {}).get(str(guild_id)) or {}).get(key)
    records = store.read_completions()
    lines = daily_log_lines(records, guild_id, day, tz)

    if entry:
        channel = bot.get_channel(int(entry.get("channel_id")
                                      or cfg["channel_id"]))
        if channel is None:
            return
        pm = channel.get_partial_message(int(entry["message_id"]))
        if not lines:
            # The day's last event was undone — an empty log is just clutter.
            await _drop_entry(guild_id, key)
            try:
                await pm.delete()
            except discord.HTTPException:
                pass
            return
        embed = build_daily_log_embed(lines, day,
                                      day_puntos(records, guild_id, day, tz))
        try:
            await pm.edit(embed=embed)
            return
        except discord.NotFound:
            pass  # someone deleted the log message by hand — repost below
        except discord.HTTPException as e:
            log.warning("daily log edit failed for guild %s: %s", guild_id, e)
            return
    if not lines:
        return
    channel = bot.get_channel(int(cfg["channel_id"]))
    if channel is None:
        return
    embed = build_daily_log_embed(lines, day,
                                  day_puntos(records, guild_id, day, tz))
    try:
        message = await channel.send(embed=embed, allowed_mentions=NO_PINGS)
    except discord.HTTPException as e:
        log.warning("daily log post failed for guild %s: %s", guild_id, e)
        return
    async with store.txn() as data:
        book = data.setdefault("daily_log", {}).setdefault(str(guild_id), {})
        book[key] = {"message_id": message.id, "channel_id": channel.id}
        cutoff = day - dt.timedelta(days=LOG_KEEP_DAYS)
        for old in list(book):
            try:
                stale = dt.date.fromisoformat(old) < cutoff
            except ValueError:
                stale = True
            if stale:
                book.pop(old, None)
        # This rare write moment doubles as the touched table's tidy-up.
        horizon = to_iso(now_utc() - TOUCH_KEEP)
        touched = data.setdefault("touched", {})
        for mid, ts in list(touched.items()):
            if ts < horizon:
                touched.pop(mid, None)


async def _drop_entry(guild_id: int, key: str) -> None:
    async with store.txn() as data:
        (data.get("daily_log", {}).get(str(guild_id)) or {}).pop(key, None)


__all__ = [
    "DESC_LIMIT",
    "LOG_KEEP_DAYS",
    "build_daily_log_embed",
    "clip_lines",
    "daily_log_lines",
    "day_puntos",
    "log_day",
    "refresh_all_daily_logs",
    "refresh_daily_log",
]
