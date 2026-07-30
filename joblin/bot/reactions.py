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
    EMOJI_LIST,
    EMOJI_REQUEUE,
    EMOJI_SHUSH,
    EMOJI_SKIP,
    EMOJI_SNOOZE_DAYS,
    EMOJI_SNOOZE_HOURS,
    EMOJI_UNDO,
    EMOJI_UNSHUSH,
    SNOOZE_CHOICES,
    _join,
    discord_ts,
    emoji_key,
    from_iso,
    new_id,
    next_due,
    now_utc,
    recurrence_of,
    render_coward,
    to_iso,
)
from .core import (
    NO_PINGS,
    bot,
    store,
)
from .helpers import (
    PostButton,
    Press,
    _remove_user_reaction,
    _tidy_stale,
    completed_view,
    config_ready,
    finalize_messages,
    guild_config,
    make_task_view,
    post_content,
    refresh_post_view,
    safe_delete,
)
from .claps import (
    _arm_clap_in,
    _handle_clap,
)
from .games import (
    _handle_pitchin_reaction,
    _handle_pitchin_unreact,
)
from .scheduler import fire_task



# ---------------------------------------------------------------------------
# Reactions (the legacy entry way — posts made before the button migration
# carry self-reacted emoji, and any manually-added emoji still routes here;
# button presses arrive via handle_task_button / handle_post_button below and
# meet in the same per-action handlers)
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
        await _handle_undo(Press.from_payload(payload, channel))
        return

    # Requeue (🔄) sits on a ✅-completed post, which — like an undone one — has
    # been de-registered from "messages", so it is keyed off its own table.
    if key == emoji_key(EMOJI_REQUEUE):
        await _handle_requeue(Press.from_payload(payload, channel))
        return

    # Clap (👏) likewise lives on a ✅-completed (de-registered) post and is keyed
    # off its own table — a non-participant's tap tips the doer a bonus punto.
    if key == emoji_key(EMOJI_CLAP):
        await _handle_clap(Press.from_payload(payload, channel))
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
    press = Press.from_payload(payload, channel)

    if key == emoji_key(EMOJI_INFO):
        await _handle_info(task, press)
    elif key == emoji_key(EMOJI_FFWD):
        await _handle_ffwd(tid, task, press)
    elif key == emoji_key(EMOJI_DONE):
        await _handle_done(tid, task, cfg, tz, press)
    elif key in (emoji_key(EMOJI_SKIP), emoji_key(EMOJI_DELETE)):
        await _handle_skip_or_delete(tid, task, tz, press)
    elif key in (emoji_key(EMOJI_SHUSH), emoji_key(EMOJI_UNSHUSH)):
        await _handle_shush(tid, task, press, shush=key == emoji_key(EMOJI_SHUSH))


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


# ---------------------------------------------------------------------------
# Buttons (the primary entry way — every post since the button migration)
# ---------------------------------------------------------------------------
async def handle_task_button(tid: str, action: str, interaction: discord.Interaction) -> None:
    """Route a TaskButton press — the button-age twin of on_raw_reaction_add's
    task branch, dispatching to the same per-action handlers."""
    press = Press.from_interaction(interaction)
    snap = await store.snapshot()
    task = snap["tasks"].get(tid)
    # The tid rides in the custom_id, so unlike a reaction the press doesn't
    # prove the post is current — a button the bot failed to strip could
    # otherwise poke a *newer* occurrence. Live posts are still registered in
    # "messages"; anything else refuses politely.
    if (not task or not task.get("pending")
            or str(press.message_id) not in snap["messages"]):
        await press.whisper("These buttons have gone stale — this post is no longer live.")
        return
    cfg = guild_config(snap, task["guild_id"])
    if not config_ready(cfg):
        return
    tz = ZoneInfo(cfg["timezone"])
    if action == "info":
        await _handle_info(task, press)
    elif action == "ffwd":
        await _handle_ffwd(tid, task, press)
    elif action == "done":
        await _handle_done(tid, task, cfg, tz, press)
    elif action == "skip":
        await _handle_skip_or_delete(tid, task, tz, press)
    elif action in ("shush", "unshush"):
        await _handle_shush(tid, task, press, shush=action == "shush")


async def handle_list_button(tid: str, idx: int, interaction: discord.Interaction) -> None:
    """A 🧾 checklist item press: tick it as yours (a second tap unticks — but
    only your own), and the tick that turns the last box green completes the
    whole list through the ordinary ✅ path, so every distinct ticker earns a
    punto. A part-done tick costs exactly one API call: the free interaction
    response re-renders just the pressed post's buttons."""
    press = Press.from_interaction(interaction)
    snap = await store.snapshot()
    task = snap["tasks"].get(tid)
    # Same stale-press guard as handle_task_button: the custom_id proves the
    # task, the "messages" registration proves the post is current.
    if (not task or not task.get("pending")
            or str(press.message_id) not in snap["messages"]):
        await press.whisper("These buttons have gone stale — this post is no longer live.")
        return
    cfg = guild_config(snap, task["guild_id"])
    if not config_ready(cfg):
        return

    outcome = None  # "ticked" | "unticked" | "complete" | "taken" | "stale"
    taken_by = item = None
    updated = None
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        p = live.get("pending") if live else None
        items = (live or {}).get("items") or []
        if not p or idx >= len(items):
            outcome = "stale"  # resolved, or the checklist shrank under this post
        else:
            item = items[idx]
            ticks = p.setdefault("ticks", {})
            prev = ticks.get(str(idx))
            if prev is None:
                ticks[str(idx)] = {"user_id": press.user_id, "user_name": press.display}
                outcome = "complete" if len(ticks) == len(items) else "ticked"
            elif prev["user_id"] == press.user_id:
                ticks.pop(str(idx))
                outcome = "unticked"
            else:
                outcome, taken_by = "taken", prev["user_name"]
            if outcome in ("ticked", "unticked"):
                updated = json.loads(json.dumps(live))

    if outcome == "stale":
        await press.whisper("These buttons have gone stale — this post is no longer live.")
        return
    if outcome == "taken":
        await press.whisper(
            f"{EMOJI_LIST} **{item}** is already ticked — by {taken_by}, "
            "and only they can untick it."
        )
        return
    if outcome == "complete":
        # The final tick is committed; the shared ✅ path finishes the job
        # (status edit, per-ticker puntos, ↩️ 🔄 👏, recurrence roll).
        await _handle_done(tid, task, cfg, ZoneInfo(cfg["timezone"]), press)
        return
    # Part-done: flip just the pressed post's buttons (a nag keeps its 🤫 face).
    mids = (updated.get("pending") or {}).get("message_ids") or []
    reminder = bool(mids) and press.message_id != mids[0]
    await press.edit_pressed(view=make_task_view(tid, updated, reminder=reminder))


async def handle_post_button(action: str, interaction: discord.Interaction) -> None:
    """Route a resolved post's ↩️/🔄 PostButton press (👏 routes to claps.py)."""
    press = Press.from_interaction(interaction)
    if action == "undo":
        await _handle_undo(press)
    elif action == "requeue":
        await _handle_requeue(press)


async def _handle_info(task, press: Press) -> None:
    desc = task.get("description")
    if desc:
        await press.whisper(f"ℹ️ **{task['brief']}**\n{desc}")
    elif press.interaction is not None:
        await press.whisper("No extra details on this one.")
    await press.retract()


# --- Snooze "numpad" -------------------------------------------------------
# Tapping the ⏩ button answers with an *ephemeral* numpad (SnoozeView below):
# digit buttons plus an hours/days unit toggle, private to the snoozer and
# deliberately not persisted — after a restart a dead numpad is just a fresh ⏩
# tap away. Picking a number snoozes the occurrence and edits the task post
# with the result + ↩️ undo.
#
# The ⏩ *reaction* (pre-button posts) instead opens the legacy public panel
# message that self-reacts the same numpad, keyed in store["snooze_panels"] so
# it survives restarts.
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


async def _apply_snooze(
    tid: str, n: int, unit: str, *, panel_mid: Optional[int] = None
) -> Optional[tuple[dict, dt.datetime, str]]:
    """The snooze txn shared by the button numpad and the legacy reaction panel:
    bump ffwd_count and push remind_at out. When ``panel_mid`` is given the
    panel record is popped in the same txn, so two racing digit taps can't both
    land. Returns (before-snapshot, new remind instant, human amount), or None
    if the occurrence resolved (or the panel was spent) in the meantime."""
    hours = n if unit == "hours" else n * 24
    remind = None
    async with store.txn() as data:
        if panel_mid is not None and data["snooze_panels"].pop(str(panel_mid), None) is None:
            return None  # a concurrent tap already took this panel
        live = data["tasks"].get(tid)
        p = live.get("pending") if live else None
        if not p:
            return None
        before = json.loads(json.dumps(live))  # snapshot for undo
        p["ffwd_count"] = p.get("ffwd_count", 0) + 1
        remind = now_utc() + dt.timedelta(hours=hours)
        p["remind_at"] = to_iso(remind)
    amount = (f"{hours} hour{'' if hours == 1 else 's'}" if unit == "hours"
              else f"{n} day{'' if n == 1 else 's'}")
    return before, remind, amount


async def _announce_snooze(
    tid: str, before: dict, remind: dt.datetime, amount: str, mention: str,
    anchor_id: int, channel: discord.abc.Messageable,
) -> None:
    """Stamp the anchor post with the snooze result — keeping its live action
    row (the occurrence is still pending) plus a fresh ↩️ — and arm the undo."""
    status = (
        f"**{before['brief']}**\n"
        f"⏩ Snoozed {amount} by {mention} — next reminder {discord_ts(remind, 'R')}"
    )
    await _arm_undo("snooze", tid, before, anchor_id, channel)
    view = make_task_view(tid, before)
    try:
        # A 🧾 list's ↩️ joins the bottom control row rather than the items;
        # a completely full view (20 items + 5 controls) simply goes without —
        # the undo table is armed either way.
        view.add_item(PostButton(tid, "undo", row=4 if before.get("items") else None))
    except ValueError:
        pass
    try:
        await channel.get_partial_message(anchor_id).edit(
            content=status, view=view, allowed_mentions=NO_PINGS
        )
    except discord.HTTPException:
        pass


class SnoozeView(discord.ui.View):
    """The ⏩ numpad as an ephemeral button grid: a digit snoozes the pending
    occurrence by that many hours/days, the toggle flips the unit, ❌ cancels.
    Unlike the legacy reaction panel this is *not* persisted: the grid is
    private to the snoozer and transient — after a restart it goes dead, and
    ⏩ on the post summons a fresh one."""

    def __init__(self, tid: str, brief: str, anchor_id: int, unit: str = "hours") -> None:
        super().__init__(timeout=600)
        self.tid = tid
        self.brief = brief
        self.anchor_id = anchor_id
        self.unit = unit
        self._build()

    def content(self) -> str:
        return f"💤 **Snooze {self.brief}** — pick a number of **{self.unit}**."

    def _build(self) -> None:
        self.clear_items()
        for i, n in enumerate(SNOOZE_CHOICES):
            digit = discord.ui.Button(
                label=str(n), style=discord.ButtonStyle.secondary, row=i // 4
            )
            digit.callback = self._picker(n)
            self.add_item(digit)
        # Emoji-only: 📅/⏱️ shows the unit a press switches TO (the current
        # unit is bolded in the content line above), ❌ dismisses.
        flip = "days" if self.unit == "hours" else "hours"
        toggle = discord.ui.Button(
            emoji=EMOJI_SNOOZE_DAYS if flip == "days" else EMOJI_SNOOZE_HOURS,
            style=discord.ButtonStyle.secondary, row=1,
        )
        toggle.callback = self._toggle
        self.add_item(toggle)
        cancel = discord.ui.Button(
            emoji=EMOJI_DELETE, style=discord.ButtonStyle.secondary, row=1
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    def _picker(self, n: int):
        async def pick(interaction: discord.Interaction) -> None:
            unit = self.unit
            self.stop()
            applied = await _apply_snooze(self.tid, n, unit)
            if applied is None:
                await interaction.response.edit_message(
                    content="↩️ Too late — that occurrence was already resolved.",
                    view=None,
                )
                return
            before, remind, amount = applied
            await interaction.response.edit_message(
                content=f"⏩ Snoozed **{self.brief}** {amount} — next reminder "
                        f"{discord_ts(remind, 'R')}.",
                view=None,
            )
            await _announce_snooze(
                self.tid, before, remind, amount, interaction.user.mention,
                self.anchor_id, interaction.channel,
            )
        return pick

    async def _toggle(self, interaction: discord.Interaction) -> None:
        self.unit = "days" if self.unit == "hours" else "hours"
        self._build()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="💤 Snooze cancelled.", view=None)


async def _handle_ffwd(tid, task, press: Press) -> None:
    snap = await store.snapshot()
    live = snap["tasks"].get(tid)
    if not live or not live.get("pending"):
        await press.retract()
        if press.interaction is not None:
            await press.whisper("Nothing to snooze — this occurrence has already been resolved.")
        return  # occurrence already resolved — nothing to snooze
    if press.interaction is not None:
        numpad = SnoozeView(tid, task["brief"], press.message_id)
        try:
            await press.interaction.response.send_message(
                numpad.content(), view=numpad, ephemeral=True
            )
        except discord.HTTPException:
            pass
        return
    # ⏩ reaction on a pre-button post: the legacy public, persisted panel.
    channel = press.channel
    panel = await channel.send(
        snooze_panel_text(task["brief"], "hours"), allowed_mentions=NO_PINGS
    )
    async with store.txn() as data:
        data["snooze_panels"][str(panel.id)] = {
            "task_id": tid,
            "anchor_id": press.message_id,
            "unit": "hours",
            "brief": task["brief"],
            "channel_id": getattr(channel, "id", None),
        }
    for emoji in SNOOZE_REACTIONS:
        try:
            await panel.add_reaction(emoji)
        except discord.HTTPException:
            pass
    await press.retract()


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
    member = payload.member
    mention = member.mention if member else f"<@{payload.user_id}>"
    anchor_id = rec.get("anchor_id")

    applied = await _apply_snooze(tid, n, unit, panel_mid=payload.message_id)
    await safe_delete(panel)
    if applied is None:
        return  # resolved between opening the panel and picking a number
    if anchor_id:
        before, remind, amount = applied
        await _announce_snooze(tid, before, remind, amount, mention, anchor_id, channel)


async def _handle_shush(tid, task, press: Press, *, shush: bool) -> None:
    """🤫 sets the task's lifetime no-nag flag, 🔊 clears it: a shushed chore
    still fires on schedule, but the hourly reminders stop until someone taps
    🔊 on a live post. Nag posts carry the 🤫 button and a shushed chore's
    posts carry 🔊 (either emoji added manually on a live pre-button post
    routes here too). A tap that matches the current state (🤫 on an
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
    await press.retract()
    if not changed:
        # Task vanished under us, or already in the asked-for state.
        if press.interaction is not None:
            await press.whisper("Already sorted — this chore is in that state.")
        return
    # No separate confirmation post: stamp the tapped post itself with who
    # flipped the switch, replacing any earlier stamp so repeated toggles on
    # one post never stack up. The button/emoji face flips with it so the post
    # always shows the *next* action — the two directions never share a face.
    if shush:
        line = f"{EMOJI_SHUSH} Shushed by {press.mention} — reminders off."
    else:
        line = f"{EMOJI_UNSHUSH} Un-shushed by {press.mention} — reminders back on."
    stamps = (f"{EMOJI_SHUSH} Shushed by ", f"{EMOJI_UNSHUSH} Un-shushed by ")
    if press.interaction is not None:
        # One edit does it all: restamp the content and flip the action row
        # (reminder=True keeps a shush face on the post in both directions).
        content = press.interaction.message.content or ""
        kept = [ln for ln in content.split("\n") if ln and not ln.startswith(stamps)]
        await press.edit_pressed(
            content="\n".join(kept + [line]),
            view=make_task_view(tid, {**task, "no_nag": shush}, reminder=True),
            allowed_mentions=NO_PINGS,
        )
        return
    reacted = press.message
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
    try:
        content = (await reacted.fetch()).content or ""
        kept = [ln for ln in content.split("\n") if ln and not ln.startswith(stamps)]
        await reacted.edit(content="\n".join(kept + [line]), allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass


async def _handle_done(tid, task, cfg, tz, press: Press) -> None:
    channel = press.channel
    # A bounty is a chore the creator has put up for *someone else*: it's worth
    # double, and they can't claim it themselves.
    if task.get("bounty") and press.user_id == task.get("created_by"):
        await press.retract()
        await press.whisper(
            f"💰 {press.mention}, this is **your** bounty — someone else has to claim it "
            "(it's worth 2 puntos!)."
        )
        return
    # A puntobomb past its fuse can't be defused — the tick that blows it may
    # simply not have come round yet. Let the sweep do its grim work.
    if (task.get("puntobomb") and task.get("explodes_at")
            and now_utc() >= from_iso(task["explodes_at"])):
        await press.retract()
        await press.whisper("💥 Too late — this puntobomb has already gone off.")
        return

    await press.ack()
    completed = now_utc()
    records: list[dict] = []
    message_ids: list[int] = []
    before = None
    doers: list[dict] = []
    items: list = []
    legacy = True
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        p = live.get("pending") if live else None
        if not p:
            return
        before = json.loads(json.dumps(live))  # snapshot for undo
        legacy = p.get("ui") != "buttons"
        due = from_iso(p["due_at"])
        message_ids = list(p["message_ids"])
        for mid in message_ids:
            data["messages"].pop(str(mid), None)
        panels = _take_task_panels(data, tid)
        items = live.get("items") or []
        if items:
            # A ✅ on a part-done 🧾 list means "the rest is done too" — the
            # unticked remainder is swept as the presser's. Every distinct
            # ticker is a doer and earns their own punto (never a bounty, so
            # no doubling; a solo doer is the plain 1-chore-1-punto case).
            ticks = p.setdefault("ticks", {})
            for i in range(len(items)):
                ticks.setdefault(str(i), {"user_id": press.user_id,
                                          "user_name": press.display})
            seen: set = set()
            for i in range(len(items)):
                t = ticks[str(i)]
                if t["user_id"] not in seen:
                    seen.add(t["user_id"])
                    doers.append({"user_id": t["user_id"], "user_name": t["user_name"]})
        else:
            doers = [{"user_id": press.user_id, "user_name": press.display}]
        # A 🧾 row's kind can't carry once-vs-recurring like a plain chore's
        # does, so it says so explicitly (the once/recurring badge tally reads it).
        extra = {"recurring": bool(task["recurring"])} if items else {}
        records = [{
            "id": new_id(),  # lets an undo void exactly these log entries
            "ts": to_iso(completed),
            "month": completed.astimezone(tz).strftime("%Y-%m"),  # local-tz bucket
            "guild_id": task["guild_id"],
            "task_id": tid,
            "brief": task["brief"],
            "user_id": d["user_id"],
            "user_name": d["user_name"],
            "kind": ("puntobomb" if task.get("puntobomb")
                     else "list" if items
                     else "recurring" if task["recurring"] else "once"),
            "points": 2 if task.get("bounty") else 1,  # a defusal is the plain 1
            "due_at": p["due_at"],
            "late_seconds": max(0, int((completed - due).total_seconds())),
            **extra,
        } for d in doers]
        if live["recurring"]:
            live["pending"] = None
            live["next_due"] = to_iso(next_due(recurrence_of(live), tz, due, completed))
        else:
            data["tasks"].pop(tid, None)

    if records:
        for record in records:
            await store.log_completion(record)
        bonus = " 💰 **+2 puntos**" if task.get("bounty") else ""
        mentions = _join([f"<@{d['user_id']}>" for d in doers])
        if task.get("puntobomb"):
            status = (
                f"~~**{task['brief']}**~~\n"
                f"✂️ Defused by {press.mention} with time to spare • "
                f"{discord_ts(completed, 't')}"
            )
        elif items:
            each = " — +1 punto each" if len(doers) > 1 else ""
            status = (
                f"~~**{task['brief']}**~~\n"
                f"✅ {EMOJI_LIST} All {len(items)} ticked by {mentions}{each} • "
                f"{discord_ts(completed, 't')}"
            )
        else:
            status = (
                f"~~**{task['brief']}**~~\n"
                f"✅ Completed by {press.mention}{bonus} • {discord_ts(completed, 't')}"
            )
        stale_undo: list[tuple[int, bool]] = []
        stale_requeue: list[tuple[int, bool]] = []
        stale_clap: list[tuple[int, bool]] = []
        # No 🔄 on a defused bomb: a requeue past the fuse would re-arm it into
        # an instant kaboom. ↩️ (an honest retraction) and 👏 both stay.
        requeueable = not task.get("puntobomb")
        if message_ids:
            # Arm all three tables in ONE txn (one flush), then land ↩️ 🔄 👏 in
            # the one status edit — so a button can never be pressed before its
            # record exists. Retiring the rows left on this task's older
            # resolved posts waits until *after* that edit: the tidy-up doesn't
            # gate the new buttons, and the confirmation should land first.
            anchor = message_ids[-1]
            # The doers (one for a plain chore, every ticker for a 🧾 list) are
            # this occurrence's participants; anyone *else* can 👏 them a bonus
            # punto on the finished post.
            participants = doers
            async with store.txn() as data:
                stale_undo = _arm_undo_in(data, "done", tid, before, anchor,
                                          channel,
                                          completion_ids=[r["id"] for r in records])
                if requeueable:
                    stale_requeue = _arm_requeue_in(data, tid, before, anchor,
                                                    channel, task["guild_id"])
                stale_clap = _arm_clap_in(data, tid, anchor, channel,
                                          task["guild_id"], task["brief"],
                                          status, participants)
        await finalize_messages(
            channel, message_ids, status, legacy=legacy,
            view=(completed_view(tid, undo=True, requeue=requeueable, clap=True)
                  if message_ids else None),
        )
        await _delete_panels(panels)
        tidy: set[int] = set()
        await _tidy_stale(channel, stale_undo, EMOJI_UNDO, tidy)
        await _tidy_stale(channel, stale_requeue, EMOJI_REQUEUE, tidy)
        await _tidy_stale(channel, stale_clap, EMOJI_CLAP, tidy)
        for mid in tidy:
            await refresh_post_view(channel, mid)


async def _handle_skip_or_delete(tid, task, tz, press: Press) -> None:
    channel = press.channel
    await press.ack()
    message_ids: list[int] = []
    mode = None
    before = None
    legacy = True
    async with store.txn() as data:
        live = data["tasks"].get(tid)
        p = live.get("pending") if live else None
        if not p:
            return
        before = json.loads(json.dumps(live))  # snapshot for undo
        legacy = p.get("ui") != "buttons"
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

    if mode == "skip":
        status = f"**{task['brief']}**\n⏭️ Skipped this time by {press.mention} — back next cycle."
    elif mode == "delete":
        if task.get("puntobomb"):
            # Deleting a live bomb is always permitted — and on the record.
            status = render_coward(task["brief"], by=press.mention)
        else:
            status = f"~~**{task['brief']}**~~\n❌ Cancelled by {press.mention}."
    else:
        return
    tidy: set[int] = set()
    if message_ids:
        # Arm before the status edit lands the ↩️; retiring older posts' rows
        # waits until after it (same order as _handle_done).
        await _arm_undo(mode, tid, before, message_ids[-1], channel, tidy=tidy)
    await finalize_messages(
        channel, message_ids, status, legacy=legacy,
        view=completed_view(tid, undo=True) if message_ids else None,
    )
    await _delete_panels(panels)
    for mid in tidy:
        await refresh_post_view(channel, mid)


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
      * any action on a puntobomb — additionally refuse once the fuse has run
                            out: restoring would re-arm a bomb the next tick
                            blows, letting a stray ↩️ nuke everyone's puntos.
    """
    if (before.get("puntobomb") and before.get("explodes_at")
            and now_utc() >= from_iso(before["explodes_at"])):
        return False
    if action == "snooze":
        lp = live.get("pending") if live else None
        bp = before.get("pending")
        return bool(lp and bp and lp.get("due_at") == bp.get("due_at"))
    if before.get("recurring"):
        return live is not None and live.get("pending") is None
    return live is None


def _arm_undo_in(
    data: dict,
    action: str,
    tid: str,
    before: dict,
    anchor_id: int,
    channel: discord.abc.Messageable,
    *,
    completion_ids: Optional[list[str]] = None,
) -> list[tuple[int, bool]]:
    """Write the undo record for ``anchor_id`` into an open txn's ``data`` (the
    txn-body core of _arm_undo, so a caller arming several tables can land them
    all in one flush), returning the older anchors whose ↩️ this retires as
    (message_id, was_buttons) pairs for _tidy_stale. ``completion_ids`` are the
    log rows this action wrote — one for a plain ✅, one per ticker for a 🧾
    list — all voided together if the undo takes."""
    stale: list[tuple[int, bool]] = []
    for mid, rec in list(data["undo"].items()):
        if rec.get("task_id") == tid and str(mid) != str(anchor_id):
            data["undo"].pop(mid, None)
            stale.append((int(mid), rec.get("ui") == "buttons"))
    data["undo"][str(anchor_id)] = {
        "action": action,
        "task_id": tid,
        "before": before,
        "completion_ids": completion_ids,
        "channel_id": getattr(channel, "id", None),
        "ui": "buttons",
    }
    return stale


async def _arm_undo(
    action: str,
    tid: str,
    before: Optional[dict],
    anchor_id: int,
    channel: discord.abc.Messageable,
    *,
    completion_ids: Optional[list[str]] = None,
    tidy: Optional[set] = None,
) -> None:
    """Record how to reverse the action just taken; the ↩️ button itself lands
    with the caller's status edit. Only the most recent action per task is kept
    undoable, so older ↩️ buttons for this task are retired (via ``tidy`` — see
    _tidy_stale)."""
    if before is None:
        return
    async with store.txn() as data:
        stale = _arm_undo_in(
            data, action, tid, before, anchor_id, channel, completion_ids=completion_ids
        )
    await _tidy_stale(channel, stale, EMOJI_UNDO, tidy)


async def _restore_anchor(
    channel: discord.abc.Messageable, msg_id: int, tid: str, task: dict,
    *, legacy_record: bool = False,
) -> None:
    """Bring the resolved message back to a live, actionable post: restore the
    brief and the live action row (dropping ↩️/🔄/👏). A pre-button anchor also
    gets its old reaction UI swept — its other posts' reactions still work, so
    losing the emoji row here costs nothing."""
    pm = channel.get_partial_message(msg_id)
    try:
        await pm.edit(
            content=post_content(task, reminder=False, cfg={}),
            view=make_task_view(tid, task),
            allowed_mentions=NO_PINGS,
        )
    except discord.HTTPException:
        pass
    if legacy_record or (task.get("pending") or {}).get("ui") != "buttons":
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


async def _disarm_undo_button(press: Press, legacy_record: bool) -> None:
    """A refused undo: retire the dead ↩️ and say (quietly) why nothing happened."""
    if legacy_record:
        if bot.user:
            try:
                await press.message.remove_reaction(EMOJI_UNDO, bot.user)
            except discord.HTTPException:
                pass
    else:
        # The undo record was popped, so re-deriving drops just the ↩️ (any
        # armed 🔄/👏 stay).
        await refresh_post_view(press.channel, press.message_id)
    await press.whisper("↩️ Too late to undo — this chore has already moved on.")


async def _handle_undo(press: Press) -> None:
    channel = press.channel
    snap = await store.snapshot()
    if str(press.message_id) not in snap["undo"]:
        if press.interaction is not None:
            await press.whisper("Nothing left to undo here.")
        return  # fast path: a ↩️ on something we don't track — ignore
    await press.ack()

    # Everything authoritative is read from inside the txn so a concurrent
    # re-arm (e.g. a second snooze on this message) can't make us act on stale
    # snapshot data.
    outcome = None  # "ok" | "refused"
    action = before = None
    completion_ids: list[str] = []
    tid = None
    legacy_record = False
    clap_log_ids: list[str] = []
    async with store.txn() as data:
        rec = data["undo"].get(str(press.message_id))
        if not rec:
            return  # a concurrent ↩️ beat us to it
        action = rec["action"]
        before = rec["before"]
        # Newer records carry the list; pre-list ones a single completion_id.
        completion_ids = list(rec.get("completion_ids") or [])
        if rec.get("completion_id"):
            completion_ids.append(rec["completion_id"])
        tid = rec["task_id"]
        legacy_record = rec.get("ui") != "buttons"
        if can_undo(action, before, data["tasks"].get(tid)):
            data["tasks"][tid] = json.loads(json.dumps(before))
            pending = before.get("pending") or {}
            for mid in pending.get("message_ids", []):
                data["messages"][str(mid)] = tid
            outcome = "ok"
        else:
            outcome = "refused"
        data["undo"].pop(str(press.message_id), None)
        # Undoing a ✅ turns its post back into a live occurrence, so the 🔄
        # requeue and 👏 claps we armed on that same post no longer apply. Any
        # bonus puntos the claps already paid are retracted below — but only when
        # the undo actually takes (a refused ↩️ leaves the completion, and its
        # claps, standing).
        data["requeue"].pop(str(press.message_id), None)
        clap = data["claps"].pop(str(press.message_id), None)
        if clap:
            clap_log_ids = list(clap.get("log_ids", []))

    if outcome == "ok":
        if action == "done":
            for cid in completion_ids:  # one row, or one per 🧾 ticker
                await store.void_completion(cid)
        for lid in clap_log_ids:  # retract every bonus punto the claps awarded
            await store.void_completion(lid)
        await _restore_anchor(channel, press.message_id, tid, before,
                              legacy_record=legacy_record)
    elif outcome == "refused":
        await _disarm_undo_button(press, legacy_record)


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
def _arm_requeue_in(
    data: dict,
    tid: str,
    before: dict,
    anchor_id: int,
    channel: discord.abc.Messageable,
    guild_id: int,
) -> list[tuple[int, bool]]:
    """Write, into an open txn's ``data``, how to re-fire the task from a
    just-completed post (the 🔄 button itself lands with the caller's status
    edit), returning the older anchors whose 🔄 this retires as
    (message_id, was_buttons) pairs for _tidy_stale."""
    stale: list[tuple[int, bool]] = []
    for mid, rec in list(data["requeue"].items()):
        if rec.get("task_id") == tid and str(mid) != str(anchor_id):
            data["requeue"].pop(mid, None)
            stale.append((int(mid), rec.get("ui") == "buttons"))
    data["requeue"][str(anchor_id)] = {
        "task_id": tid,
        "before": before,  # lets a completed one-off be recreated and re-run
        "guild_id": guild_id,
        "channel_id": getattr(channel, "id", None),
        "ui": "buttons",
    }
    return stale


async def _handle_requeue(press: Press) -> None:
    channel = press.channel
    snap = await store.snapshot()
    if str(press.message_id) not in snap["requeue"]:
        if press.interaction is not None:
            await press.whisper("This post's 🔄 has been retired.")
        return  # a 🔄 on something we don't track — ignore
    await press.ack()

    outcome = None  # "fired" | "busy" | "gone"
    tid = cfg = brief = None
    legacy_record = False
    async with store.txn() as data:
        rec = data["requeue"].get(str(press.message_id))
        if not rec:
            return  # a concurrent 🔄 beat us to it
        tid = rec["task_id"]
        legacy_record = rec.get("ui") != "buttons"
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
            data["requeue"].pop(str(press.message_id), None)

    pm = press.message
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
            "user_id": press.user_id,
            "user_name": press.display,
            "kind": "requeue",
            "points": 0,
        })
        await fire_task(tid, channel, cfg)
        # Tidy the spent 🔄 off the old completed post (both ours and theirs) and
        # confirm right where they tapped — the fresh post may be far down.
        await press.retract()
        if legacy_record:
            if bot.user:
                try:
                    await pm.remove_reaction(EMOJI_REQUEUE, bot.user)
                except discord.HTTPException:
                    pass
        else:
            await refresh_post_view(channel, press.message_id)
        try:
            await channel.send(
                f"🔄 Re-queued by {press.mention} — posted it again below.",
                reference=pm,
                allowed_mentions=NO_PINGS,
            )
        except discord.HTTPException:
            pass
    elif outcome == "busy":
        # Leave the button so they can requeue once the live one is resolved.
        await press.retract()
        await press.whisper(
            f"🔄 {press.mention}, that chore is already queued — finish the active "
            "reminder above first."
        )
    else:  # gone
        if legacy_record:
            if bot.user:
                try:
                    await pm.remove_reaction(EMOJI_REQUEUE, bot.user)
                except discord.HTTPException:
                    pass
        else:
            await refresh_post_view(channel, press.message_id)
        if press.interaction is not None:
            await press.whisper("Nothing left to re-run here.")


__all__ = [
    "SNOOZE_REACTIONS",
    "SnoozeView",
    "_announce_snooze",
    "_apply_snooze",
    "_arm_requeue_in",
    "_arm_undo",
    "_arm_undo_in",
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
    "handle_list_button",
    "handle_post_button",
    "handle_task_button",
    "on_raw_reaction_add",
    "on_raw_reaction_remove",
    "snooze_panel_text",
]
