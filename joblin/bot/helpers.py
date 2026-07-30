from __future__ import annotations

import os
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from ..models import (
    EMOJI_CLAP,
    EMOJI_DELETE,
    EMOJI_DONE,
    EMOJI_FFWD,
    EMOJI_INFO,
    EMOJI_REQUEUE,
    EMOJI_SHUSH,
    EMOJI_SKIP,
    EMOJI_UNDO,
    EMOJI_UNSHUSH,
    UTC,
    describe_repeat,
    recurrence_of,
)
from .core import (
    NO_PINGS,
    bot,
    store,
)



# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def guild_config(snapshot: dict, guild_id: int) -> Optional[dict]:
    return snapshot["configs"].get(str(guild_id))


def config_ready(cfg: Optional[dict]) -> bool:
    return bool(cfg and cfg.get("channel_id") and cfg.get("timezone"))


def web_base_url() -> Optional[str]:
    """The public URL of the bundled web UI, or None when it isn't configured.
    Read straight from the env (not joblin.web) so the bot package never needs
    to import the web package just to mention its address."""
    base = (os.getenv("WEB_BASE_URL") or "").strip().rstrip("/")
    return base or None


def schedule_label(task: dict) -> str:
    rule = recurrence_of(task)
    if rule["freq"] == "once":
        return "one-off"
    return f"{describe_repeat(rule)} at {task.get('time_of_day')}"


def bounty_tag(task: dict) -> str:
    """A small inline marker shown on a bounty's posts (worth 2 puntos, not the poster)."""
    return " 💰 *bounty · 2 puntos*" if task.get("bounty") else ""


def post_content(task: dict, *, reminder: bool, cfg: dict) -> str:
    brief = task["brief"]
    tag = bounty_tag(task)
    if not reminder:
        return f"**{brief}**{tag}"
    role_id = cfg.get("reminder_role_id")
    prefix = f"<@&{role_id}> " if role_id else ""
    return f"{prefix}⏰ Still pending: **{brief}**{tag}"


# ---------------------------------------------------------------------------
# Presses: one user action, arriving as a reaction OR a button
# ---------------------------------------------------------------------------
class Press:
    """One user action on one of our posts — a raw reaction or a button press —
    normalized so each per-action handler runs identically for both entry ways.
    Only the verbs that differ live here: ``retract`` pulls the clicker's emoji
    back off (a no-op for buttons, which leave nothing behind), ``whisper``
    sends a just-for-you note (ephemeral for buttons, a channel reply for
    reactions), ``ack`` answers a button press inside Discord's 3s deadline
    before slow work, and ``edit_pressed`` restyles the pressed message (riding
    the free interaction response when one is still open)."""

    def __init__(
        self, *, user_id: int, mention: str, display: str, message_id: int,
        channel: discord.abc.Messageable,
        interaction: Optional[discord.Interaction] = None,
        payload: Optional[discord.RawReactionActionEvent] = None,
    ) -> None:
        self.user_id = user_id
        self.mention = mention
        self.display = display
        self.message_id = message_id
        self.channel = channel
        self.interaction = interaction
        self.payload = payload

    @classmethod
    def from_payload(
        cls, payload: discord.RawReactionActionEvent, channel: discord.abc.Messageable
    ) -> "Press":
        member = payload.member
        return cls(
            user_id=payload.user_id,
            mention=member.mention if member else f"<@{payload.user_id}>",
            display=member.display_name if member else str(payload.user_id),
            message_id=payload.message_id,
            channel=channel,
            payload=payload,
        )

    @classmethod
    def from_interaction(cls, interaction: discord.Interaction) -> "Press":
        user = interaction.user
        return cls(
            user_id=user.id,
            mention=user.mention,
            display=user.display_name,
            message_id=interaction.message.id,
            channel=interaction.channel,
            interaction=interaction,
        )

    @property
    def message(self) -> discord.PartialMessage:
        return self.channel.get_partial_message(self.message_id)

    async def ack(self) -> None:
        if self.interaction is not None and not self.interaction.response.is_done():
            try:
                await self.interaction.response.defer()
            except discord.HTTPException:
                pass

    async def retract(self) -> None:
        if self.payload is not None:
            await _remove_user_reaction(self.message, self.payload)

    async def whisper(self, text: str) -> None:
        try:
            if self.interaction is None:
                await self.channel.send(
                    text, reference=self.message, allowed_mentions=NO_PINGS
                )
            elif self.interaction.response.is_done():
                await self.interaction.followup.send(text, ephemeral=True)
            else:
                await self.interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def edit_pressed(self, **kw) -> None:
        try:
            if self.interaction is not None and not self.interaction.response.is_done():
                await self.interaction.response.edit_message(**kw)
            else:
                await self.message.edit(**kw)
        except discord.HTTPException:
            pass


# ---------------------------------------------------------------------------
# Buttons: the live post's action row and the resolved post's ↩️/🔄/👏 row
# ---------------------------------------------------------------------------
class TaskButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"task:(?P<action>done|ffwd|info|skip|shush|unshush):(?P<tid>[\w-]+)",
):
    """A persistent action button on a live occurrence post — the button-age
    successor of the ✅ ⏩ ℹ️ ⏭️/❌ (🤫/🔊) self-reactions. The task id rides in
    the custom_id (exactly like DoEmUpButton), so one ``add_dynamic_items``
    registration on startup revives every live post's buttons after a restart
    with no per-message bookkeeping."""

    # Emoji-only faces (no labels) — phone screens are narrow and the row is
    # wide; /joblinhelp is the legend.
    FACES = {  # action -> (emoji, style)
        "done":    (EMOJI_DONE, discord.ButtonStyle.secondary),
        "ffwd":    (EMOJI_FFWD, discord.ButtonStyle.secondary),
        "info":    (EMOJI_INFO, discord.ButtonStyle.secondary),
        "skip":    (EMOJI_SKIP, discord.ButtonStyle.secondary),
        "shush":   (EMOJI_SHUSH, discord.ButtonStyle.secondary),
        "unshush": (EMOJI_UNSHUSH, discord.ButtonStyle.secondary),
    }
    # One-offs aren't skipped, they're cancelled — same action id, harder face.
    CANCEL_FACE = (EMOJI_DELETE, discord.ButtonStyle.secondary)

    def __init__(self, tid: str, action: str, *, face: Optional[tuple] = None) -> None:
        self.tid = tid
        self.action = action
        emoji, style = face or self.FACES[action]
        super().__init__(discord.ui.Button(
            emoji=emoji, style=style,
            custom_id=f"task:{action}:{tid}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # noqa: ANN001
        return cls(match["tid"], match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        from .reactions import handle_task_button  # runtime import — no cycle
        await handle_task_button(self.tid, self.action, interaction)


def make_task_view(tid: str, task: dict, *, reminder: bool = False) -> discord.ui.View:
    """The live post's action row — mirrors what the bot used to self-react:
    ✅ ⏩ (ℹ️ with a description) ⏭️/❌, plus 🔊 on a shushed chore's posts and
    🤫 on a nag."""
    view = discord.ui.View(timeout=None)
    view.add_item(TaskButton(tid, "done"))
    view.add_item(TaskButton(tid, "ffwd"))
    if task.get("description"):
        view.add_item(TaskButton(tid, "info"))
    if task.get("recurring"):
        view.add_item(TaskButton(tid, "skip"))
    else:
        view.add_item(TaskButton(tid, "skip", face=TaskButton.CANCEL_FACE))
    if task.get("no_nag"):
        view.add_item(TaskButton(tid, "unshush"))
    elif reminder:
        view.add_item(TaskButton(tid, "shush"))
    return view


class PostButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"post:(?P<action>undo|requeue|clap):(?P<tid>[\w-]+)",
):
    """A persistent button on a *resolved* post: ↩️ undo, 🔄 requeue, 👏 clap.
    Which of the three a post carries is decided by the same restart-safe store
    tables that route the equivalent legacy reactions (``undo`` / ``requeue`` /
    ``claps``, keyed by message id): handlers look the pressed message up
    there, so a stale button on a retired post refuses politely."""

    FACES = {  # action -> (emoji, style); emoji-only except the clap tally
        "undo":    (EMOJI_UNDO, discord.ButtonStyle.secondary),
        "requeue": (EMOJI_REQUEUE, discord.ButtonStyle.secondary),
        "clap":    (EMOJI_CLAP, discord.ButtonStyle.secondary),
    }

    def __init__(self, tid: str, action: str, *, clap_count: int = 0) -> None:
        self.tid = tid
        self.action = action
        emoji, style = self.FACES[action]
        # ×n is data (how many claps), not a caption — it stays.
        label = f"×{clap_count}" if action == "clap" and clap_count else None
        super().__init__(discord.ui.Button(
            emoji=emoji, label=label, style=style,
            custom_id=f"post:{action}:{tid}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # noqa: ANN001
        return cls(match["tid"], match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        # Runtime imports — reactions/claps import helpers, never the reverse.
        if self.action == "clap":
            from .claps import handle_clap_button
            await handle_clap_button(interaction)
        else:
            from .reactions import handle_post_button
            await handle_post_button(self.action, interaction)


def completed_view(
    tid: str, *, undo: bool = False, requeue: bool = False,
    clap: bool = False, clap_count: int = 0,
) -> Optional[discord.ui.View]:
    """The resolved post's action row, or None when nothing is armed."""
    if not (undo or requeue or clap):
        return None
    view = discord.ui.View(timeout=None)
    if undo:
        view.add_item(PostButton(tid, "undo"))
    if requeue:
        view.add_item(PostButton(tid, "requeue"))
    if clap:
        view.add_item(PostButton(tid, "clap", clap_count=clap_count))
    return view


def post_view_for(snap: dict, mid: int) -> Optional[discord.ui.View]:
    """Rebuild a resolved post's action row from whatever the undo/requeue/claps
    tables still hold for it (None once they hold nothing) — the single source
    of truth when a button retires or a clap bumps the tally."""
    key = str(mid)
    undo = snap["undo"].get(key)
    requeue = snap["requeue"].get(key)
    clap = snap["claps"].get(key)
    rec = undo or requeue or clap
    if rec is None:
        return None
    return completed_view(
        rec.get("task_id", "0"),
        undo=undo is not None,
        requeue=requeue is not None,
        clap=clap is not None,
        clap_count=len(clap.get("clappers", [])) if clap else 0,
    )


async def refresh_post_view(channel: discord.abc.Messageable, mid: int) -> None:
    """Re-derive and re-apply a resolved post's buttons after a table change."""
    snap = await store.snapshot()
    try:
        await channel.get_partial_message(mid).edit(view=post_view_for(snap, mid))
    except discord.HTTPException:
        pass


async def _tidy_stale(
    channel: discord.abc.Messageable,
    stale: list[tuple[int, bool]],
    emoji: str,
    tidy: Optional[set],
) -> None:
    """Retire a dead ↩️/🔄/👏 from a task's older resolved posts. A legacy
    (reaction-era) anchor loses our emoji; a button anchor is re-rendered from
    the tables — batched through ``tidy`` when the caller retires several
    tables' worth at once, so each post is edited only once."""
    for mid, is_buttons in stale:
        if is_buttons:
            if tidy is not None:
                tidy.add(mid)
            else:
                await refresh_post_view(channel, mid)
        elif bot.user:
            try:
                await channel.get_partial_message(mid).remove_reaction(emoji, bot.user)
            except discord.HTTPException:
                pass


async def post_occurrence(
    channel: discord.abc.Messageable, tid: str, task: dict, cfg: dict, *, reminder: bool
) -> discord.Message:
    allowed = (
        discord.AllowedMentions(roles=True)
        if reminder and cfg.get("reminder_role_id")
        else NO_PINGS
    )
    return await channel.send(
        post_content(task, reminder=reminder, cfg=cfg),
        view=make_task_view(tid, task, reminder=reminder),
        allowed_mentions=allowed,
    )


async def safe_delete(message: Optional[discord.Message]) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        pass


# The functional reactions the bot self-reacted on chores posted before the
# button migration. When such a legacy occurrence resolves we strip exactly
# these — never a 😄 / 🎉 a family member piled on for fun — see
# _clear_bot_reactions.
TASK_FUNCTIONAL_EMOJIS = (
    EMOJI_DONE, EMOJI_FFWD, EMOJI_INFO, EMOJI_SKIP, EMOJI_DELETE,
    EMOJI_SHUSH, EMOJI_UNSHUSH,
)


async def _clear_bot_reactions(
    message: discord.PartialMessage, emojis: tuple[str, ...]
) -> None:
    """Take down the bot's *functional* buttons from a resolved post while leaving
    any reaction a member added for fun untouched — unlike ``clear_reactions``,
    which wipes every reaction on the message.

    Per functional emoji we try ``clear_reaction`` (which, with Manage Messages,
    also sweeps members' own copies of that same button — e.g. a pitch-in's ✅
    tallies — so the closed post reads clean); lacking that permission we at least
    pull our own copy so the dead button goes away.
    """
    for emoji in emojis:
        try:
            await message.clear_reaction(emoji)
            continue  # also removed everyone else's copy of this button
        except discord.HTTPException:
            pass
        if bot.user:  # no Manage Messages — settle for removing just ours
            try:
                await message.remove_reaction(emoji, bot.user)
            except discord.HTTPException:
                pass


async def finalize_messages(
    channel: discord.abc.Messageable, message_ids: list[int], status: str,
    *, view: Optional[discord.ui.View] = None, legacy: bool = False,
) -> None:
    """Rewrite the most recent message of a resolved occurrence with a status
    line (carrying ``view`` — e.g. the ↩️/🔄/👏 row), then take down the action
    UI on its older messages. The status edit goes first so the resolution is
    visible the moment it lands — the cleanup edits queue behind it in the
    channel's rate-limit bucket, not in front. ``legacy`` marks an occurrence
    that fired before the button migration: its posts self-reacted, so the old
    emoji sweep also runs (keeping any reaction a member added for fun)."""
    if not message_ids:
        return
    last = channel.get_partial_message(message_ids[-1])
    try:
        await last.edit(content=status, view=view, allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass
    for mid in message_ids:
        pm = channel.get_partial_message(mid)
        if legacy:
            await _clear_bot_reactions(pm, TASK_FUNCTIONAL_EMOJIS)
        if mid != message_ids[-1]:
            try:
                await pm.edit(view=None)
            except discord.HTTPException:
                pass


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
def _game_tz(snap: dict, guild_id: int) -> ZoneInfo:
    cfg = guild_config(snap, guild_id)
    if cfg and cfg.get("timezone"):
        try:
            return ZoneInfo(cfg["timezone"])
        except Exception:
            pass
    return UTC


__all__ = [
    "PostButton",
    "Press",
    "TASK_FUNCTIONAL_EMOJIS",
    "TaskButton",
    "_clear_bot_reactions",
    "_game_tz",
    "_remove_user_reaction",
    "_tidy_stale",
    "bounty_tag",
    "completed_view",
    "config_ready",
    "finalize_messages",
    "guild_config",
    "make_task_view",
    "post_content",
    "post_occurrence",
    "post_view_for",
    "refresh_post_view",
    "safe_delete",
    "schedule_label",
    "web_base_url",
]
