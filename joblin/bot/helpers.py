from __future__ import annotations

import os
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from ..models import (
    EMOJI_DELETE,
    EMOJI_DONE,
    EMOJI_FFWD,
    EMOJI_INFO,
    EMOJI_SHUSH,
    EMOJI_SKIP,
    EMOJI_UNSHUSH,
    UTC,
    describe_repeat,
    recurrence_of,
)
from .core import (
    NO_PINGS,
    bot,
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


async def add_task_reactions(
    message: discord.Message, task: dict, *, reminder: bool = False
) -> None:
    await message.add_reaction(EMOJI_DONE)
    await message.add_reaction(EMOJI_FFWD)
    if task.get("description"):
        await message.add_reaction(EMOJI_INFO)
    # Recurring chores skip just this occurrence (⏭️); one-offs are deleted (❌).
    await message.add_reaction(EMOJI_SKIP if task.get("recurring") else EMOJI_DELETE)
    if reminder:  # nags grow a 🤫 so the hourly reminders can be shushed
        await message.add_reaction(EMOJI_SHUSH)
    elif task.get("no_nag"):  # a shushed chore's post offers 🔊 to un-shush
        await message.add_reaction(EMOJI_UNSHUSH)


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
    await add_task_reactions(msg, task, reminder=reminder)
    return msg


async def safe_delete(message: Optional[discord.Message]) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except discord.HTTPException:
        pass


# The functional reactions the bot itself puts on a posted chore. When an
# occurrence resolves we strip exactly these — never a 😄 / 🎉 a family member
# piled on for fun — see _clear_bot_reactions.
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
    channel: discord.abc.Messageable, message_ids: list[int], status: str
) -> None:
    """Take down our reactions on every message of a resolved occurrence (keeping
    any a member added for fun) and rewrite the most recent one with a status line."""
    for mid in message_ids:
        pm = channel.get_partial_message(mid)
        await _clear_bot_reactions(pm, TASK_FUNCTIONAL_EMOJIS)
    if message_ids:
        last = channel.get_partial_message(message_ids[-1])
        try:
            await last.edit(content=status, allowed_mentions=NO_PINGS)
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
    "TASK_FUNCTIONAL_EMOJIS",
    "_clear_bot_reactions",
    "_game_tz",
    "_remove_user_reaction",
    "add_task_reactions",
    "bounty_tag",
    "config_ready",
    "finalize_messages",
    "guild_config",
    "post_content",
    "post_occurrence",
    "safe_delete",
    "schedule_label",
    "web_base_url",
]
