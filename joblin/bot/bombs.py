"""Puntobombs — one-off chores with a fuse (💣).

A puntobomb is a plain *task* (it lives in ``tasks`` and rides the normal
fire/nag/button lifecycle) carrying ``puntobomb: True`` and a required
``explodes_at``. The extra clause lives here: ✅ before the fuse runs out is a
defusal (a 1-punto completion, kind ``"puntobomb"`` — handled in
``reactions._handle_done``); past it, the scheduler calls
:func:`explode_puntobomb` and everyone in the game — every user in the guild's
completion log (:func:`scoring.puntobomb_casualties`) — is docked
``PUNTOBOMB_PENALTY`` via a ``kind: "kaboom"`` log row, the economy's one
sanctioned negative. Deleting the bomb instead (❌ on the post, ``/deletetask``,
or the web UI) is always permitted — The Coward's Way Out — and moves no puntos.

Restart-safe the usual way: the fuse is the persisted ``explodes_at`` compared
against ``now`` each tick, so a bomb that blew during downtime detonates on the
next tick (even if it never got to post)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import discord

from ..models import (
    PUNTOBOMB_PENALTY,
    from_iso,
    new_id,
    now_utc,
    render_coward,
    render_kaboom,
    to_iso,
)
from .core import NO_PINGS, bot, store
from .helpers import (
    declutter_enabled,
    finalize_messages,
    guild_config,
    sweep_occurrence_posts,
)
from .scoring import puntobomb_casualties
from .daily_log import refresh_daily_log
from .reactions import _delete_panels, _take_task_panels


def kaboom_records(task: dict, victims: list[dict], tz: ZoneInfo,
                   now: dt.datetime) -> list[dict]:
    """The penalty rows a blown puntobomb owes: one −PUNTOBOMB_PENALTY per
    player in the game, same shape as every other completion-log row."""
    return [
        {
            "id": new_id(),
            "ts": to_iso(now),
            "month": now.astimezone(tz).strftime("%Y-%m"),  # local-tz bucket
            "guild_id": task["guild_id"],
            "task_id": task["id"],
            "brief": task["brief"],
            "user_id": v["user_id"],
            "user_name": v["user_name"],
            "kind": "kaboom",
            "points": -PUNTOBOMB_PENALTY,
            "due_at": task.get("explodes_at"),
            "late_seconds": 0,
        }
        for v in victims
    ]


async def explode_puntobomb(
    tid: str, channel: discord.abc.Messageable, cfg: dict
) -> bool:
    """Blow a puntobomb whose fuse has run out: pop the task, dock everyone in
    the game, and rewrite its live post as the blast (or post the blast fresh
    if downtime meant it never fired). Popping inside the txn is the guard —
    a ✅ racing this tick either defuses first (we find no bomb) or loses (its
    stale press whispers). Returns False if there was nothing to blow."""
    snap = await store.snapshot()
    task = snap["tasks"].get(tid)
    if not task or not task.get("puntobomb") or not task.get("explodes_at"):
        return False
    now = now_utc()
    if now < from_iso(task["explodes_at"]):
        return False
    # Resolve everything fallible before popping the bomb — a bad timezone
    # must abort here, not after the txn (that would be a silent free defuse).
    tz = ZoneInfo(cfg["timezone"])

    removed = None
    message_ids: list[int] = []
    legacy = False
    panels: list = []
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        if not live or not live.get("puntobomb"):
            return False  # defused or deleted while we were looking
        p = live.get("pending") or {}
        message_ids = list(p.get("message_ids", []))
        legacy = bool(p) and p.get("ui") != "buttons"
        for mid in message_ids:
            data["messages"].pop(str(mid), None)
        # A snooze on the bomb may have armed an undo — none of it survives.
        for table in ("undo", "requeue", "claps"):
            for mid, rec in list(data[table].items()):
                if rec.get("task_id") == tid:
                    data[table].pop(mid, None)
        panels = _take_task_panels(data, tid)
        removed = data["tasks"].pop(tid, None)
    if not removed:
        return False

    victims = puntobomb_casualties(store.read_completions(), removed["guild_id"])
    for rec in kaboom_records(removed, victims, tz, now):
        await store.log_completion(rec)

    status = render_kaboom(removed, victims)
    if message_ids:
        await finalize_messages(channel, message_ids, status, legacy=legacy, view=None)
        if declutter_enabled(cfg) and len(message_ids) > 1:
            # Same declutter as a ✅: the blast anchor tells the story, the
            # superseded fire post and nags go (touched ones stay).
            await sweep_occurrence_posts(channel, tid, message_ids,
                                         keep=message_ids[-1])
    else:
        # Armed but never posted (downtime past both the due and the fuse):
        # the blast still needs announcing.
        try:
            await channel.send(status, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass
    await _delete_panels(panels)
    if victims:
        await refresh_daily_log(removed["guild_id"], now)
    return True


async def coward_strike(removed: dict) -> None:
    """Strike a *deleted* (not defused, not blown) bomb's live posts through
    with The Coward's Way Out, so the channel doesn't keep showing a ticking
    bomb. Shared by ``/deletetask`` and the web UI's delete — the ❌ button
    lands the same wording through its own status edit."""
    p = removed.get("pending") or {}
    mids = list(p.get("message_ids", []))
    if not mids:
        return
    ch = bot.get_channel(int(p["channel_id"])) if p.get("channel_id") else None
    if ch is None:
        return
    await finalize_messages(
        ch, mids, render_coward(removed["brief"]),
        legacy=p.get("ui") != "buttons", view=None,
    )
    snap = await store.snapshot()
    if declutter_enabled(guild_config(snap, removed["guild_id"])) and len(mids) > 1:
        await sweep_occurrence_posts(ch, removed["id"], mids, keep=mids[-1])


__all__ = [
    "coward_strike",
    "explode_puntobomb",
    "kaboom_records",
]
