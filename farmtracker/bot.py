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
     🔄  requeue   -> appears on a ✅-completed post; re-fires the chore right
                      now (a fresh occurrence) without waiting for its next slot.

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

import asyncio
import datetime as dt
import itertools
import json
import logging
import os
import pathlib
import sys
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import trinkets
from .models import (
    DIGIT_BY_KEY,
    DIGIT_EMOJI,
    EMOJI_DELETE,
    EMOJI_DONE,
    EMOJI_END,
    EMOJI_FFWD,
    EMOJI_INFO,
    EMOJI_REQUEUE,
    EMOJI_SNOOZE_DAYS,
    EMOJI_SNOOZE_HOURS,
    EMOJI_UNDO,
    SNOOZE_CHOICES,
    UTC,
    describe_repeat,
    discord_ts,
    doemup_apply,
    emoji_key,
    first_due,
    from_iso,
    new_id,
    next_due,
    now_utc,
    parse_repeat,
    pitchin_add,
    pitchin_remove,
    recurrence_of,
    render_doemup,
    render_pitchin,
    resolve_when,
    time_of_day_from,
    to_iso,
)
from .store import Store

log = logging.getLogger("farmtracker")

DATA_DIR = pathlib.Path(os.getenv("FARMTRACKER_DATA_DIR", "data"))
store = Store(DATA_DIR / "store.json", DATA_DIR / "completions.jsonl")

# Repo root (the directory containing pyproject.toml), used by /redeploy to
# git-pull and re-exec the bot.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

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
        # Revive every do-em-up's ➕/➖/End buttons after a restart in one shot —
        # the do-em-up id rides in each button's custom_id (see DoEmUpButton).
        self.add_dynamic_items(DoEmUpButton)
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


def bounty_tag(task: dict) -> str:
    """A small inline marker shown on a bounty's posts (worth 2 pts, not the poster)."""
    return " 💰 *bounty · 2 pts*" if task.get("bounty") else ""


def post_content(task: dict, *, reminder: bool, cfg: dict) -> str:
    brief = task["brief"]
    tag = bounty_tag(task)
    if not reminder:
        return f"**{brief}**{tag}"
    role_id = cfg.get("reminder_role_id")
    prefix = f"<@&{role_id}> " if role_id else ""
    return f"{prefix}⏰ Still pending: **{brief}**{tag}"


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

    # Requeue (🔄) sits on a ✅-completed post, which — like an undone one — has
    # been de-registered from "messages", so it is keyed off its own table.
    if key == emoji_key(EMOJI_REQUEUE):
        await _handle_requeue(payload, channel)
        return

    snap = await store.snapshot()
    if str(payload.message_id) in snap["snooze_panels"]:
        await _handle_snooze_panel(payload, channel)
        return
    game = snap["game_messages"].get(str(payload.message_id))
    if game:
        if game["kind"] == "pitchin":
            await _handle_pitchin_reaction(payload, channel, game["id"])
        return  # do-em-ups resolve via buttons; ignore stray reactions on them
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


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    """Only meaningful for pitch-ins: pulling your ✅ before it closes drops you
    from the scorers (Discord sends no member object on removals, so we match by
    user id)."""
    if bot.user and payload.user_id == bot.user.id:
        return
    if payload.guild_id is None:
        return
    if emoji_key(payload.emoji) != emoji_key(EMOJI_DONE):
        return  # only a ✅ removal matters
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        return
    snap = await store.snapshot()
    game = snap["game_messages"].get(str(payload.message_id))
    if game and game["kind"] == "pitchin":
        await _handle_pitchin_unreact(payload, channel, game["id"])


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
    # A bounty is a chore the creator has put up for *someone else*: it's worth
    # double, and they can't claim it themselves.
    if task.get("bounty") and payload.user_id == task.get("created_by"):
        reacted = channel.get_partial_message(payload.message_id)
        await _remove_user_reaction(reacted, payload)
        try:
            await channel.send(
                f"💰 {mention}, this is **your** bounty — someone else has to claim it "
                "(it's worth 2 points!).",
                reference=reacted,
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException:
            pass
        return

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
            "points": 2 if task.get("bounty") else 1,
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
        bonus = " 💰 **+2 pts**" if task.get("bounty") else ""
        status = (
            f"~~**{task['brief']}**~~\n"
            f"✅ Completed by {mention}{bonus} • {discord_ts(completed, 't')}"
        )
        await finalize_messages(channel, message_ids, status)
        if message_ids:
            await _arm_undo("done", tid, before, message_ids[-1], channel, completion_id=completion_id)
            await _arm_requeue(tid, before, message_ids[-1], channel, task["guild_id"])


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
    if bot.user:  # ensure our ↩️/🔄 are gone even without Manage Messages
        for emoji in (EMOJI_UNDO, EMOJI_REQUEUE):
            try:
                await pm.remove_reaction(emoji, bot.user)
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
        # Undoing a ✅ turns its post back into a live occurrence, so any 🔄
        # requeue we armed on that same post no longer applies.
        data["requeue"].pop(str(payload.message_id), None)

    if outcome == "ok":
        if action == "done" and completion_id:
            await store.void_completion(completion_id)
        await _restore_anchor(channel, payload.message_id, before)
    elif outcome == "refused":
        await _disarm_undo_button(channel, payload.message_id)


# ---------------------------------------------------------------------------
# Requeue (🔄)
# ---------------------------------------------------------------------------
# A ✅-completed post keeps a 🔄 button so an occurrence can be re-run on the
# spot — e.g. you marked "water the animals" done, then notice the trough is dry
# again. Pressing it fires a *fresh* occurrence right now instead of waiting for
# the next scheduled slot; finishing that occurrence rolls the recurrence to its
# normal next slot exactly as usual (the schedule re-pins to time_of_day, so it
# doesn't drift). Like ↩️, only the most recent completed post per task carries a
# live 🔄, and the table survives restarts.
async def _arm_requeue(
    tid: str,
    before: Optional[dict],
    anchor_id: int,
    channel: discord.abc.Messageable,
    guild_id: int,
) -> None:
    """Add the 🔄 button to a just-completed post and remember how to re-fire the
    task from it, retiring any 🔄 left on this task's older completed posts."""
    if before is None:
        return
    stale: list[int] = []
    async with store.txn() as data:
        for mid, rec in list(data["requeue"].items()):
            if rec.get("task_id") == tid and str(mid) != str(anchor_id):
                data["requeue"].pop(mid, None)
                stale.append(int(mid))
        data["requeue"][str(anchor_id)] = {
            "task_id": tid,
            "before": before,  # lets a completed one-off be recreated and re-run
            "guild_id": guild_id,
            "channel_id": getattr(channel, "id", None),
        }
    try:
        await channel.get_partial_message(anchor_id).add_reaction(EMOJI_REQUEUE)
    except discord.HTTPException:
        pass
    if bot.user:  # tidy now-dead 🔄 buttons left on this task's older posts
        for mid in stale:
            try:
                await channel.get_partial_message(mid).remove_reaction(EMOJI_REQUEUE, bot.user)
            except discord.HTTPException:
                pass


async def _handle_requeue(
    payload: discord.RawReactionActionEvent, channel: discord.abc.Messageable
) -> None:
    snap = await store.snapshot()
    if str(payload.message_id) not in snap["requeue"]:
        return  # a 🔄 on something we don't track — ignore

    member = payload.member
    mention = member.mention if member else f"<@{payload.user_id}>"

    outcome = None  # "fired" | "busy" | "gone"
    tid = cfg = None
    async with store.txn() as data:
        rec = data["requeue"].get(str(payload.message_id))
        if not rec:
            return  # a concurrent 🔄 beat us to it
        tid = rec["task_id"]
        cfg = guild_config(snap, rec["guild_id"])
        if not config_ready(cfg):
            return
        live = data["tasks"].get(tid)
        if live is not None and live.get("pending"):
            outcome = "busy"  # an occurrence is already live — finish that one
        elif live is not None:
            live["next_due"] = to_iso(now_utc())  # fire on the spot below
            outcome = "fired"
        elif rec.get("before") is not None:
            # The task is gone (a completed one-off, or it was deleted): rebuild
            # it from the saved snapshot as a fresh, due-now occurrence.
            restored = json.loads(json.dumps(rec["before"]))
            restored["pending"] = None
            restored["next_due"] = to_iso(now_utc())
            data["tasks"][tid] = restored
            outcome = "fired"
        else:
            outcome = "gone"
        if outcome == "fired":
            # This completed post is spent; the fresh occurrence carries its own
            # buttons. Drop the record so a second tap can't double-fire.
            data["requeue"].pop(str(payload.message_id), None)

    pm = channel.get_partial_message(payload.message_id)
    if outcome == "fired":
        await fire_task(tid, channel, cfg)
        # Tidy the spent 🔄 off the old completed post (both ours and theirs) and
        # confirm right where they tapped — the fresh post may be far down.
        await _remove_user_reaction(pm, payload)
        if bot.user:
            try:
                await pm.remove_reaction(EMOJI_REQUEUE, bot.user)
            except discord.HTTPException:
                pass
        try:
            await channel.send(
                f"🔄 Re-queued by {mention} — posted it again below.",
                reference=pm,
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException:
            pass
    elif outcome == "busy":
        # Leave the button so they can requeue once the live one is resolved.
        await _remove_user_reaction(pm, payload)
        try:
            await channel.send(
                f"🔄 {mention}, that chore is already queued — finish the active "
                "reminder above first.",
                reference=pm,
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException:
            pass
    else:  # gone
        if bot.user:
            try:
                await pm.remove_reaction(EMOJI_REQUEUE, bot.user)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Pitch-ins and do-em-ups  (ad-hoc point events; see models.py for the schemas)
# ---------------------------------------------------------------------------
# Both are posted immediately by their slash command into the configured farm
# channel, resolve by people reacting (pitch-in ✅) or clicking buttons (do-em-up
# ➕/➖), and close at an expiry/deadline, a point cap, or the creator's manual
# end. Closing awards points to the same completion log as chores (with a
# ``points`` field), so one /leaderboard totals chores and games alike. The
# ``ended`` flag is flipped inside the same txn that pops the row, so an
# expiry-tick and a manual end racing each other can only award once.
def _game_tz(snap: dict, guild_id: int) -> ZoneInfo:
    cfg = guild_config(snap, guild_id)
    if cfg and cfg.get("timezone"):
        try:
            return ZoneInfo(cfg["timezone"])
        except Exception:
            pass
    return UTC


def _game_record(
    event: dict, kind: str, user_id: int, user_name: str, points: int,
    tz: ZoneInfo, now: dt.datetime,
) -> dict:
    """A completion-log row for one person's points from a pitch-in / do-em-up.
    Same shape as a chore completion (so /leaderboard reads them uniformly) with
    an added ``points`` count (chores omit it and are read as 1)."""
    return {
        "id": new_id(),
        "ts": to_iso(now),
        "month": now.astimezone(tz).strftime("%Y-%m"),  # local-tz bucket
        "guild_id": event["guild_id"],
        "task_id": event["id"],
        "brief": event["brief"],
        "user_id": user_id,
        "user_name": user_name,
        "kind": kind,  # "pitchin" | "doemup"
        "points": points,
        "due_at": to_iso(now),
        "late_seconds": 0,
    }


def game_records(event: dict, kind: str, tz: ZoneInfo, now: dt.datetime) -> list[dict]:
    """Every point-award row owed when an event closes: one per pitch-in scorer,
    or one per do-em-up tallier (worth their unit count × ``points_each``)."""
    pe = event.get("points_each", 1)
    recs: list[dict] = []
    if kind == "pitchin":
        for s in event.get("scorers", []):
            recs.append(_game_record(event, kind, s["user_id"], s["user_name"], pe, tz, now))
    else:  # doemup
        for key, e in event.get("tallies", {}).items():
            if e.get("count", 0) > 0:
                recs.append(_game_record(event, kind, int(key), e["name"], e["count"] * pe, tz, now))
    return recs


def _game_recurrence_fields(
    recurrence: Optional[dict], duration_secs: Optional[int]
) -> dict:
    """The recurrence columns shared by pitch-ins and do-em-ups. ``recurrence`` is
    a rule dict (carrying ``time_of_day``) for a repeating game, or None for a
    one-off. ``next_due`` starts None — it's only set later, while dormant between
    rounds."""
    if not recurrence:
        return {"recurring": False, "freq": "once", "interval_days": 0,
                "weekdays": [], "monthdays": [], "time_of_day": None,
                "next_due": None, "duration_secs": None}
    return {"recurring": True, "freq": recurrence["freq"],
            "interval_days": recurrence.get("interval_days", 0),
            "weekdays": recurrence.get("weekdays", []),
            "monthdays": recurrence.get("monthdays", []),
            "time_of_day": recurrence["time_of_day"],
            "next_due": None, "duration_secs": duration_secs}


async def _send_pitchin(channel: discord.abc.Messageable, p: dict) -> discord.Message:
    """Post a pitch-in's live body and self-react ✅ (pitch in) + 🏁 (end)."""
    msg = await channel.send(render_pitchin(p), allowed_mentions=NO_PINGS)
    try:
        await msg.add_reaction(EMOJI_DONE)
        await msg.add_reaction(EMOJI_END)
    except discord.HTTPException:
        pass
    return msg


async def _send_doemup(channel: discord.abc.Messageable, d: dict) -> discord.Message:
    """Post a do-em-up's live body with its ➕/➖/End buttons."""
    return await channel.send(
        render_doemup(d), view=make_doemup_view(d["id"]), allowed_mentions=NO_PINGS
    )


def _game_next_round(game: dict, tz: ZoneInfo, now: dt.datetime) -> dt.datetime:
    """When the next round of a recurring game should post: its next scheduled
    slot strictly after ``now``, anchored on the original creation time so the
    rounds stay phase-locked to the wall-clock they were started at."""
    return next_due(recurrence_of(game), tz, from_iso(game["created_at"]), now)


async def finalize_pitchin(pid: str, channel: discord.abc.Messageable) -> bool:
    """Close a pitch-in round: award every scorer, rewrite the post as a result
    line, and clear its reactions. A recurring pitch-in then rolls on — it goes
    dormant until its next slot (the whole series is torn down with
    ``/deletetask``). Returns False if it was already gone/closed."""
    snap = await store.snapshot()
    p0 = snap["pitchins"].get(pid)
    if not p0:
        return False
    tz, now = _game_tz(snap, p0["guild_id"]), now_utc()
    event = nxt = None
    async with store.txn() as data:
        p = data["pitchins"].get(pid)
        if not p or p.get("ended"):
            return False
        p["ended"] = True
        event = json.loads(json.dumps(p))
        data["game_messages"].pop(str(p.get("message_id")), None)
        if p.get("recurring"):
            nxt = _game_next_round(p, tz, now)
            p.update({"scorers": [], "ended": False, "message_id": None,
                      "expires_at": None, "next_due": to_iso(nxt)})
        else:
            data["pitchins"].pop(pid, None)
    for rec in game_records(event, "pitchin", tz, now):
        await store.log_completion(rec)
    body = render_pitchin(event, final=True)
    if nxt is not None:
        body += f"\n🔁 Next round {discord_ts(nxt, 'R')}."
    pm = channel.get_partial_message(event["message_id"])
    try:
        await pm.edit(content=body, allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass
    try:
        await pm.clear_reactions()
    except discord.HTTPException:
        pass
    return True


async def finalize_doemup(did: str, channel: discord.abc.Messageable) -> bool:
    """Close a do-em-up round: award each tallier their unit count × points_each,
    rewrite the post as a result line, and drop its buttons. A recurring do-em-up
    then rolls on — it goes dormant until its next slot (the whole series is torn
    down with ``/deletetask``)."""
    snap = await store.snapshot()
    d0 = snap["doemups"].get(did)
    if not d0:
        return False
    tz, now = _game_tz(snap, d0["guild_id"]), now_utc()
    event = nxt = None
    async with store.txn() as data:
        d = data["doemups"].get(did)
        if not d or d.get("ended"):
            return False
        d["ended"] = True
        event = json.loads(json.dumps(d))
        data["game_messages"].pop(str(d.get("message_id")), None)
        if d.get("recurring"):
            nxt = _game_next_round(d, tz, now)
            d.update({"tallies": {}, "ended": False, "message_id": None,
                      "deadline": None, "next_due": to_iso(nxt)})
        else:
            data["doemups"].pop(did, None)
    for rec in game_records(event, "doemup", tz, now):
        await store.log_completion(rec)
    body = render_doemup(event, final=True)
    if nxt is not None:
        body += f"\n🔁 Next round {discord_ts(nxt, 'R')}."
    try:
        await channel.get_partial_message(event["message_id"]).edit(
            content=body, view=None, allowed_mentions=NO_PINGS
        )
    except discord.HTTPException:
        pass
    return True


async def repost_pitchin(pid: str, channel: discord.abc.Messageable) -> None:
    """Open a fresh round of a dormant recurring pitch-in (its previous round has
    closed and ``next_due`` has come due)."""
    snap = await store.snapshot()
    p0 = snap["pitchins"].get(pid)
    if not p0 or p0.get("ended") or p0.get("message_id") or not p0.get("next_due"):
        return  # not actually dormant
    tz, now = _game_tz(snap, p0["guild_id"]), now_utc()
    dur = p0.get("duration_secs")
    exp = (now + dt.timedelta(seconds=int(dur))) if dur else _game_next_round(p0, tz, now)
    p_live = {**p0, "scorers": [], "ended": False,
              "expires_at": to_iso(exp), "next_due": None}
    msg = await _send_pitchin(channel, p_live)
    orphan = False
    async with store.txn() as data:
        p = data["pitchins"].get(pid)
        if not p or p.get("ended") or p.get("message_id") or not p.get("next_due"):
            orphan = True  # something raced us
        else:
            p.update({"message_id": msg.id, "scorers": [],
                      "expires_at": to_iso(exp), "next_due": None})
            data["game_messages"][str(msg.id)] = {"kind": "pitchin", "id": pid}
    if orphan:
        await safe_delete(msg)


async def repost_doemup(did: str, channel: discord.abc.Messageable) -> None:
    """Open a fresh round of a dormant recurring do-em-up."""
    snap = await store.snapshot()
    d0 = snap["doemups"].get(did)
    if not d0 or d0.get("ended") or d0.get("message_id") or not d0.get("next_due"):
        return
    tz, now = _game_tz(snap, d0["guild_id"]), now_utc()
    dur = d0.get("duration_secs")
    if dur:
        deadline_iso = to_iso(now + dt.timedelta(seconds=int(dur)))
    elif d0.get("recurring"):  # no fixed window -> run this round to the next slot
        deadline_iso = to_iso(_game_next_round(d0, tz, now))
    else:  # a deferred one-off with no deadline opens and stays open until 🏁
        deadline_iso = None
    d_live = {**d0, "tallies": {}, "ended": False,
              "deadline": deadline_iso, "next_due": None}
    msg = await _send_doemup(channel, d_live)
    orphan = False
    async with store.txn() as data:
        d = data["doemups"].get(did)
        if not d or d.get("ended") or d.get("message_id") or not d.get("next_due"):
            orphan = True
        else:
            d.update({"message_id": msg.id, "tallies": {},
                      "deadline": deadline_iso, "next_due": None})
            data["game_messages"][str(msg.id)] = {"kind": "doemup", "id": did}
    if orphan:
        await safe_delete(msg)


async def post_pitchin(
    channel: discord.abc.Messageable, *, guild_id: int, creator_id: int, brief: str,
    description: Optional[str], expires_at: str, points_each: int,
    max_scorers: Optional[int], now: dt.datetime,
    recurrence: Optional[dict] = None, duration_secs: Optional[int] = None,
) -> tuple[str, discord.Message]:
    pid = new_id()
    p = {
        "id": pid, "guild_id": guild_id, "channel_id": getattr(channel, "id", None),
        "message_id": None, "brief": brief, "description": description,
        "created_by": creator_id, "created_at": to_iso(now),
        "points_each": points_each, "max_scorers": max_scorers,
        "expires_at": expires_at, "scorers": [], "ended": False,
        **_game_recurrence_fields(recurrence, duration_secs),
    }
    msg = await _send_pitchin(channel, p)
    async with store.txn() as data:
        p["message_id"] = msg.id
        data["pitchins"][pid] = p
        data["game_messages"][str(msg.id)] = {"kind": "pitchin", "id": pid}
    return pid, msg


async def schedule_pitchin(
    *, guild_id: int, creator_id: int, channel_id: int, brief: str,
    description: Optional[str], points_each: int, max_scorers: Optional[int],
    now: dt.datetime, recurrence: Optional[dict], duration_secs: Optional[int],
    starts_at: dt.datetime,
) -> str:
    """Create a pitch-in whose first round is deferred: it sits dormant (no post)
    until the scheduler opens it at ``starts_at``. Used when ``/pitchin`` is given
    an ``at:`` — a recurring round then fires at its wall-clock slot, and a one-off
    can be scheduled for later — instead of posting the instant it's created."""
    pid = new_id()
    p = {
        "id": pid, "guild_id": guild_id, "channel_id": channel_id,
        "message_id": None, "brief": brief, "description": description,
        "created_by": creator_id, "created_at": to_iso(now),
        "points_each": points_each, "max_scorers": max_scorers,
        "expires_at": None, "scorers": [], "ended": False,
        **_game_recurrence_fields(recurrence, duration_secs),
        # The recurrence helper zeroes next_due/duration for the live-now path;
        # a deferred round needs its slot, and a one-off needs its open span kept.
        "next_due": to_iso(starts_at), "duration_secs": duration_secs,
    }
    async with store.txn() as data:
        data["pitchins"][pid] = p
    return pid


async def post_doemup(
    channel: discord.abc.Messageable, *, guild_id: int, creator_id: int, brief: str,
    description: Optional[str], points_each: int, deadline: Optional[str],
    point_limit: Optional[int], now: dt.datetime,
    recurrence: Optional[dict] = None, duration_secs: Optional[int] = None,
) -> tuple[str, discord.Message]:
    did = new_id()
    d = {
        "id": did, "guild_id": guild_id, "channel_id": getattr(channel, "id", None),
        "message_id": None, "brief": brief, "description": description,
        "created_by": creator_id, "created_at": to_iso(now),
        "points_each": points_each, "deadline": deadline, "point_limit": point_limit,
        "tallies": {}, "ended": False,
        **_game_recurrence_fields(recurrence, duration_secs),
    }
    msg = await _send_doemup(channel, d)
    async with store.txn() as data:
        d["message_id"] = msg.id
        data["doemups"][did] = d
        data["game_messages"][str(msg.id)] = {"kind": "doemup", "id": did}
    return did, msg


async def schedule_doemup(
    *, guild_id: int, creator_id: int, channel_id: int, brief: str,
    description: Optional[str], points_each: int, point_limit: Optional[int],
    now: dt.datetime, recurrence: Optional[dict], duration_secs: Optional[int],
    starts_at: dt.datetime,
) -> str:
    """Create a do-em-up whose first round is deferred to ``starts_at`` — the
    do-em-up analogue of :func:`schedule_pitchin`. It sits dormant (no post) until
    the scheduler opens it; a deferred one-off with no window then runs until 🏁."""
    did = new_id()
    d = {
        "id": did, "guild_id": guild_id, "channel_id": channel_id,
        "message_id": None, "brief": brief, "description": description,
        "created_by": creator_id, "created_at": to_iso(now),
        "points_each": points_each, "deadline": None, "point_limit": point_limit,
        "tallies": {}, "ended": False,
        **_game_recurrence_fields(recurrence, duration_secs),
        # The recurrence helper zeroes next_due/duration for the live-now path;
        # a deferred round needs its slot, and a windowed one needs its span kept.
        "next_due": to_iso(starts_at), "duration_secs": duration_secs,
    }
    async with store.txn() as data:
        data["doemups"][did] = d
    return did


async def _handle_pitchin_reaction(
    payload: discord.RawReactionActionEvent, channel: discord.abc.Messageable, pid: str
) -> None:
    """A ✅ (pitch in) or 🏁 (creator: end now) on a pitch-in post."""
    key = emoji_key(payload.emoji)
    reacted = channel.get_partial_message(payload.message_id)

    if key == emoji_key(EMOJI_END):
        snap = await store.snapshot()
        p = snap["pitchins"].get(pid)
        if not p or p.get("ended"):
            return
        if payload.user_id == p["created_by"]:
            # 🏁 closes this round early (awarding whoever's in). A recurring
            # pitch-in rolls on to its next slot; stop the series with /deletetask.
            await finalize_pitchin(pid, channel)
        else:
            await _remove_user_reaction(reacted, payload)  # only the creator ends it
        return

    if key != emoji_key(EMOJI_DONE):
        return  # any other reaction on the post — ignore

    member = payload.member
    name = member.display_name if member else str(payload.user_id)
    body = None
    do_finalize = False
    async with store.txn() as data:
        p = data["pitchins"].get(pid)
        if not p or p.get("ended"):
            return
        res = pitchin_add(p, payload.user_id, name)
        if res["changed"]:
            if res["full"]:
                do_finalize = True  # cap reached — close it now
            else:
                body = render_pitchin(p)
    if do_finalize:
        await finalize_pitchin(pid, channel)
    elif body is not None:
        try:
            await reacted.edit(content=body, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass


async def _handle_pitchin_unreact(
    payload: discord.RawReactionActionEvent, channel: discord.abc.Messageable, pid: str
) -> None:
    """A ✅ pulled off a still-open pitch-in drops that person before it closes."""
    body = None
    async with store.txn() as data:
        p = data["pitchins"].get(pid)
        if not p or p.get("ended"):
            return
        if pitchin_remove(p, payload.user_id):
            body = render_pitchin(p)
    if body is not None:
        try:
            await channel.get_partial_message(payload.message_id).edit(
                content=body, allowed_mentions=NO_PINGS
            )
        except discord.HTTPException:
            pass


async def _doemup_press(did: str, action: str, user_id: int, user_name: str) -> dict:
    """Apply a do-em-up button press inside a txn and report what to do next:
    ``status`` ∈ {gone, error, changed, final} (+ the new ``body`` when changed).
    ``final`` means the caller should run :func:`finalize_doemup`."""
    out = {"status": "gone", "body": None}
    async with store.txn() as data:
        d = data["doemups"].get(did)
        if not d or d.get("ended"):
            return out
        res = doemup_apply(d, action, user_id, user_name)
        if res["error"] == "not_creator":
            out["status"] = "error"
        elif res["final"]:
            out["status"] = "final"  # ➕ hit the cap, or the creator tapped End
        else:
            out["status"] = "changed"
            out["body"] = render_doemup(d)
    return out


async def handle_doemup_button(
    did: str, action: str, interaction: discord.Interaction
) -> None:
    res = await _doemup_press(did, action, interaction.user.id, interaction.user.display_name)
    status = res["status"]
    if status == "gone":
        await interaction.response.send_message(
            "That do-em-up has already closed.", ephemeral=True
        )
    elif status == "error":
        await interaction.response.send_message(
            "Only the person who started this do-em-up can end it.", ephemeral=True
        )
    elif status == "final":
        await interaction.response.defer()  # finalize edits the post itself
        # End and hitting the point_limit both just close this round; a recurring
        # do-em-up rolls on to its next slot (stop the series with /deletetask).
        await finalize_doemup(did, interaction.channel)
    else:  # changed — update the live tally in place
        await interaction.response.edit_message(content=res["body"], view=make_doemup_view(did))


class DoEmUpButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"doemup:(?P<action>plus|minus|end):(?P<did>[0-9a-f]+)",
):
    """A persistent ➕ / ➖ / 🏁End button. The do-em-up id rides in the
    custom_id, so a single ``add_dynamic_items`` registration on startup revives
    every do-em-up's buttons after a restart with no per-message bookkeeping."""

    LABELS = {"plus": "➕", "minus": "➖", "end": f"{EMOJI_END} End"}
    STYLES = {
        "plus": discord.ButtonStyle.success,
        "minus": discord.ButtonStyle.secondary,
        "end": discord.ButtonStyle.danger,
    }

    def __init__(self, did: str, action: str) -> None:
        self.did = did
        self.action = action
        super().__init__(
            discord.ui.Button(
                label=self.LABELS[action],
                style=self.STYLES[action],
                custom_id=f"doemup:{action}:{did}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # noqa: ANN001
        return cls(match["did"], match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_doemup_button(self.did, self.action, interaction)


def make_doemup_view(did: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DoEmUpButton(did, "plus"))
    view.add_item(DoEmUpButton(did, "minus"))
    view.add_item(DoEmUpButton(did, "end"))
    return view


async def sweep_games(now: dt.datetime, snap: dict) -> None:
    """Drive each game's clock from the scheduler tick: close a live round past
    its expiry/deadline, and re-post a dormant recurring round once its next slot
    comes due. Running off the tick means anything that fell due while the bot was
    down fires on the next tick — exactly like chore reminders."""
    for pid, p in list(snap.get("pitchins", {}).items()):
        try:
            if p.get("ended"):
                continue
            ch = bot.get_channel(int(p["channel_id"])) if p.get("channel_id") else None
            if ch is None:
                continue
            if p.get("next_due"):  # dormant between rounds -> re-post when due
                if now >= from_iso(p["next_due"]):
                    await repost_pitchin(pid, ch)
            elif p.get("expires_at") and now >= from_iso(p["expires_at"]):
                await finalize_pitchin(pid, ch)
        except Exception:
            log.exception("scheduler error on pitchin %s", pid)
    for did, d in list(snap.get("doemups", {}).items()):
        try:
            if d.get("ended"):
                continue
            ch = bot.get_channel(int(d["channel_id"])) if d.get("channel_id") else None
            if ch is None:
                continue
            if d.get("next_due"):  # dormant between rounds -> re-post when due
                if now >= from_iso(d["next_due"]):
                    await repost_doemup(did, ch)
            else:
                dl = d.get("deadline")
                if dl and now >= from_iso(dl):
                    await finalize_doemup(did, ch)
        except Exception:
            log.exception("scheduler error on doemup %s", did)


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
    item_bar="Points per trinket each month — every multiple earns another (default 25)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def farmconfig(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    timezone: Optional[str] = None,
    reminder_role: Optional[discord.Role] = None,
    item_bar: Optional[int] = None,
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

    if item_bar is not None and item_bar < 1:
        await interaction.response.send_message(
            "❌ The trinket bar must be at least 1 point.", ephemeral=True
        )
        return

    async with store.txn() as data:
        cfg = data["configs"].setdefault(
            str(interaction.guild_id),
            {"channel_id": None, "timezone": None, "reminder_role_id": None,
             "item_bar": trinkets.DEFAULT_BAR},
        )
        cfg.setdefault("item_bar", trinkets.DEFAULT_BAR)
        if channel is not None:
            cfg["channel_id"] = channel.id
        if timezone is not None:
            cfg["timezone"] = timezone
        if reminder_role is not None:
            cfg["reminder_role_id"] = reminder_role.id
        if item_bar is not None:
            cfg["item_bar"] = item_bar
        current = dict(cfg)

    ch = f"<#{current['channel_id']}>" if current.get("channel_id") else "— *(unset)*"
    tz = f"`{current['timezone']}`" if current.get("timezone") else "— *(unset)*"
    role = f"<@&{current['reminder_role_id']}>" if current.get("reminder_role_id") else "— *(none)*"
    bar = current.get("item_bar") or trinkets.DEFAULT_BAR
    msg = (
        "**Farm configuration**\n"
        f"• Channel: {ch}\n"
        f"• Timezone: {tz}\n"
        f"• Reminder role: {role}\n"
        f"• Trinket bar: **{bar} pts** each — every multiple earns another 🖼️"
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
    bounty="Worth 2 points, and only someone other than you can complete it (default: off)",
)
async def newtask(
    interaction: discord.Interaction,
    brief: app_commands.Range[str, 1, 200],
    at: Optional[str] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
    bounty: bool = False,
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
        "bounty": bool(bounty),
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
    if bounty:
        body += "\n💰 **Bounty** — worth 2 points; anyone *but* you can complete it."
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


def _find_game(snap: dict, guild_id: int, text: str) -> tuple[Optional[str], Optional[dict]]:
    """Resolve a pitch-in or do-em-up the same way :func:`_find_task` resolves a
    task — by id, then exact, then substring brief. Returns ``(kind, game)`` with
    ``kind`` ∈ {"pitchin", "doemup"}, or ``(None, None)``."""
    for kind, section in (("pitchin", "pitchins"), ("doemup", "doemups")):
        g = snap[section].get(text)
        if g and str(g["guild_id"]) == str(guild_id):
            return kind, g
    needle = (text or "").strip().lower()
    games = [("pitchin", g) for g in snap["pitchins"].values()
             if str(g["guild_id"]) == str(guild_id)]
    games += [("doemup", g) for g in snap["doemups"].values()
              if str(g["guild_id"]) == str(guild_id)]
    for matcher in (
        lambda g: g["id"].lower() == needle or g["brief"].strip().lower() == needle,
        lambda g: bool(needle) and needle in g["brief"].lower(),
    ):
        for kind, g in games:
            if matcher(g):
                return kind, g
    return None, None


async def _cancel_game_message(
    channel: discord.abc.Messageable, brief: str, mid: int, *, is_doemup: bool
) -> None:
    """Make a deleted game's live post inert: strike it through as cancelled and
    strip its reactions/buttons. No points are awarded (delete ≠ close)."""
    pm = channel.get_partial_message(mid)
    try:
        await pm.edit(content=f"🗑️ ~~**{brief}**~~ — cancelled.",
                      view=None, allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass
    if not is_doemup:  # do-em-ups carry buttons (cleared by view=None); pitch-ins, reactions
        try:
            await pm.clear_reactions()
        except discord.HTTPException:
            pass


@bot.tree.command(
    name="deletetask",
    description="Permanently delete a task, pitch-in, or do-em-up (recurring or one-off)",
)
@app_commands.describe(task="Start typing to pick a task or game (or paste its id)")
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
                for mid, rec in list(data["requeue"].items()):
                    if rec.get("task_id") == tid:
                        data["requeue"].pop(mid, None)
                panels = _take_task_panels(data, tid)
                removed = data["tasks"].pop(tid, None)
        await _delete_panels(panels)
    else:
        # Not a task — it may be a pitch-in or do-em-up (kills the whole series).
        kind, game = _find_game(snap, interaction.guild_id, task)
        if game:
            section = "pitchins" if kind == "pitchin" else "doemups"
            live_mid = None
            async with store.txn() as data:
                g = data[section].get(game["id"])
                if g and str(g["guild_id"]) == str(interaction.guild_id):
                    live_mid = g.get("message_id")
                    data["game_messages"].pop(str(live_mid), None)
                    removed = data[section].pop(game["id"], None)
            if removed and live_mid:
                ch = (bot.get_channel(int(removed["channel_id"]))
                      if removed.get("channel_id") else None)
                if ch is not None:
                    await _cancel_game_message(
                        ch, removed["brief"], live_mid, is_doemup=(kind == "doemup")
                    )
    if removed:
        await interaction.response.send_message(
            f"🗑️ Deleted **{removed['brief']}**.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ Not found. Use `/listtasks` to see current tasks.", ephemeral=True
        )


@bot.tree.command(name="edittask", description="Edit a task's text, time, or repeat (ids come from /listtasks)")
@app_commands.describe(
    task="The task to edit — pick from the list, or paste its id",
    brief="New short text (optional)",
    at="New time/date — now, in 2h, 18:00, tomorrow 8am, 2026-06-20 14:00 (optional)",
    repeat="New repeat — once, daily, every 2 days, weekdays, mon/thu, monthly on the 1st (optional)",
    description="New longer details (optional)",
    clear_description="Remove the existing long description",
    bounty="Make this a 2-point bounty the creator can't complete (or turn it off)",
)
async def edittask(
    interaction: discord.Interaction,
    task: str,
    brief: Optional[app_commands.Range[str, 1, 200]] = None,
    at: Optional[str] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
    clear_description: bool = False,
    bounty: Optional[bool] = None,
) -> None:
    snap = await store.snapshot()
    live = _find_task(snap, interaction.guild_id, task)
    if not live:
        await interaction.response.send_message(
            "❌ Task not found. Use `/listtasks` to see ids.", ephemeral=True
        )
        return
    tid = live["id"]

    if (brief is None and at is None and repeat is None and description is None
            and not clear_description and bounty is None):
        await interaction.response.send_message(
            "❌ Nothing to change — set at least one of brief, at, repeat, description, or bounty.",
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
            if bounty is not None:
                t["bounty"] = bool(bounty)
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
    if bounty is not None:
        body += (
            "\n💰 Now a **bounty** — worth 2 points; you can't complete it yourself."
            if bounty else "\n💰 Bounty removed — back to a normal 1-point chore."
        )
    # Public on purpose: shared chores changing is something the family should see.
    await interaction.response.send_message(body, allowed_mentions=NO_PINGS)


async def delete_autocomplete(interaction: discord.Interaction, current: str):
    """Like :func:`task_autocomplete`, but also lists active pitch-ins / do-em-ups
    so ``/deletetask`` can tear those series down too."""
    out = await task_autocomplete(interaction, current)
    cur = current.lower()
    snap = await store.snapshot()
    for icon, section in (("🤝", "pitchins"), ("💪", "doemups")):
        for gid, g in snap[section].items():
            if len(out) >= 25:
                break
            if str(g["guild_id"]) != str(interaction.guild_id):
                continue
            sched = describe_repeat(recurrence_of(g)) if g.get("recurring") else "one-off"
            label = f"{icon} {g['brief']} ({sched})"
            if cur in label.lower() or cur in gid.lower():
                out.append(app_commands.Choice(name=label[:100], value=gid))
    return out[:25]


# Register the shared autocompletes onto each command that needs them.
for _cmd in (newtask, edittask):
    _cmd.autocomplete("at")(at_autocomplete)
    _cmd.autocomplete("repeat")(repeat_autocomplete)
edittask.autocomplete("task")(task_autocomplete)
deletetask.autocomplete("task")(delete_autocomplete)


def _game_recurrence_from(
    repeat: Optional[str], tz: ZoneInfo, now: dt.datetime, at: Optional[str] = None
) -> Optional[dict]:
    """Parse a game's ``repeat`` into a recurrence rule, or None for a one-off.
    Mirrors :func:`schedule_from_rule`: a bare ``monthly`` takes today's date, and
    every round fires at ``at`` (the wall-clock time the game is started at when
    ``at`` is omitted)."""
    rule = parse_repeat(repeat)  # raises ValueError on junk
    if rule["freq"] == "once":
        return None
    if rule["freq"] == "monthly" and not rule["monthdays"]:
        rule["monthdays"] = [now.astimezone(tz).day]
    rule["time_of_day"] = time_of_day_from(at, tz, now)
    return rule


@bot.tree.command(
    name="pitchin",
    description="Start a pitch-in: everyone who ✅s before it closes earns a point",
)
@app_commands.describe(
    brief="What to pitch in on, e.g. 'laundry bonanza' (required)",
    at="When the first round opens — now, 06:00, tomorrow 8am. Recurring? sets the daily slot (default: now)",
    expires="When a round closes — in 5m, tonight, 18:00, tomorrow 8am (default: in 24h; recurring: at the next slot)",
    points="Points each pitcher-inner earns (default: 1)",
    max_scorers="Optional cap: only the first N to pitch in score",
    repeat="Repeat it — daily, weekdays, mon/thu, monthly on the 1st (default: once)",
    description="Optional extra details shown on the post",
)
async def pitchin(
    interaction: discord.Interaction,
    brief: app_commands.Range[str, 1, 200],
    at: Optional[str] = None,
    expires: Optional[str] = None,
    points: app_commands.Range[int, 1, 100] = 1,
    max_scorers: Optional[app_commands.Range[int, 1, 100]] = None,
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
        recurrence = _game_recurrence_from(repeat, tz, now, at)
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/farmhelp` for the `repeat` formats.", ephemeral=True
        )
        return
    # With `at`, the first round is deferred to its scheduled slot (a recurring
    # one fires at that wall-clock time rather than whenever it was created, and a
    # one-off can be set for later); without it, the round opens right now.
    deferred = bool(at)
    duration_secs = None
    try:
        if deferred:
            start = first_due(recurrence, tz, now) if recurrence else resolve_when(at, tz, now)
            if start <= now:
                raise ValueError("that start time is already in the past")
        else:
            start = now
        if expires:
            exp = resolve_when(expires, tz, start)  # clock times land on the slot's day
        elif recurrence:  # no window given -> the round runs until the next slot
            exp = next_due(recurrence, tz, start, start)
        else:
            exp = start + dt.timedelta(hours=24)
        if exp <= start:
            raise ValueError("that close time is already in the past")
        # A repeating round, or any deferred round, stores its open span as a
        # duration the scheduler reuses each time it (re)opens the post.
        if recurrence:
            if expires:  # an explicit window each round repeats
                duration_secs = max(1, int((exp - start).total_seconds()))
        elif deferred:  # a one-off scheduled for later keeps its open span too
            duration_secs = max(1, int((exp - start).total_seconds()))
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/farmhelp` for the time formats.", ephemeral=True
        )
        return
    channel = bot.get_channel(int(cfg["channel_id"]))
    if channel is None:
        await interaction.response.send_message(
            "❌ I can't see the configured channel — check `/farmconfig`.", ephemeral=True
        )
        return

    if deferred:
        await schedule_pitchin(
            guild_id=interaction.guild_id, creator_id=interaction.user.id,
            channel_id=channel.id, brief=str(brief),
            description=(description[:1000] if description else None),
            points_each=int(points),
            max_scorers=(int(max_scorers) if max_scorers else None), now=now,
            recurrence=recurrence, duration_secs=duration_secs, starts_at=start,
        )
    else:
        await post_pitchin(
            channel, guild_id=interaction.guild_id, creator_id=interaction.user.id,
            brief=str(brief), description=(description[:1000] if description else None),
            expires_at=to_iso(exp), points_each=int(points),
            max_scorers=(int(max_scorers) if max_scorers else None), now=now,
            recurrence=recurrence, duration_secs=duration_secs,
        )
    cap = f" · first {max_scorers} score" if max_scorers else ""
    rep = f" · 🔁 {describe_repeat(recurrence)} (`/deletetask` to stop it)" if recurrence else ""
    if deferred:
        verb, when = "Scheduled", f"first round opens {discord_ts(start, 'R')}"
    elif recurrence:
        verb, when = "Posted", f"first round closes {discord_ts(exp, 'R')}"
    else:
        verb, when = "Posted", f"closes {discord_ts(exp, 'R')}"
    await interaction.response.send_message(
        f"🤝 {verb} **{brief}** in <#{cfg['channel_id']}> — {when}{cap}{rep}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="doemup",
    description="Start a do-em-up: tap ➕ for each one you do; points tally live",
)
@app_commands.describe(
    brief="What's being done one-at-a-time, e.g. 'thistle bush removed' (required)",
    at="When the first round opens — now, 06:00, tomorrow 8am. Recurring? sets the daily slot (default: now)",
    points="Points per ➕ (default: 1)",
    deadline="Optional auto-close time — tonight, in 3h, tomorrow 18:00",
    point_limit="Optional cap: auto-close once this many points are tallied",
    repeat="Repeat it — daily, weekdays, mon/thu, monthly on the 1st (default: once)",
    description="Optional extra details shown on the post",
)
async def doemup(
    interaction: discord.Interaction,
    brief: app_commands.Range[str, 1, 200],
    at: Optional[str] = None,
    points: app_commands.Range[int, 1, 100] = 1,
    deadline: Optional[str] = None,
    point_limit: Optional[app_commands.Range[int, 1, 100000]] = None,
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
        recurrence = _game_recurrence_from(repeat, tz, now, at)
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/farmhelp` for the `repeat` formats.", ephemeral=True
        )
        return
    # With `at`, the first round is deferred to its scheduled slot (see /pitchin);
    # without it, the round opens right now.
    deferred = bool(at)
    deadline_iso, duration_secs = None, None
    try:
        if deferred:
            start = first_due(recurrence, tz, now) if recurrence else resolve_when(at, tz, now)
            if start <= now:
                raise ValueError("that start time is already in the past")
        else:
            start = now
        if deadline:
            dl = resolve_when(deadline, tz, start)  # clock times land on the slot's day
            if dl <= start:
                raise ValueError("that deadline is already in the past")
            deadline_iso = to_iso(dl)
            # A repeating round, or any deferred round, stores its open span as a
            # duration the scheduler reuses each time it (re)opens the post.
            if recurrence or deferred:
                duration_secs = max(1, int((dl - start).total_seconds()))
        elif recurrence:  # recurring needs a close: run each round to the next slot
            deadline_iso = to_iso(next_due(recurrence, tz, start, start))
        # else: a plain one-off do-em-up stays open until 🏁 (even when deferred)
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/farmhelp` for the time formats.", ephemeral=True
        )
        return
    channel = bot.get_channel(int(cfg["channel_id"]))
    if channel is None:
        await interaction.response.send_message(
            "❌ I can't see the configured channel — check `/farmconfig`.", ephemeral=True
        )
        return

    if deferred:
        await schedule_doemup(
            guild_id=interaction.guild_id, creator_id=interaction.user.id,
            channel_id=channel.id, brief=str(brief),
            description=(description[:1000] if description else None),
            points_each=int(points),
            point_limit=(int(point_limit) if point_limit else None), now=now,
            recurrence=recurrence, duration_secs=duration_secs, starts_at=start,
        )
        opens = f" — first round opens {discord_ts(start, 'R')}"
        rep = f" · 🔁 {describe_repeat(recurrence)} (`/deletetask` to stop it)" if recurrence else ""
        await interaction.response.send_message(
            f"💪 Scheduled **{brief}** in <#{cfg['channel_id']}>{opens}{rep}.",
            ephemeral=True,
        )
        return

    await post_doemup(
        channel, guild_id=interaction.guild_id, creator_id=interaction.user.id,
        brief=str(brief), description=(description[:1000] if description else None),
        points_each=int(points), deadline=deadline_iso,
        point_limit=(int(point_limit) if point_limit else None), now=now,
        recurrence=recurrence, duration_secs=duration_secs,
    )
    if recurrence:
        closes = (
            f" — first round closes {discord_ts(from_iso(deadline_iso), 'R')}"
            f" · 🔁 {describe_repeat(recurrence)} (`/deletetask` to stop it)"
        )
    else:
        closes = f" — closes {discord_ts(from_iso(deadline_iso), 'R')}" if deadline_iso else ""
    await interaction.response.send_message(
        f"💪 Posted **{brief}** in <#{cfg['channel_id']}> — tap ➕ as you go{closes}.",
        ephemeral=True,
    )


# The friendly "when"/"repeat" autocompletes (live previews of the resolved
# instant / rule) are the same ones /newtask uses for `at` and `repeat`.
pitchin.autocomplete("at")(at_autocomplete)
pitchin.autocomplete("expires")(at_autocomplete)
pitchin.autocomplete("repeat")(repeat_autocomplete)
doemup.autocomplete("at")(at_autocomplete)
doemup.autocomplete("deadline")(at_autocomplete)
doemup.autocomplete("repeat")(repeat_autocomplete)


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
        flag = " 💰" if t.get("bounty") else ""
        rows.append(f"• `{t['id']}` **{t['brief']}**{info}{flag} — {schedule_label(t)} · {state}")

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
            "• `/newtask` — add a chore (see scheduling below; `bounty:true` for a 2-pointer)\n"
            "• `/pitchin` — group task: everyone who ✅s before it closes scores\n"
            "• `/doemup` — per-unit task: tap ➕ for each one you do\n"
            "• `/listtasks` — list chores with their ids\n"
            "• `/edittask` — change a chore (paste its id from the list)\n"
            "• `/deletetask` — remove a chore for good\n"
            "• `/leaderboard` — monthly points ranking & ⭐ stars 🏆\n"
            "• `/vitrine` — your cabinet of month's-end trinkets 🖼️\n"
            "• `/farmconfig` — channel, timezone, reminder role, trinket bar *(Manage Server)*\n"
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
            "↩️ **Undo** — appears after ✅/⏩/❌ to reverse it\n"
            "🔄 **Requeue** — appears on a completed chore; re-posts it right now"
        ),
        inline=False,
    )
    embed.add_field(
        name="💰 Bounties & ⭐ stars",
        value=(
            "Mark a chore you can't do yourself with `bounty:true`: it's worth "
            "**2 points** and only **someone else** can tap ✅ on it. Every completed "
            "chore is a point (bounties two); whoever leads the month's `/leaderboard` "
            "earns a permanent **⭐ star** shown there for keeps."
        ),
        inline=False,
    )
    embed.add_field(
        name="Pitch-ins & do-em-ups (bonus points 🏆)",
        value=(
            "• `/pitchin brief:\"laundry bonanza\"` — everyone who taps ✅ before it "
            "closes earns a point. Optional `expires` (default 24h), `points` each, "
            "and `max_scorers` (only the first N score). 🏁 ends it early.\n"
            "• Add `repeat:` to either (same as a chore — `daily`, `weekdays`, "
            "`mon,thu`, `monthly on the 1st`) and it re-posts a fresh round each "
            "slot. 🏁 just closes the current round (it rolls on); stop the whole "
            "series with `/deletetask`.\n"
            "• Add `at:` to either to set the slot — e.g. `/pitchin … at:06:00 "
            "expires:06:05 repeat:daily` opens 06:00–06:05 every day. The first round "
            "waits for that time instead of posting the moment you create it.\n"
            "• `/doemup brief:\"thistle bush removed\"` — tap ➕ once per one you did "
            "(➖ to fix); the tally updates live. Optional `points` each, `deadline`, "
            "and `point_limit` (auto-closes at that total). 🏁 ends it.\n"
            "Points from both feed the `/leaderboard`."
        ),
        inline=False,
    )
    embed.add_field(
        name="🖼️ Trinkets & the vitrine",
        value=(
            "Clear the month's **bar** of points (default **25**, set with "
            "`/farmconfig item_bar:`) and when the month closes an inert **trinket** "
            "— a rolled *objet d'art* — lands in your `/vitrine`; clear it several "
            "times over (50 pts on a 25-pt bar) and you collect that many. Each "
            "month a different **zone** is *in season* (the Bean Zone, the Vaults, the "
            "Menagerie…), shown on the `/leaderboard`: ~7 in 10 of your trinkets are "
            "rolled from it, the rest stray in from other zones. Trinkets cost no "
            "points and do nothing but delight; the ⭐ star still goes to the top scorer."
        ),
        inline=False,
    )
    embed.set_footer(
        text="e.g.  /newtask brief:Trash out at:19:00 repeat:mon,thu   ·   "
        "/pitchin brief:Laundry bonanza expires:tonight"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Leaderboard scoring — points (bounties count double) and monthly ⭐ stars
# ---------------------------------------------------------------------------
def _completion_points(rec: dict) -> int:
    """Points a logged completion is worth. Bounties record ``points: 2``; older
    records predate the field and count as the normal 1 point."""
    p = rec.get("points")
    return int(p) if isinstance(p, (int, float)) and p > 0 else 1


def _rec_month(rec: dict) -> str:
    """The local-tz 'YYYY-MM' bucket a completion belongs to (tolerating very old
    records that only carry a 'ts')."""
    return rec.get("month") or str(rec.get("ts", ""))[:7]


def monthly_scores(records: list[dict], guild_id: int) -> dict[str, dict[int, dict]]:
    """Aggregate one guild's completions into
    ``{month: {user_id: {"points", "chores", "name"}}}``."""
    months: dict[str, dict[int, dict]] = {}
    for rec in records:
        if rec.get("guild_id") != guild_id:
            continue
        bucket = months.setdefault(_rec_month(rec), {})
        ent = bucket.setdefault(
            rec["user_id"], {"points": 0, "chores": 0, "name": str(rec["user_id"])}
        )
        ent["points"] += _completion_points(rec)
        ent["chores"] += 1
        ent["name"] = rec.get("user_name", ent["name"])
    return months


def star_counts(records: list[dict], guild_id: int, current_month: str) -> dict[int, int]:
    """Stars per user: one for each *past* month they led on points (a tie shares
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
    """The guild's trinket bar (monthly points to earn one), defaulted & sane."""
    try:
        return max(1, int(cfg.get("item_bar")))  # type: ignore[union-attr]
    except (TypeError, ValueError, AttributeError):
        return trinkets.DEFAULT_BAR


def vitrine_for(records: list[dict], guild_id: int, user_id: int, bar: int,
                current_month: str) -> list[dict]:
    """Every trinket a user has earned: one deterministic roll per *whole multiple*
    of ``bar`` their points reached, for each *past* month (50 pts against a
    25-point bar → two). Like stars, it's derived from the log — the current
    month is still in play, so it's excluded. Sorted oldest→newest, idx 0…n−1
    within a month."""
    out: list[dict] = []
    for month, bucket in sorted(monthly_scores(records, guild_id).items()):
        if not month or month >= current_month:
            continue
        ent = bucket.get(user_id)
        if not ent:
            continue
        for idx in range(ent["points"] // bar):  # bar ≥ 1, guaranteed by _guild_bar
            out.append(trinkets.roll_for(guild_id, user_id, month, idx))
    return out


@bot.tree.command(name="leaderboard", description="Monthly chore points & ⭐ stars")
@app_commands.describe(month="Month as YYYY-MM (defaults to the current month)")
async def leaderboard(interaction: discord.Interaction, month: Optional[str] = None) -> None:
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    tz = ZoneInfo(cfg["timezone"]) if cfg and cfg.get("timezone") else UTC
    current_month = now_utc().astimezone(tz).strftime("%Y-%m")
    bar = _guild_bar(cfg)
    if month is None:
        month = current_month

    records = store.read_completions()
    months = monthly_scores(records, interaction.guild_id)
    stars = star_counts(records, interaction.guild_id, current_month)

    # All-time display names so a star holder shows even when idle this month.
    names = {uid: ent["name"] for bucket in months.values() for uid, ent in bucket.items()}
    star_line = ""
    if stars:
        holders = sorted(stars.items(), key=lambda kv: (-kv[1], names.get(kv[0], "").lower()))
        star_line = "⭐ **Stars** — " + " · ".join(f"<@{uid}> ×{n}" for uid, n in holders)

    bucket = months.get(month, {})
    if not bucket:
        msg = (f"No chores logged for **{month}** yet. Get to work! 🚜\n"
               + trinkets.zone_blurb(month, bar, past=month < current_month))
        if star_line:
            msg += "\n\n" + star_line
        await interaction.response.send_message(
            msg, ephemeral=True, allowed_mentions=NO_PINGS
        )
        return

    ranking = sorted(bucket.items(), key=lambda kv: (-kv[1]["points"], kv[1]["name"].lower()))
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, ent) in enumerate(ranking):
        badge = medals[i] if i < 3 else f"`{i + 1}.`"
        star = f" ⭐×{stars[uid]}" if stars.get(uid) else ""
        pts = ent["points"]
        lines.append(f"{badge} <@{uid}> — **{pts} pt{'' if pts == 1 else 's'}**{star}")

    total_pts = sum(ent["points"] for ent in bucket.values())
    total_chores = sum(ent["chores"] for ent in bucket.values())
    when = "this month" if month == current_month else f"in {month}"
    footer = (
        f"_{total_chores} chore{'' if total_chores == 1 else 's'} · "
        f"{total_pts} pt{'' if total_pts == 1 else 's'} {when}._"
    )
    if month == current_month:
        footer += "\n⭐ Whoever tops the board when the month ends earns a star."

    zone_note = trinkets.zone_blurb(month, bar, past=month < current_month)
    msg = f"🏆 **Chore leaderboard — {month}**\n{zone_note}\n" + "\n".join(lines)
    if star_line:
        msg += "\n\n" + star_line
    msg += "\n\n" + footer
    await interaction.response.send_message(msg, allowed_mentions=NO_PINGS)


@bot.tree.command(name="vitrine", description="Gaze upon a collection of trinkets won at month's end")
@app_commands.describe(user="Whose vitrine to view (default: yours)")
async def vitrine(interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
    target = user or interaction.user
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    tz = ZoneInfo(cfg["timezone"]) if cfg and cfg.get("timezone") else UTC
    current_month = now_utc().astimezone(tz).strftime("%Y-%m")
    bar = _guild_bar(cfg)

    records = store.read_completions()
    items = vitrine_for(records, interaction.guild_id, target.id, bar, current_month)

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
    to_next = bar - pts % bar  # 1…bar: points until the next trinket tips over
    zk = trinkets.zone_for_month(current_month)
    z = f"{trinkets.zone_emoji(zk)} {current_month}: **{trinkets.zone_label(zk)}** in season"
    if secured == 0:
        foot = f"{z} — **{pts}/{bar} pts**, {to_next} to go for your first trinket"
    else:
        foot = (f"{z} — at **{pts} pts** you've secured "
                f"**{secured} trinket{'' if secured == 1 else 's'}** ✨, "
                f"**{to_next}** more for the next")

    msg = header + "\n" + "\n".join(body) + "\n\n" + foot
    await interaction.response.send_message(msg, allowed_mentions=NO_PINGS)


# ---------------------------------------------------------------------------
# /redeploy — owner-only: git pull, sync deps, and restart in place
# ---------------------------------------------------------------------------
# The Discord-triggered twin of ./redeploy.sh: pull the latest code, sync deps,
# report the result, then re-exec this process (os.execv) so it restarts in the
# *same* tmux pane — the log just continues, no scrollback lost. Running under
# ./run.sh isn't required, but is recommended so a crash on the new code
# auto-restarts instead of leaving the bot down.
_redeploy_lock = asyncio.Lock()


def _owner_ids_from_env() -> set[int]:
    raw = os.getenv("OWNER_IDS") or os.getenv("OWNER_ID") or ""
    return {int(p) for p in raw.replace(";", ",").split(",") if p.strip().isdigit()}


async def _is_bot_owner(interaction: discord.Interaction) -> bool:
    """True for the application owner (or any id listed in OWNER_IDS)."""
    return interaction.user.id in _owner_ids_from_env() or await bot.is_owner(
        interaction.user
    )


def _clip(text: str, limit: int = 1500) -> str:
    text = (text or "").strip() or "(no output)"
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _run(cmd: list[str], timeout: float = 180.0) -> tuple[int, str]:
    """Run a command in the repo root; return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=REPO_ROOT,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"`{' '.join(cmd)}` timed out after {int(timeout)}s"
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


@bot.tree.command(
    name="redeploy",
    description="(owner only) git pull, sync deps, and restart the bot",
)
@app_commands.describe(sync_deps="Run `uv sync` after pulling (default: on)")
@app_commands.check(_is_bot_owner)
async def redeploy(interaction: discord.Interaction, sync_deps: bool = True) -> None:
    if _redeploy_lock.locked():
        await interaction.response.send_message(
            "⏳ A redeploy is already in progress.", ephemeral=True
        )
        return

    async with _redeploy_lock:
        await interaction.response.defer(ephemeral=True)

        rc, pull_out = await _run(["git", "pull", "--ff-only"])
        if rc != 0:
            await interaction.followup.send(
                f"❌ `git pull` failed — **not** restarting:\n```\n{_clip(pull_out)}\n```",
                ephemeral=True,
            )
            return

        if sync_deps:
            rc, sync_out = await _run(["uv", "sync"])
            if rc != 0:
                await interaction.followup.send(
                    "⚠️ Pulled OK but `uv sync` failed — **not** restarting:\n"
                    f"```\n{_clip(sync_out)}\n```",
                    ephemeral=True,
                )
                return

        await interaction.followup.send(
            "✅ Pulled & synced — **restarting now**, back in a few seconds.\n"
            f"```\n{_clip(pull_out, 600)}\n```",
            ephemeral=True,
        )
        log.warning(
            "Redeploy requested by %s (id=%s) — re-execing",
            interaction.user,
            interaction.user.id,
        )

        # Replace this process image in place: same PID, same tmux pane, so the
        # log continues uninterrupted. The reply above is already awaited (hence
        # delivered) before the gateway socket drops on exec. No await between
        # here and execv, so the task can't be cancelled mid-restart.
        os.chdir(REPO_ROOT)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, "-m", "farmtracker"])


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
