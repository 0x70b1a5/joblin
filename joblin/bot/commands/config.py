"""``/joblinconfig`` — the guild's channel, timezone, reminder role, and
trinket bar (a bar change is recorded as an event so closed months keep the
bar they ended under — see ``scoring.bar_for``)."""

from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from ... import trinkets
from ...models import now_utc
from ..core import COMMON_TZS, NO_PINGS, bot, store
from ..helpers import config_ready
from ..scoring import record_bar_change


@bot.tree.command(name="joblinconfig", description="Set the channel, timezone, and optional reminder role")
@app_commands.describe(
    channel="Channel where tasks are posted",
    timezone="IANA timezone, e.g. Europe/Berlin (autocompletes)",
    reminder_role="Role to ping on overdue hourly reminders (optional)",
    item_bar="Puntos per trinket each month — every multiple earns another (default 25)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def joblinconfig(
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
            "❌ The trinket bar must be at least 1 punto.", ephemeral=True
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
            record_bar_change(cfg, item_bar, now_utc())
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
        f"• Trinket bar: **{bar} puntos** each — every multiple earns another 🖼️"
    )
    if item_bar is not None:
        msg += "\n  ↳ _counts for the month underway and onward; closed months keep the bar they ended under._"
    if not config_ready(current):
        msg += "\n\n⚠️ Set **both** a channel and a timezone before creating tasks."
    await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=NO_PINGS)


@joblinconfig.autocomplete("timezone")
async def _tz_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    matches = [z for z in COMMON_TZS if cur in z.lower()][:25]
    return [app_commands.Choice(name=z, value=z) for z in matches]


__all__ = [
    "_tz_autocomplete",
    "joblinconfig",
]
