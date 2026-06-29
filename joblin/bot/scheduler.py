from __future__ import annotations

import datetime as dt

import discord
from discord.ext import tasks

from ..models import (
    from_iso,
    now_utc,
    to_iso,
)
from .core import (
    bot,
    log,
    store,
)
from .helpers import (
    config_ready,
    guild_config,
    post_occurrence,
    safe_delete,
)
from .games import sweep_games



# ---------------------------------------------------------------------------
# Scheduler tick
# ---------------------------------------------------------------------------
@tasks.loop(seconds=30)
async def scheduler() -> None:
    now = now_utc()
    snap = await store.snapshot()
    for tid, task in list(snap["tasks"].items()):
        cfg = guild_config(snap, task["guild_id"])
        if not config_ready(cfg):
            continue
        channel = bot.get_channel(int(cfg["channel_id"]))
        if channel is None:
            continue
        try:
            pending = task.get("pending")
            if pending:
                if now >= from_iso(pending["remind_at"]):
                    await send_reminder(tid, channel, cfg)
            elif task.get("next_due") and now >= from_iso(task["next_due"]):
                await fire_task(tid, channel, cfg)
        except Exception:  # never let one bad task kill the loop
            log.exception("scheduler error on task %s", tid)

    await sweep_games(now, snap)


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

    message = await post_occurrence(channel, task, cfg, reminder=False)

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
            }
            live["next_due"] = None
            data["messages"][str(message.id)] = tid
    if orphan:
        await safe_delete(message)


async def send_reminder(tid: str, channel: discord.abc.Messageable, cfg: dict) -> None:
    snap = await store.snapshot()
    task = snap["tasks"].get(tid)
    pending = task.get("pending") if task else None
    if not pending or now_utc() < from_iso(pending["remind_at"]):
        return

    message = await post_occurrence(channel, task, cfg, reminder=True)

    orphan = False
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        p = live.get("pending") if live else None
        if not p:
            orphan = True
        else:
            p["message_ids"].append(message.id)
            p["remind_at"] = to_iso(now_utc() + dt.timedelta(hours=1))
            data["messages"][str(message.id)] = tid
            # Lifetime tally of how often this chore has had to be nagged — it
            # outlives each occurrence (never reset on completion) and is surfaced
            # in /listtasks so the persistent foot-draggers stand out.
            live["nag_count"] = live.get("nag_count", 0) + 1
    if orphan:
        await safe_delete(message)


__all__ = [
    "_before_scheduler",
    "fire_task",
    "scheduler",
    "send_reminder",
]
