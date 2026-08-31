from __future__ import annotations

import datetime as dt

import discord
from discord.ext import tasks

from ..models import (
    from_iso,
    now_utc,
    quiet_tick,
    to_iso,
)
from .core import (
    bot,
    log,
    store,
)
from .helpers import (
    config_ready,
    declutter_enabled,
    guild_config,
    post_occurrence,
    safe_delete,
    sweep_occurrence_posts,
)
from .games import sweep_games
from .backup import run_daily_backups
from .month_close import run_month_closes
from .listing import run_hourly_listopen



# ---------------------------------------------------------------------------
# Scheduler tick
# ---------------------------------------------------------------------------
async def sync_quiet_windows(now: dt.datetime) -> None:
    """Flip each guild's quiet switch on daily-window edges; persist only
    when the switch (or the last-sampled membership) actually moved."""
    snap = await store.snapshot()
    updates = []
    for gid, cfg in (snap.get("configs") or {}).items():
        delta, _ = quiet_tick(cfg, now)
        if delta:
            updates.append((gid, delta))
    if not updates:
        return
    async with store.txn() as data:
        for gid, delta in updates:
            cfg = (data.get("configs") or {}).get(gid)
            if cfg is not None:
                cfg.update(delta)


async def run_scheduler_tick() -> None:
    """One pass of the 30s loop. Extracted so tests can drive it without
    starting the discord.ext.tasks Loop."""
    now = now_utc()
    await sync_quiet_windows(now)
    snap = await store.snapshot()
    for tid, task in list(snap["tasks"].items()):
        cfg = guild_config(snap, task["guild_id"])
        if not config_ready(cfg):
            continue
        channel = bot.get_channel(int(cfg["channel_id"]))
        if channel is None:
            continue
        try:
            # Guild quiet time hushes fires, nags, and kabooms; EOD/SOD
            # bookkeeping (backup, month close, daily-log pin) still runs
            # below. A manual 🔄 requeue calls fire_task directly and is
            # not gated here.
            if cfg.get("quiet"):
                continue
            # A puntobomb's fuse outranks everything: past explodes_at it blows —
            # pending, snoozed, shushed, or (after downtime) never even posted.
            if (task.get("puntobomb") and task.get("explodes_at")
                    and now >= from_iso(task["explodes_at"])):
                from .bombs import explode_puntobomb  # runtime import — no cycle
                await explode_puntobomb(tid, channel, cfg)
                continue
            pending = task.get("pending")
            if pending:
                if not task.get("no_nag") and now >= from_iso(pending["remind_at"]):
                    await send_reminder(tid, channel, cfg)
            elif task.get("next_due") and now >= from_iso(task["next_due"]):
                await fire_task(tid, channel, cfg)
        except Exception:  # never let one bad task kill the loop
            log.exception("scheduler error on task %s", tid)

    await sweep_games(now, snap)
    # After fires/nags/game-rounds have landed, a single /listopen digest
    # (jump links, not a game bump) iff this guild-local hour has a chore
    # on the schedule. Fresh snapshot inside — this ``snap`` predates the loop.
    await run_hourly_listopen(now)
    await run_daily_backups(now, snap)
    await run_month_closes(now, snap)


@tasks.loop(seconds=30)
async def scheduler() -> None:
    await run_scheduler_tick()


@scheduler.before_loop
async def _before_scheduler() -> None:
    await bot.wait_until_ready()


async def fire_task(tid: str, channel: discord.abc.Messageable, cfg: dict) -> None:
    snap = await store.snapshot()
    task = snap["tasks"].get(tid)
    if not task or task.get("pending") or not task.get("next_due"):
        return
    if now_utc() < from_iso(task["next_due"]):
        return

    message = await post_occurrence(channel, tid, task, cfg, reminder=False)

    orphan = False
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        if not live or live.get("pending") or not live.get("next_due"):
            orphan = True  # resolved/deleted while we were posting
        else:
            due = live["next_due"]
            live["pending"] = {
                "due_at": due,
                "remind_at": to_iso(from_iso(due) + dt.timedelta(hours=1)),
                "ffwd_count": 0,
                "channel_id": getattr(channel, "id", None),
                "message_ids": [message.id],
                "ui": "buttons",  # posts carry views, not self-reactions
            }
            live["next_due"] = None
            data["messages"][str(message.id)] = tid
    if orphan:
        await safe_delete(message)
        return


async def send_reminder(tid: str, channel: discord.abc.Messageable, cfg: dict) -> None:
    snap = await store.snapshot()
    task = snap["tasks"].get(tid)
    pending = task.get("pending") if task else None
    if not pending or task.get("no_nag") or now_utc() < from_iso(pending["remind_at"]):
        return  # nothing pending, shushed (🤫), or not yet time

    message = await post_occurrence(channel, tid, task, cfg, reminder=True)

    orphan = False
    prior_mids: list[int] = []
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        p = live.get("pending") if live else None
        if not p:
            orphan = True
        else:
            prior_mids = list(p["message_ids"])
            p["message_ids"].append(message.id)
            p["remind_at"] = to_iso(now_utc() + dt.timedelta(hours=1))
            data["messages"][str(message.id)] = tid
            # Lifetime tally of how often this chore has had to be nagged — it
            # outlives each occurrence (never reset on completion) and is surfaced
            # in /listtasks so the persistent foot-draggers stand out.
            live["nag_count"] = live.get("nag_count", 0) + 1
    if orphan:
        await safe_delete(message)
        return
    if prior_mids and declutter_enabled(cfg):
        # Rolling declutter: the fresh nag supersedes the older posts, so the
        # untouched ones go — the channel holds at most one live post per chore
        # (plus any post the family has actually reacted to or replied on).
        await sweep_occurrence_posts(channel, tid, prior_mids, keep=None)


__all__ = [
    "_before_scheduler",
    "fire_task",
    "run_scheduler_tick",
    "scheduler",
    "send_reminder",
    "sync_quiet_windows",
]
