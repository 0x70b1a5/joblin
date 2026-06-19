"""The Discord bot: slash commands, the scheduler tick, and reactions.

Lifecycle of a task occurrence
------------------------------
1. The scheduler tick (every 30s) notices ``now >= next_due`` and *fires* it:
   posts the brief to the configured channel and self-reacts ✅ ⏩ ℹ️ ❌
   (ℹ️ only if the task has a long description). The task flips to "pending"
   with ``remind_at = due + 1h``; ``next_due`` is cleared so it can't re-fire.
2. While pending, every tick checks ``remind_at``. When it passes, the bot
   posts a fresh nag (optionally pinging a role) and sets ``remind_at = now+1h``.
3. Reactions resolve or defer the occurrence:
     ✅  complete  -> log the completer; recurring tasks roll to the next slot,
                      one-offs are removed.
     ⏩  fast-fwd  -> snooze 1h, then 2h, 4h, 8h ... (doubling each press).
     ℹ️  info      -> reply with the long description.
     ❌  skip      -> recurring: skip just this occurrence; one-off: delete it.
                      (Deleting an entire recurring task is /deletetask.)
     ↩️  undo      -> reverse the most recent ✅/⏩/❌ on that occurrence. The
                      bot adds this button right after one of those actions.

Everything is keyed off ``store["messages"][message_id] -> task_id`` so that
reactions keep working across restarts, and the persisted ``remind_at`` means
nags survive restarts too.

Undo
----
Each of the three mutating actions stashes a deep copy of the task *as it was
just before the action* into ``store["undo"][anchor_message_id]`` (plus the
completion-log id for ✅) and self-reacts ↩️ on the message showing the result.
Undo simply restores that snapshot — after first checking the occurrence hasn't
moved on (``can_undo``), so we never clobber a newer occurrence — and voids the
logged completion when reverting a ✅. Like the rest of the store it survives
restarts, so the ↩️ button keeps working after a reboot.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .models import (
    DIGIT_BY_KEY,
    DIGIT_EMOJI,
    EMOJI_DELETE,
    EMOJI_DONE,
    EMOJI_FFWD,
    EMOJI_INFO,
    EMOJI_SNOOZE_DAYS,
    EMOJI_SNOOZE_HOURS,
    EMOJI_UNDO,
    SNOOZE_CHOICES,
    UTC,
    describe_repeat,
    discord_ts,
    emoji_key,
    first_due,
    from_iso,
    new_id,
    next_due,
    now_utc,
    parse_repeat,
    recurrence_of,
    resolve_when,
    time_of_day_from,
    to_iso,
)
from .store import Store

log = logging.getLogger("farmtracker")

DATA_DIR = pathlib.Path(os.getenv("FARMTRACKER_DATA_DIR", "data"))
store = Store(DATA_DIR / "store.json", DATA_DIR / "completions.jsonl")

NO_PINGS = discord.AllowedMentions.none()

# A small curated list for the /farmconfig timezone autocomplete. Any valid
# IANA name is accepted; this is just for convenience.
COMMON_TZS = [
    "UTC",
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "America/Halifax",
    "America/Sao_Paulo", "America/Mexico_City",
    "Europe/London", "Europe/Dublin", "Europe/Lisbon", "Europe/Madrid",
    "Europe/Paris", "Europe/Berlin", "Europe/Amsterdam", "Europe/Rome",
    "Europe/Zurich", "Europe/Warsaw", "Europe/Athens", "Europe/Helsinki",
    "Europe/Istanbul", "Europe/Moscow",
    "Africa/Johannesburg", "Africa/Nairobi", "Africa/Cairo",
    "Asia/Jerusalem", "Asia/Dubai", "Asia/Kolkata", "Asia/Bangkok",
    "Asia/Shanghai", "Asia/Tokyo", "Asia/Singapore",
    "Australia/Perth", "Australia/Sydney", "Pacific/Auckland",
]


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class FarmBot(commands.Bot):
    def __init__(self) -> None:
        # Default (non-privileged) intents cover guilds + reactions, which is
        # all we need: slash commands and raw reaction events. We do NOT need
        # message_content or members.
        intents = discord.Intents.default()
        super().__init__(command_prefix="!unused!", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        scheduler.start()
        dev_guild = os.getenv("DEV_GUILD_ID")
        if dev_guild:
            # Sync to one guild for instant availability while developing.
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", dev_guild)
        else:
            await self.tree.sync()
            log.info("Synced global commands (may take up to ~1h to appear)")


bot = FarmBot()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def guild_config(snapshot: dict, guild_id: int) -> Optional[dict]:
    return snapshot["configs"].get(str(guild_id))


def config_ready(cfg: Optional[dict]) -> bool:
    return bool(cfg and cfg.get("channel_id") and cfg.get("timezone"))


def schedule_label(task: dict) -> str:
    rule = recurrence_of(task)
    if rule["freq"] == "once":
        return "one-off"
    return f"{describe_repeat(rule)} at {task.get('time_of_day')}"


def post_content(task: dict, *, reminder: bool, cfg: dict) -> str:
    brief = task["brief"]
    if not reminder:
        return f"**{brief}**"
    role_id = cfg.get("reminder_role_id")
    prefix = f"<@&{role_id}> " if role_id else ""
    return f"{prefix}⏰ Still pending: **{brief}**"


async def add_task_reactions(message: discord.Message, task: dict) -> None:
    await message.add_reaction(EMOJI_DONE)
    await message.add_reaction(EMOJI_FFWD)
    if task.get("description"):
        await message.add_reaction(EMOJI_INFO)
    await message.add_reaction(EMOJI_DELETE)


async def post_occurrence(
    channel: discord.abc.Messageable, task: dict, cfg: dict, *, reminder: bool
) -> discord.Message:
    allowed = (
        discord.AllowedMentions(roles=True)
        if reminder and cfg.get("reminder_role_id")
        else NO_PINGS
    )
    msg = await channel.send(
        post_content(task, reminder=reminder, cfg=cfg), allowed_mentions=allowed
    )
    await add_task_reactions(msg, task)
    return msg


async def safe_delete(message: Optional[discord.Message]) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        pass


async def finalize_messages(
    channel: discord.abc.Messageable, message_ids: list[int], status: str
) -> None:
    """Clear reactions on every message of a resolved occurrence and rewrite
    the most recent one with a status line."""
    for mid in message_ids:
        pm = channel.get_partial_message(mid)
        try:
            await pm.clear_reactions()
        except discord.HTTPException:
            pass
    if message_ids:
        last = channel.get_partial_message(message_ids[-1])
        try:
            await last.edit(content=status, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass


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
    if orphan:
        await safe_delete(message)


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    if bot.user and payload.user_id == bot.user.id:
        return  # ignore our own self-reactions
    if payload.guild_id is None:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    key = emoji_key(payload.emoji)

    # Undo (↩️) is keyed off the separate "undo" table rather than the live
    # message map, because a completed/skipped message has been de-registered
    # from "messages" — yet we still want its ↩️ button to work.
    if key == emoji_key(EMOJI_UNDO):
        await _handle_undo(payload, channel)
        return

    snap = await store.snapshot()
    if str(payload.message_id) in snap["snooze_panels"]:
        await _handle_snooze_panel(payload, channel)
        return
    tid = snap["messages"].get(str(payload.message_id))
    if not tid:
        return
    task = snap["tasks"].get(tid)
    if not task:
        return
    cfg = guild_config(snap, task["guild_id"])
    if not config_ready(cfg):
        return

    tz = ZoneInfo(cfg["timezone"])
    reacted = channel.get_partial_message(payload.message_id)
    member = payload.member
    mention = member.mention if member else f"<@{payload.user_id}>"
    display = member.display_name if member else str(payload.user_id)

    if key == emoji_key(EMOJI_INFO):
        await _handle_info(task, channel, reacted, payload)
    elif key == emoji_key(EMOJI_FFWD):
        await _handle_ffwd(tid, task, channel, reacted, payload)
    elif key == emoji_key(EMOJI_DONE):
        await _handle_done(tid, task, cfg, tz, channel, payload, mention, display)
    elif key == emoji_key(EMOJI_DELETE):
        await _handle_skip_or_delete(tid, task, tz, channel, mention)


async def _remove_user_reaction(
    message: discord.PartialMessage, payload: discord.RawReactionActionEvent
) -> None:
    """Remove the clicker's reaction so a toggle button can be pressed again.
    Requires the bot to have Manage Messages; ignored otherwise."""
    user = payload.member or discord.Object(id=payload.user_id)
    try:
        await message.remove_reaction(payload.emoji, user)
    except discord.HTTPException:
        pass


async def _handle_info(task, channel, reacted, payload) -> None:
    desc = task.get("description")
    if desc:
        try:
            await channel.send(
                f"ℹ️ **{task['brief']}**\n{desc}",
                reference=reacted,
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException:
            pass
    await _remove_user_reaction(reacted, payload)


# --- Snooze "numpad" panel -------------------------------------------------
# Tapping ⏩ posts a separate panel message that self-reacts with a number pad
# plus an hours/days unit toggle, so the task post itself stays clean. Picking a
# number snoozes the occurrence, edits the task post with the result + ↩️ undo,
# and deletes the panel. The panel is keyed in store["snooze_panels"] so it
# survives restarts.
SNOOZE_REACTIONS = (
    [DIGIT_EMOJI[n] for n in SNOOZE_CHOICES]
    + [EMOJI_SNOOZE_HOURS, EMOJI_SNOOZE_DAYS, EMOJI_DELETE]
)


def snooze_panel_text(brief: str, unit: str) -> str:
    other = "tap 📅 for days" if unit == "hours" else "tap ⏱️ for hours"
    return (
        f"💤 **Snooze {brief}** — pick a number of **{unit}** below "
        f"({other}, or ❌ to cancel)."
    )


def _take_task_panels(data: dict, tid: str) -> list[tuple[int, Optional[int]]]:
    """Pop (within a txn) every open snooze panel belonging to a task, returning
    (message_id, channel_id) pairs so the caller can delete the messages."""
    out: list[tuple[int, Optional[int]]] = []
    for pid, rec in list(data["snooze_panels"].items()):
        if rec.get("task_id") == tid:
            out.append((int(pid), rec.get("channel_id")))
            data["snooze_panels"].pop(pid, None)
    return out


async def _delete_panels(panels: list[tuple[int, Optional[int]]]) -> None:
    for mid, cid in panels:
        ch = bot.get_channel(int(cid)) if cid else None
        if ch is not None:
            await safe_delete(ch.get_partial_message(mid))


async def _handle_ffwd(tid, task, channel, reacted, payload) -> None:
    snap = await store.snapshot()
    live = snap["tasks"].get(tid)
    if not live or not live.get("pending"):
        await _remove_user_reaction(reacted, payload)
        return  # occurrence already resolved — nothing to snooze
    panel = await channel.send(
        snooze_panel_text(task["brief"], "hours"), allowed_mentions=NO_PINGS
    )
    async with store.txn() as data:
        data["snooze_panels"][str(panel.id)] = {
            "task_id": tid,
            "anchor_id": payload.message_id,
            "unit": "hours",
            "brief": task["brief"],
            "channel_id": getattr(channel, "id", None),
        }
    for emoji in SNOOZE_REACTIONS:
        try:
            await panel.add_reaction(emoji)
        except discord.HTTPException:
            pass
    await _remove_user_reaction(reacted, payload)


async def _handle_snooze_panel(
    payload: discord.RawReactionActionEvent, channel: discord.abc.Messageable
) -> None:
    key = emoji_key(payload.emoji)
    snap = await store.snapshot()
    rec = snap["snooze_panels"].get(str(payload.message_id))
    if not rec:
        return
    panel = channel.get_partial_message(payload.message_id)
    tid, brief = rec["task_id"], rec.get("brief", "")

    if key == emoji_key(EMOJI_DELETE):  # cancel the panel
        async with store.txn() as data:
            data["snooze_panels"].pop(str(payload.message_id), None)
        await safe_delete(panel)
        return

    if key in (emoji_key(EMOJI_SNOOZE_HOURS), emoji_key(EMOJI_SNOOZE_DAYS)):
        unit = "hours" if key == emoji_key(EMOJI_SNOOZE_HOURS) else "days"
        async with store.txn() as data:
            r = data["snooze_panels"].get(str(payload.message_id))
            if r:
                r["unit"] = unit
        try:
            await panel.edit(content=snooze_panel_text(brief, unit), allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass
        await _remove_user_reaction(panel, payload)
        return

    n = DIGIT_BY_KEY.get(key)
    if n is None:
        return  # an unrelated reaction on the panel

    unit = rec.get("unit", "hours")
    hours = n if unit == "hours" else n * 24
    member = payload.member
    mention = member.mention if member else f"<@{payload.user_id}>"
    anchor_id = rec.get("anchor_id")

    before = None
    remind = None
    async with store.txn() as data:
        data["snooze_panels"].pop(str(payload.message_id), None)
        live = data["tasks"].get(tid)
        p = live.get("pending") if live else None
        if p:
            before = json.loads(json.dumps(live))  # snapshot for undo
            p["ffwd_count"] = p.get("ffwd_count", 0) + 1
            remind = now_utc() + dt.timedelta(hours=hours)
            p["remind_at"] = to_iso(remind)
    await safe_delete(panel)
    if before is None or remind is None:
        return  # resolved between opening the panel and picking a number

    amount = f"{hours} hour{'' if hours == 1 else 's'}" if unit == "hours" \
        else f"{n} day{'' if n == 1 else 's'}"
    status = (
        f"**{brief}**\n"
        f"⏩ Snoozed {amount} by {mention} — next reminder {discord_ts(remind, 'R')}"
    )
    if anchor_id:
        try:
            await channel.get_partial_message(anchor_id).edit(
                content=status, allowed_mentions=NO_PINGS
            )
        except discord.HTTPException:
            pass
        await _arm_undo("snooze", tid, before, anchor_id, channel)


async def _handle_done(tid, task, cfg, tz, channel, payload, mention, display) -> None:
    completed = now_utc()
    record = None
    message_ids: list[int] = []
    before = None
    completion_id = None
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        p = live.get("pending") if live else None
        if not p:
            return
        before = json.loads(json.dumps(live))  # snapshot for undo
        completion_id = new_id()
        due = from_iso(p["due_at"])
        message_ids = list(p["message_ids"])
        for mid in message_ids:
            data["messages"].pop(str(mid), None)
        panels = _take_task_panels(data, tid)
        record = {
            "id": completion_id,  # lets an undo void exactly this log entry
            "ts": to_iso(completed),
            "month": completed.astimezone(tz).strftime("%Y-%m"),  # local-tz bucket
            "guild_id": task["guild_id"],
            "task_id": tid,
            "brief": task["brief"],
            "user_id": payload.user_id,
            "user_name": display,
            "kind": "recurring" if task["recurring"] else "once",
            "due_at": p["due_at"],
            "late_seconds": max(0, int((completed - due).total_seconds())),
        }
        if live["recurring"]:
            live["pending"] = None
            live["next_due"] = to_iso(next_due(recurrence_of(live), tz, due, completed))
        else:
            data["tasks"].pop(tid, None)

    await _delete_panels(panels)
    if record:
        await store.log_completion(record)
        status = (
            f"~~**{task['brief']}**~~\n"
            f"✅ Completed by {mention} • {discord_ts(completed, 't')}"
        )
        await finalize_messages(channel, message_ids, status)
        if message_ids:
            await _arm_undo("done", tid, before, message_ids[-1], channel, completion_id=completion_id)


async def _handle_skip_or_delete(tid, task, tz, channel, mention) -> None:
    message_ids: list[int] = []
    mode = None
    before = None
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        p = live.get("pending") if live else None
        if not p:
            return
        before = json.loads(json.dumps(live))  # snapshot for undo
        message_ids = list(p["message_ids"])
        for mid in message_ids:
            data["messages"].pop(str(mid), None)
        panels = _take_task_panels(data, tid)
        if live["recurring"]:
            due = from_iso(p["due_at"])
            live["pending"] = None
            live["next_due"] = to_iso(next_due(recurrence_of(live), tz, due, now_utc()))
            mode = "skip"
        else:
            data["tasks"].pop(tid, None)
            mode = "delete"

    await _delete_panels(panels)
    if mode == "skip":
        status = f"**{task['brief']}**\n⏭️ Skipped this time by {mention} — back next cycle."
    elif mode == "delete":
        status = f"~~**{task['brief']}**~~\n❌ Cancelled by {mention}."
    else:
        return
    await finalize_messages(channel, message_ids, status)
    if message_ids:
        await _arm_undo(mode, tid, before, message_ids[-1], channel)


# ---------------------------------------------------------------------------
# Undo (↩️)
# ---------------------------------------------------------------------------
def can_undo(action: str, before: dict, live: Optional[dict]) -> bool:
    """Is it still safe to restore ``before`` over the current ``live`` task?

    The guard exists so a stale ↩️ (e.g. tapped on yesterday's completed chore
    after today's occurrence has already fired) can't clobber newer state.

      * snooze            — the very same occurrence must still be pending
                            (matched by ``due_at``); refuse if it was resolved
                            or a different occurrence is now in flight.
      * done/skip (recurring) — refuse if a new occurrence is already pending,
                            or the task was deleted in the meantime.
      * done/delete (one-off) — the task was removed; only restore if nothing
                            has since taken its id.
    """
    if action == "snooze":
        lp = live.get("pending") if live else None
        bp = before.get("pending")
        return bool(lp and bp and lp.get("due_at") == bp.get("due_at"))
    if before.get("recurring"):
        return live is not None and live.get("pending") is None
    return live is None


async def _arm_undo(
    action: str,
    tid: str,
    before: Optional[dict],
    anchor_id: int,
    channel: discord.abc.Messageable,
    *,
    completion_id: Optional[str] = None,
) -> None:
    """Record how to reverse the action just taken and add the ↩️ button to the
    message that shows its result. Only the most recent action per task is kept
    undoable, so older ↩️ buttons for this task are retired."""
    if before is None:
        return
    stale: list[int] = []
    async with store.txn() as data:
        for mid, rec in list(data["undo"].items()):
            if rec.get("task_id") == tid and str(mid) != str(anchor_id):
                data["undo"].pop(mid, None)
                stale.append(int(mid))
        data["undo"][str(anchor_id)] = {
            "action": action,
            "task_id": tid,
            "before": before,
            "completion_id": completion_id,
            "channel_id": getattr(channel, "id", None),
        }
    try:
        await channel.get_partial_message(anchor_id).add_reaction(EMOJI_UNDO)
    except discord.HTTPException:
        pass
    if bot.user:  # tidy now-dead ↩️ buttons left on this task's older messages
        for mid in stale:
            try:
                await channel.get_partial_message(mid).remove_reaction(EMOJI_UNDO, bot.user)
            except discord.HTTPException:
                pass


async def _restore_anchor(
    channel: discord.abc.Messageable, msg_id: int, task: dict
) -> None:
    """Bring the resolved message back to a live, actionable post: restore the
    brief, drop the ↩️ button, and re-add the ✅/⏩/(ℹ️)/❌ reactions."""
    pm = channel.get_partial_message(msg_id)
    try:
        await pm.edit(content=post_content(task, reminder=False, cfg={}), allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass
    try:
        await pm.clear_reactions()  # needs Manage Messages; best effort
    except discord.HTTPException:
        pass
    if bot.user:  # ensure our ↩️ is gone even without Manage Messages
        try:
            await pm.remove_reaction(EMOJI_UNDO, bot.user)
        except discord.HTTPException:
            pass
    try:
        await add_task_reactions(pm, task)
    except discord.HTTPException:
        pass


async def _disarm_undo_button(channel: discord.abc.Messageable, msg_id: int) -> None:
    """A refused undo: remove the dead ↩️ and say (quietly) why nothing happened."""
    pm = channel.get_partial_message(msg_id)
    if bot.user:
        try:
            await pm.remove_reaction(EMOJI_UNDO, bot.user)
        except discord.HTTPException:
            pass
    try:
        await channel.send(
            "↩️ Too late to undo — this chore has already moved on.",
            reference=pm,
            allowed_mentions=NO_PINGS,
        )
    except discord.HTTPException:
        pass


async def _handle_undo(
    payload: discord.RawReactionActionEvent, channel: discord.abc.Messageable
) -> None:
    snap = await store.snapshot()
    if str(payload.message_id) not in snap["undo"]:
        return  # fast path: a ↩️ on something we don't track — ignore

    # Everything authoritative is read from inside the txn so a concurrent
    # re-arm (e.g. a second snooze on this message) can't make us act on stale
    # snapshot data.
    outcome = None  # "ok" | "refused"
    action = before = completion_id = None
    async with store.txn() as data:
        rec = data["undo"].get(str(payload.message_id))
        if not rec:
            return  # a concurrent ↩️ beat us to it
        action = rec["action"]
        before = rec["before"]
        completion_id = rec.get("completion_id")
        tid = rec["task_id"]
        if can_undo(action, before, data["tasks"].get(tid)):
            data["tasks"][tid] = json.loads(json.dumps(before))
            pending = before.get("pending") or {}
            for mid in pending.get("message_ids", []):
                data["messages"][str(mid)] = tid
            outcome = "ok"
        else:
            outcome = "refused"
        data["undo"].pop(str(payload.message_id), None)

    if outcome == "ok":
        if action == "done" and completion_id:
            await store.void_completion(completion_id)
        await _restore_anchor(channel, payload.message_id, before)
    elif outcome == "refused":
        await _disarm_undo_button(channel, payload.message_id)


# ---------------------------------------------------------------------------
# Scheduling from user input (shared by /newtask and /edittask)
# ---------------------------------------------------------------------------
def schedule_from_rule(
    rule: dict,
    at: Optional[str],
    tz: ZoneInfo,
    now: dt.datetime,
    *,
    at_given: bool,
    default_tod: Optional[str] = None,
) -> dict:
    """Turn a parsed ``repeat`` rule + an ``at`` string into concrete task
    schedule fields. Raises ``ValueError`` for an explicit past one-off time.

    ``at_given`` distinguishes "user typed an `at`" from "defaulted to now": a
    one-off that lands in the past is an error *only* when the user asked for a
    specific past time; an omitted/`now` `at` simply fires on the next tick.
    ``default_tod`` is the recurring time to keep when ``at`` isn't supplied
    (used by edits that change only the repeat rule).
    """
    if rule["freq"] == "monthly" and not rule["monthdays"]:
        rule["monthdays"] = [now.astimezone(tz).day]  # bare "monthly" → today's date

    if rule["freq"] == "once":
        due = resolve_when(at, tz, now)
        if due <= now:
            explicit = at_given and (at or "").strip().lower() not in ("", "now")
            if explicit:
                raise ValueError("that time is already in the past")
            due = now  # fire on the next scheduler tick
        return {
            "recurring": False, "freq": "once", "interval_days": 0,
            "weekdays": [], "monthdays": [], "time_of_day": None, "next_due": due,
        }

    tod = time_of_day_from(at, tz, now) if at_given else (default_tod or time_of_day_from(None, tz, now))
    rule["time_of_day"] = tod
    return {
        "recurring": True, "freq": rule["freq"], "interval_days": rule["interval_days"],
        "weekdays": rule["weekdays"], "monthdays": rule["monthdays"],
        "time_of_day": tod, "next_due": first_due(rule, tz, now),
    }


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="farmconfig", description="Set the channel, timezone, and optional reminder role")
@app_commands.describe(
    channel="Channel where tasks are posted",
    timezone="IANA timezone, e.g. Europe/Berlin (autocompletes)",
    reminder_role="Role to ping on overdue hourly reminders (optional)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def farmconfig(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    timezone: Optional[str] = None,
    reminder_role: Optional[discord.Role] = None,
) -> None:
    if timezone is not None:
        try:
            ZoneInfo(timezone)
        except Exception:
            await interaction.response.send_message(
                f"❌ Unknown timezone `{timezone}`. Use an IANA name like `Europe/Berlin`.",
                ephemeral=True,
            )
            return

    async with store.txn() as data:
        cfg = data["configs"].setdefault(
            str(interaction.guild_id),
            {"channel_id": None, "timezone": None, "reminder_role_id": None},
        )
        if channel is not None:
            cfg["channel_id"] = channel.id
        if timezone is not None:
            cfg["timezone"] = timezone
        if reminder_role is not None:
            cfg["reminder_role_id"] = reminder_role.id
        current = dict(cfg)

    ch = f"<#{current['channel_id']}>" if current.get("channel_id") else "— *(unset)*"
    tz = f"`{current['timezone']}`" if current.get("timezone") else "— *(unset)*"
    role = f"<@&{current['reminder_role_id']}>" if current.get("reminder_role_id") else "— *(none)*"
    msg = (
        "**Farm configuration**\n"
        f"• Channel: {ch}\n"
        f"• Timezone: {tz}\n"
        f"• Reminder role: {role}"
    )
    if not config_ready(current):
        msg += "\n\n⚠️ Set **both** a channel and a timezone before creating tasks."
    await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=NO_PINGS)


@farmconfig.autocomplete("timezone")
async def _tz_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    matches = [z for z in COMMON_TZS if cur in z.lower()][:25]
    return [app_commands.Choice(name=z, value=z) for z in matches]


# --- Autocomplete helpers (shared across commands) -------------------------
async def _guild_tz(interaction: discord.Interaction) -> ZoneInfo:
    """Best-effort timezone for live autocomplete previews."""
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    if cfg and cfg.get("timezone"):
        try:
            return ZoneInfo(cfg["timezone"])
        except Exception:
            pass
    return UTC


def _human_until(now: dt.datetime, due: dt.datetime) -> str:
    secs = (due - now).total_seconds()
    if -60 <= secs <= 60:
        return "now"
    past, secs = secs < 0, abs(secs)
    if secs < 3600:
        v, u = round(secs / 60), "min"
    elif secs < 86400:
        v = round(secs / 3600)
        u = "hour" if v == 1 else "hours"
    else:
        v = round(secs / 86400)
        u = "day" if v == 1 else "days"
    return f"{v} {u} ago" if past else f"in {v} {u}"


def _when_label(due: dt.datetime, tz: ZoneInfo, now: dt.datetime, *, head: bool = True) -> str:
    local = due.astimezone(tz)
    prefix = "📅 " if head else ""
    return f"{prefix}{local:%a %b %d · %H:%M} ({_human_until(now, due)})"


def _dedup(choices: list[app_commands.Choice]) -> list[app_commands.Choice]:
    seen, out = set(), []
    for c in choices:
        if c.value in seen:
            continue
        seen.add(c.value)
        out.append(c)
    return out[:25]


async def at_autocomplete(interaction: discord.Interaction, current: str):
    tz, now = await _guild_tz(interaction), now_utc()
    cur = current.strip()
    choices: list[app_commands.Choice] = []
    if cur:
        try:
            choices.append(app_commands.Choice(
                name=_when_label(resolve_when(cur, tz, now), tz, now)[:100], value=cur[:100]))
        except ValueError:
            choices.append(app_commands.Choice(
                name="⚠️ e.g. now · in 2h · 18:00 · tomorrow 8am · Jun 20 14:00", value=cur[:100]))
    for text in ("now", "in 1 hour", "in 30 minutes", "tonight", "tomorrow 08:00",
                 "this saturday 09:00", "next monday 18:00"):
        if cur and cur.lower() not in text.lower():
            continue
        try:
            due = resolve_when(text, tz, now)
        except ValueError:
            continue
        choices.append(app_commands.Choice(
            name=f"{text}  →  {_when_label(due, tz, now, head=False)}"[:100], value=text))
    return _dedup(choices)


def _repeat_label(rule: dict, now: dt.datetime) -> str:
    if rule["freq"] == "monthly" and not rule["monthdays"]:
        rule = {**rule, "monthdays": [now.day]}  # preview only; real day set at run time
    return "one-off" if rule["freq"] == "once" else describe_repeat(rule)


async def repeat_autocomplete(interaction: discord.Interaction, current: str):
    now = now_utc()
    cur = current.strip()
    choices: list[app_commands.Choice] = []
    if cur:
        try:
            choices.append(app_commands.Choice(
                name=f"🔁 {_repeat_label(parse_repeat(cur), now)}"[:100], value=cur[:100]))
        except ValueError:
            choices.append(app_commands.Choice(
                name="⚠️ e.g. daily · every 2 days · weekdays · mon,thu · monthly on the 1st",
                value=cur[:100]))
    for text in ("once", "daily", "every 2 days", "every 3 days", "weekdays", "weekends",
                 "mon,wed,fri", "every tuesday", "weekly", "monthly", "monthly on the 1st",
                 "monthly on the 1st,15th"):
        if cur and cur.lower() not in text.lower():
            continue
        try:
            choices.append(app_commands.Choice(
                name=f"{text}  →  {_repeat_label(parse_repeat(text), now)}"[:100], value=text))
        except ValueError:
            continue
    return _dedup(choices)


async def task_autocomplete(interaction: discord.Interaction, current: str):
    snap = await store.snapshot()
    cur = current.lower()
    out = []
    for tid, t in snap["tasks"].items():
        if str(t["guild_id"]) != str(interaction.guild_id):
            continue
        label = f"{t['brief']} ({schedule_label(t)})"
        if cur in label.lower() or cur in tid.lower():
            out.append(app_commands.Choice(name=label[:100], value=tid))
        if len(out) >= 25:
            break
    return out


@bot.tree.command(name="newtask", description="Create a one-off or recurring task")
@app_commands.describe(
    brief="Short text posted in the channel (required)",
    at="When/what time — now, in 2h, 18:00, tomorrow 8am, 2026-06-20 14:00 (default: now)",
    repeat="How often — once, daily, every 2 days, weekdays, mon/thu, monthly on the 1st (default: once)",
    description="Optional longer details, revealed by the ℹ️ reaction",
)
async def newtask(
    interaction: discord.Interaction,
    brief: app_commands.Range[str, 1, 200],
    at: Optional[str] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
) -> None:
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    if not config_ready(cfg):
        await interaction.response.send_message(
            "❌ Run `/farmconfig` to set a channel and timezone first.", ephemeral=True
        )
        return

    tz, now = ZoneInfo(cfg["timezone"]), now_utc()
    try:
        sched = schedule_from_rule(parse_repeat(repeat), at, tz, now, at_given=at is not None)
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/farmhelp` for the `at` and `repeat` formats.", ephemeral=True
        )
        return

    tid = new_id()
    task = {
        "id": tid,
        "guild_id": interaction.guild_id,
        "brief": str(brief),
        "description": description[:1500] if description else None,
        "recurring": sched["recurring"],
        "freq": sched["freq"],
        "interval_days": sched["interval_days"],
        "weekdays": sched["weekdays"],
        "monthdays": sched["monthdays"],
        "time_of_day": sched["time_of_day"],
        "next_due": to_iso(sched["next_due"]),
        "created_by": interaction.user.id,
        "created_at": to_iso(now),
        "pending": None,
    }
    async with store.txn() as data:
        data["tasks"][tid] = task

    fire = sched["next_due"]
    when = f"{discord_ts(fire, 'F')} ({discord_ts(fire, 'R')})"
    if sched["recurring"]:
        body = f"✅ Created **{brief}** — {schedule_label(task)}.\nFirst post: {when}"
    else:
        body = f"✅ Created one-off **{brief}**.\nDue: {when}"
    if description:
        body += "\nℹ️ Details attached."
    body += f"\n· `{tid}` — change it any time with `/edittask`"
    # Public on purpose: the family should see when a chore is added.
    await interaction.response.send_message(body, allowed_mentions=NO_PINGS)


def _find_task(snap: dict, guild_id: int, text: str) -> Optional[dict]:
    """Resolve a task by id (the autocomplete value) or, failing that, by an
    exact then substring match on its brief — so a pasted id *or* a typed name
    both work."""
    t = snap["tasks"].get(text)
    if t and str(t["guild_id"]) == str(guild_id):
        return t
    needle = (text or "").strip().lower()
    mine = [t for t in snap["tasks"].values() if str(t["guild_id"]) == str(guild_id)]
    for t in mine:
        if t["id"].lower() == needle or t["brief"].strip().lower() == needle:
            return t
    for t in mine:
        if needle and needle in t["brief"].lower():
            return t
    return None


@bot.tree.command(name="deletetask", description="Permanently delete a task (recurring or one-off)")
@app_commands.describe(task="Start typing to pick a task (or paste its id)")
async def deletetask(interaction: discord.Interaction, task: str) -> None:
    snap = await store.snapshot()
    found = _find_task(snap, interaction.guild_id, task)
    removed = None
    panels: list = []
    if found:
        async with store.txn() as data:
            tid = found["id"]
            t = data["tasks"].get(tid)
            if t and str(t["guild_id"]) == str(interaction.guild_id):
                pending = t.get("pending")
                if pending:
                    for mid in pending.get("message_ids", []):
                        data["messages"].pop(str(mid), None)
                for mid, rec in list(data["undo"].items()):
                    if rec.get("task_id") == tid:
                        data["undo"].pop(mid, None)
                panels = _take_task_panels(data, tid)
                removed = data["tasks"].pop(tid, None)
    await _delete_panels(panels)
    if removed:
        await interaction.response.send_message(
            f"🗑️ Deleted **{removed['brief']}**.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ Task not found. Use `/listtasks` to see current tasks.", ephemeral=True
        )


@bot.tree.command(name="edittask", description="Edit a task's text, time, or repeat (ids come from /listtasks)")
@app_commands.describe(
    task="The task to edit — pick from the list, or paste its id",
    brief="New short text (optional)",
    at="New time/date — now, in 2h, 18:00, tomorrow 8am, 2026-06-20 14:00 (optional)",
    repeat="New repeat — once, daily, every 2 days, weekdays, mon/thu, monthly on the 1st (optional)",
    description="New longer details (optional)",
    clear_description="Remove the existing long description",
)
async def edittask(
    interaction: discord.Interaction,
    task: str,
    brief: Optional[app_commands.Range[str, 1, 200]] = None,
    at: Optional[str] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
    clear_description: bool = False,
) -> None:
    snap = await store.snapshot()
    live = _find_task(snap, interaction.guild_id, task)
    if not live:
        await interaction.response.send_message(
            "❌ Task not found. Use `/listtasks` to see ids.", ephemeral=True
        )
        return
    tid = live["id"]

    if brief is None and at is None and repeat is None and description is None and not clear_description:
        await interaction.response.send_message(
            "❌ Nothing to change — set at least one of brief, at, repeat, or description.",
            ephemeral=True,
        )
        return

    cfg = guild_config(snap, interaction.guild_id)
    recompute = at is not None or repeat is not None
    if recompute and not config_ready(cfg):
        await interaction.response.send_message(
            "❌ Set a timezone with `/farmconfig` before changing the schedule.", ephemeral=True
        )
        return

    now = now_utc()
    sched = None
    if recompute:
        tz = ZoneInfo(cfg["timezone"])
        new_rule = parse_repeat(repeat) if repeat is not None else recurrence_of(live)
        try:
            sched = schedule_from_rule(
                new_rule, at, tz, now, at_given=at is not None, default_tod=live.get("time_of_day")
            )
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ {e}\nSee `/farmhelp` for the `at` and `repeat` formats.", ephemeral=True
            )
            return

    updated = None
    pending_note = False
    async with store.txn() as data:
        t = data["tasks"].get(tid)
        if t:
            if brief is not None:
                t["brief"] = str(brief)
            if clear_description:
                t["description"] = None
            elif description is not None:
                t["description"] = description[:1500]
            if sched is not None:
                t["recurring"] = sched["recurring"]
                t["freq"] = sched["freq"]
                t["interval_days"] = sched["interval_days"]
                t["weekdays"] = sched["weekdays"]
                t["monthdays"] = sched["monthdays"]
                t["time_of_day"] = sched["time_of_day"]
                if t.get("pending"):
                    pending_note = True  # don't disturb a live occurrence
                else:
                    t["next_due"] = to_iso(sched["next_due"])
            updated = json.loads(json.dumps(t))

    if not updated:
        await interaction.response.send_message("❌ Task not found.", ephemeral=True)
        return

    body = f"✏️ Updated **{updated['brief']}** — {schedule_label(updated)}."
    if pending_note:
        body += "\n(A reminder is live now; the new schedule applies from the next cycle.)"
    elif sched is not None and updated.get("next_due"):
        nd = from_iso(updated["next_due"])
        body += f"\nNext post: {discord_ts(nd, 'F')} ({discord_ts(nd, 'R')})"
    # Public on purpose: shared chores changing is something the family should see.
    await interaction.response.send_message(body, allowed_mentions=NO_PINGS)


# Register the shared autocompletes onto each command that needs them.
for _cmd in (newtask, edittask):
    _cmd.autocomplete("at")(at_autocomplete)
    _cmd.autocomplete("repeat")(repeat_autocomplete)
for _cmd in (deletetask, edittask):
    _cmd.autocomplete("task")(task_autocomplete)


@bot.tree.command(name="listtasks", description="List every task with its id, schedule, and next post")
async def listtasks(interaction: discord.Interaction) -> None:
    snap = await store.snapshot()
    mine = [t for t in snap["tasks"].values() if str(t["guild_id"]) == str(interaction.guild_id)]

    def sort_key(t: dict):
        if t.get("pending"):
            return (0, from_iso(t["pending"]["due_at"]))
        if t.get("next_due"):
            return (1, from_iso(t["next_due"]))
        return (2, now_utc())

    mine.sort(key=sort_key)

    rows = []
    for t in mine:
        if t.get("pending"):
            state = f"⏳ pending since {discord_ts(from_iso(t['pending']['due_at']), 'R')}"
        elif t.get("next_due"):
            state = f"next {discord_ts(from_iso(t['next_due']), 'R')}"
        else:
            state = "—"
        info = " ℹ️" if t.get("description") else ""
        rows.append(f"• `{t['id']}` **{t['brief']}**{info} — {schedule_label(t)} · {state}")

    if not rows:
        await interaction.response.send_message(
            "No tasks yet. Create one with `/newtask`.", ephemeral=True
        )
        return

    head = "**Farm tasks** — edit with `/edittask` using the `id`, remove with `/deletetask`\n"
    shown, total = [], len(head)
    for r in rows:
        if total + len(r) + 1 > 1900:  # stay under Discord's 2000-char limit
            break
        shown.append(r)
        total += len(r) + 1
    msg = head + "\n".join(shown)
    if len(shown) < len(rows):
        msg += f"\n… and {len(rows) - len(shown)} more."
    await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=NO_PINGS)


@bot.tree.command(name="farmhelp", description="How to use farmtracker — commands, scheduling, and reactions")
async def farmhelp(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🚜 farmtracker help",
        description=(
            "Create one-off or recurring chores. When one is due I post it in the "
            "farm channel and self-react with buttons the family taps."
        ),
        color=0x6B8E23,
    )
    embed.add_field(
        name="Commands",
        value=(
            "• `/newtask` — add a chore (see scheduling below)\n"
            "• `/listtasks` — list chores with their ids\n"
            "• `/edittask` — change a chore (paste its id from the list)\n"
            "• `/deletetask` — remove a chore for good\n"
            "• `/leaderboard` — monthly completion ranking 🏆\n"
            "• `/farmconfig` — channel, timezone, reminder role *(Manage Server)*\n"
            "• `/farmhelp` — this message"
        ),
        inline=False,
    )
    embed.add_field(
        name="`at` — when / what time (defaults to now)",
        value=(
            "`now` · `in 2h` · `+3d` · `tonight` · `18:00` · `6pm` · `tomorrow 8am` · "
            "`fri 19:00` · `next monday` · `Jun 20 14:00` · `2026-06-20 14:00`"
        ),
        inline=False,
    )
    embed.add_field(
        name="`repeat` — how often (defaults to once)",
        value=(
            "`once` · `daily` · `every 2 days` · `weekly` · `weekdays` · `weekends` · "
            "`mon,thu` · `every tuesday` · `monthly` · `monthly on the 1st` · `1st,15th`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Reactions on a posted chore",
        value=(
            "✅ **Done** — logs who did it (counts on the leaderboard)\n"
            "⏩ **Snooze** — opens a number-pad panel; pick hours or days\n"
            "ℹ️ **Info** — shows the longer description, if any\n"
            "❌ **Skip** — skips just this time (recurring) or cancels (one-off)\n"
            "↩️ **Undo** — appears after ✅/⏩/❌ to reverse it"
        ),
        inline=False,
    )
    embed.set_footer(
        text="e.g.  /newtask brief:Trash out at:19:00 repeat:mon,thu   ·   "
        "/newtask brief:Vet visit at:tomorrow 14:00"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="leaderboard", description="Monthly chore-completion leaderboard")
@app_commands.describe(month="Month as YYYY-MM (defaults to the current month)")
async def leaderboard(interaction: discord.Interaction, month: Optional[str] = None) -> None:
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    tz = ZoneInfo(cfg["timezone"]) if cfg and cfg.get("timezone") else UTC
    if month is None:
        month = now_utc().astimezone(tz).strftime("%Y-%m")

    counts: dict[int, int] = {}
    names: dict[int, str] = {}
    for rec in store.read_completions():
        if rec.get("guild_id") != interaction.guild_id:
            continue
        rec_month = rec.get("month") or str(rec.get("ts", ""))[:7]
        if rec_month != month:
            continue
        uid = rec["user_id"]
        counts[uid] = counts.get(uid, 0) + 1
        names[uid] = rec.get("user_name", str(uid))

    if not counts:
        await interaction.response.send_message(
            f"No chores logged for **{month}** yet. Get to work! 🚜", ephemeral=True
        )
        return

    ranking = sorted(counts.items(), key=lambda kv: (-kv[1], names[kv[0]].lower()))
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, count) in enumerate(ranking):
        badge = medals[i] if i < 3 else f"`{i + 1}.`"
        lines.append(f"{badge} <@{uid}> — **{count}**")
    total = sum(counts.values())
    msg = (
        f"🏆 **Chore leaderboard — {month}**\n"
        + "\n".join(lines)
        + f"\n\n_{total} chore{'s' if total != 1 else ''} completed this month._"
    )
    await interaction.response.send_message(msg, allowed_mentions=NO_PINGS)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the **Manage Server** permission to do that."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "❌ You don't have permission to use this command."
    else:
        log.exception("command error", exc_info=error)
        msg = "❌ Something went wrong handling that command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s)", bot.user, getattr(bot.user, "id", "?"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    from dotenv import load_dotenv

    load_dotenv(override=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
        )

    store.load()
    log.info("Loaded store from %s", store.path)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
