from __future__ import annotations

import datetime as dt
import json
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from ..models import (
    DIGIT_BY_KEY,
    DIGIT_EMOJI,
    EMOJI_CLAP,
    EMOJI_DELETE,
    EMOJI_DONE,
    EMOJI_FFWD,
    EMOJI_INFO,
    EMOJI_REQUEUE,
    EMOJI_SHUSH,
    EMOJI_SKIP,
    EMOJI_SNOOZE_DAYS,
    EMOJI_SNOOZE_HOURS,
    EMOJI_UNDO,
    EMOJI_UNSHUSH,
    SNOOZE_CHOICES,
    discord_ts,
    emoji_key,
    from_iso,
    new_id,
    next_due,
    now_utc,
    recurrence_of,
    to_iso,
)
from .core import (
    NO_PINGS,
    bot,
    store,
)
from .helpers import (
    _remove_user_reaction,
    add_task_reactions,
    config_ready,
    finalize_messages,
    guild_config,
    post_content,
    safe_delete,
)
from .claps import (
    _arm_clap,
    _handle_clap,
)
from .games import (
    _handle_pitchin_reaction,
    _handle_pitchin_unreact,
)
from .scheduler import fire_task



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

    # Clap (👏) likewise lives on a ✅-completed (de-registered) post and is keyed
    # off its own table — a non-participant's tap tips the doer a bonus punto.
    if key == emoji_key(EMOJI_CLAP):
        await _handle_clap(payload, channel)
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
    elif key in (emoji_key(EMOJI_SKIP), emoji_key(EMOJI_DELETE)):
        await _handle_skip_or_delete(tid, task, tz, channel, mention)
    elif key in (emoji_key(EMOJI_SHUSH), emoji_key(EMOJI_UNSHUSH)):
        await _handle_shush(
            tid, task, channel, reacted, payload, mention,
            shush=key == emoji_key(EMOJI_SHUSH),
        )


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


async def _handle_shush(tid, task, channel, reacted, payload, mention, *, shush: bool) -> None:
    """🤫 sets the task's lifetime no-nag flag, 🔊 clears it: a shushed chore
    still fires on schedule, but the hourly reminders stop until someone taps
    🔊 on a live post. Nag posts self-react 🤫 and a shushed chore's posts
    self-react 🔊; either emoji added manually on any message of a live
    occurrence routes here too. A tap that matches the current state (🤫 on an
    already-shushed chore, or vice versa) is a no-op."""
    changed = False
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        if live is not None and bool(live.get("no_nag", False)) != shush:
            changed = True
            live["no_nag"] = shush
            p = live.get("pending")
            if not shush and p:
                # Un-shushing: remind_at is stale (often long past), so restart
                # the hourly cadence fresh instead of nagging on the next tick.
                p["remind_at"] = to_iso(now_utc() + dt.timedelta(hours=1))
    await _remove_user_reaction(reacted, payload)
    if not changed:
        return  # task vanished under us, or already in the asked-for state
    # Flip our button on the tapped post so it always shows the *next* action
    # (🤫 to shush, 🔊 to un-shush) — the two directions never share a face.
    old, new = (EMOJI_SHUSH, EMOJI_UNSHUSH) if shush else (EMOJI_UNSHUSH, EMOJI_SHUSH)
    if bot.user:
        try:
            await reacted.remove_reaction(old, bot.user)
        except discord.HTTPException:
            pass
    try:
        await reacted.add_reaction(new)
    except discord.HTTPException:
        pass
    # No separate confirmation post: stamp the tapped post itself with who
    # flipped the switch, replacing any earlier stamp so repeated toggles on
    # one post never stack up.
    if shush:
        line = f"{EMOJI_SHUSH} Shushed by {mention} — reminders off."
    else:
        line = f"{EMOJI_UNSHUSH} Un-shushed by {mention} — reminders back on."
    stamps = (f"{EMOJI_SHUSH} Shushed by ", f"{EMOJI_UNSHUSH} Un-shushed by ")
    try:
        content = (await reacted.fetch()).content or ""
        kept = [ln for ln in content.split("\n") if ln and not ln.startswith(stamps)]
        await reacted.edit(content="\n".join(kept + [line]), allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass


async def _handle_done(tid, task, cfg, tz, channel, payload, mention, display) -> None:
    # A bounty is a chore the creator has put up for *someone else*: it's worth
    # double, and they can't claim it themselves.
    if task.get("bounty") and payload.user_id == task.get("created_by"):
        reacted = channel.get_partial_message(payload.message_id)
        await _remove_user_reaction(reacted, payload)
        try:
            await channel.send(
                f"💰 {mention}, this is **your** bounty — someone else has to claim it "
                "(it's worth 2 puntos!).",
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
        bonus = " 💰 **+2 puntos**" if task.get("bounty") else ""
        status = (
            f"~~**{task['brief']}**~~\n"
            f"✅ Completed by {mention}{bonus} • {discord_ts(completed, 't')}"
        )
        await finalize_messages(channel, message_ids, status)
        if message_ids:
            await _arm_undo("done", tid, before, message_ids[-1], channel, completion_id=completion_id)
            await _arm_requeue(tid, before, message_ids[-1], channel, task["guild_id"])
            # The doer is this occurrence's sole participant; anyone *else* can 👏
            # them a bonus punto on the finished post.
            participants = [{"user_id": payload.user_id, "user_name": display}]
            await _arm_clap(
                tid, message_ids[-1], channel, task["guild_id"], task["brief"], status, participants
            )


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
    if bot.user:  # ensure our ↩️/🔄/👏 are gone even without Manage Messages
        for emoji in (EMOJI_UNDO, EMOJI_REQUEUE, EMOJI_CLAP):
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
    clap_log_ids: list[str] = []
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
        # Undoing a ✅ turns its post back into a live occurrence, so the 🔄
        # requeue and 👏 claps we armed on that same post no longer apply. Any
        # bonus puntos the claps already paid are retracted below — but only when
        # the undo actually takes (a refused ↩️ leaves the completion, and its
        # claps, standing).
        data["requeue"].pop(str(payload.message_id), None)
        clap = data["claps"].pop(str(payload.message_id), None)
        if clap:
            clap_log_ids = list(clap.get("log_ids", []))

    if outcome == "ok":
        if action == "done" and completion_id:
            await store.void_completion(completion_id)
        for lid in clap_log_ids:  # retract every bonus punto the claps awarded
            await store.void_completion(lid)
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
#
# A fired 🔄 also appends a zero-punto marker row (kind "requeue", points 0) to
# the completion log — worth nothing anywhere in the economy, but it's what the
# "The Reanimator" title badge is derived from, same recompute-from-log spirit
# as every other badge. Markers are never voided: undoing the fresh occurrence
# doesn't un-happen the tap.
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
    display = member.display_name if member else str(payload.user_id)

    outcome = None  # "fired" | "busy" | "gone"
    tid = cfg = brief = None
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
            brief = live.get("brief")
            outcome = "fired"
        elif rec.get("before") is not None:
            # The task is gone (a completed one-off, or it was deleted): rebuild
            # it from the saved snapshot as a fresh, due-now occurrence.
            restored = json.loads(json.dumps(rec["before"]))
            restored["pending"] = None
            restored["next_due"] = to_iso(now_utc())
            data["tasks"][tid] = restored
            brief = restored.get("brief")
            outcome = "fired"
        else:
            outcome = "gone"
        if outcome == "fired":
            # This completed post is spent; the fresh occurrence carries its own
            # buttons. Drop the record so a second tap can't double-fire.
            data["requeue"].pop(str(payload.message_id), None)

    pm = channel.get_partial_message(payload.message_id)
    if outcome == "fired":
        # The tap itself is on the record: a zero-punto marker row, outside the
        # txn (log_completion takes the same lock). Scoring skips these rows
        # everywhere except The Reanimator tally.
        fired_at = now_utc()
        tz = ZoneInfo(cfg["timezone"])
        await store.log_completion({
            "id": new_id(),
            "ts": to_iso(fired_at),
            "month": fired_at.astimezone(tz).strftime("%Y-%m"),  # local-tz bucket
            "guild_id": rec["guild_id"],
            "task_id": tid,
            "brief": brief,
            "user_id": payload.user_id,
            "user_name": display,
            "kind": "requeue",
            "points": 0,
        })
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


__all__ = [
    "SNOOZE_REACTIONS",
    "_arm_requeue",
    "_arm_undo",
    "_delete_panels",
    "_disarm_undo_button",
    "_handle_done",
    "_handle_ffwd",
    "_handle_info",
    "_handle_requeue",
    "_handle_shush",
    "_handle_skip_or_delete",
    "_handle_snooze_panel",
    "_handle_undo",
    "_restore_anchor",
    "_take_task_panels",
    "can_undo",
    "on_raw_reaction_add",
    "on_raw_reaction_remove",
    "snooze_panel_text",
]
