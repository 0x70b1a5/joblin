"""``/joblinconfig`` — the guild's channel, timezone, reminder role, and
trinket bar (a bar change is recorded as an event so closed months keep the
bar they ended under — see ``scoring.bar_for``).

``/quiettime`` lives here too: it's guild-wide operational config (the
switch + optional daily window), not a per-chore action.
"""

from __future__ import annotations

from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from ... import trinkets
from ...models import (
    format_quiet_window,
    in_quiet_window,
    now_utc,
    parse_quiet_clock,
)
from ..core import COMMON_TZS, NO_PINGS, bot, store
from ..helpers import config_ready, guild_config
from ..scoring import record_bar_change


def _is_off(text: Optional[str]) -> bool:
    return (text or "").strip().lower() in ("off", "none", "clear")


def _quiet_status_line(cfg: dict) -> str:
    on = "🌙 **on**" if cfg.get("quiet") else "**off**"
    start, end = cfg.get("quiet_start"), cfg.get("quiet_end")
    if start and end:
        try:
            window = format_quiet_window(start, end)
        except ValueError:
            window = f"{start}–{end}"
        return f"Quiet time: {on} · daily window {window}"
    return f"Quiet time: {on} · no daily window"


def _default_cfg() -> dict:
    return {
        "channel_id": None, "timezone": None, "reminder_role_id": None,
        "item_bar": trinkets.DEFAULT_BAR,
    }


@bot.tree.command(name="joblinconfig", description="Set the channel, timezone, and optional reminder role")
@app_commands.describe(
    channel="Channel where tasks are posted",
    timezone="IANA timezone, e.g. Europe/Berlin (autocompletes)",
    reminder_role="Role to ping on overdue hourly reminders (optional)",
    item_bar="Puntos per trinket each month — every multiple earns another (default 25)",
    declutter="Sweep superseded chore posts automatically — the 📜 Daily Log keeps the record (default On)",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def joblinconfig(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    timezone: Optional[str] = None,
    reminder_role: Optional[discord.Role] = None,
    item_bar: Optional[int] = None,
    declutter: Optional[bool] = None,
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
        cfg = data["configs"].setdefault(str(interaction.guild_id), _default_cfg())
        cfg.setdefault("item_bar", trinkets.DEFAULT_BAR)
        if channel is not None:
            cfg["channel_id"] = channel.id
        if timezone is not None:
            cfg["timezone"] = timezone
        if reminder_role is not None:
            cfg["reminder_role_id"] = reminder_role.id
        if item_bar is not None:
            record_bar_change(cfg, item_bar, now_utc())
        if declutter is not None:
            cfg["declutter"] = declutter
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
        f"• Trinket bar: **{bar} puntos** each — every multiple earns another 🖼️\n"
        f"• Declutter: **{'on' if current.get('declutter', True) else 'off'}** — "
        "superseded chore posts are swept; the 📜 Daily Log keeps the record\n"
        f"• {_quiet_status_line(current)}"
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


# ---------------------------------------------------------------------------
# /quiettime — toggle the hush, or set a daily window
# ---------------------------------------------------------------------------
@bot.tree.command(
    name="quiettime",
    description="Toggle quiet time, or set a daily window that hushes fires and nags",
)
@app_commands.describe(
    start="Begins then; alone, runs until 23:59. After end wraps midnight. off clears.",
    end="Ends then; alone, starts at 00:00. off clears the daily window.",
)
async def quiettime(
    interaction: discord.Interaction,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> None:
    start_s = start.strip() if start else None
    end_s = end.strip() if end else None
    gid = str(interaction.guild_id)
    now = now_utc()

    # --- clear the daily window --------------------------------------------
    if _is_off(start_s) or _is_off(end_s):
        if not ((start_s is None or _is_off(start_s))
                and (end_s is None or _is_off(end_s))):
            await interaction.response.send_message(
                "❌ Can't mix `off` with a time — pass `off` alone to clear "
                "the daily window (the on/off switch stays as it is).",
                ephemeral=True,
            )
            return
        async with store.txn() as data:
            cfg = data["configs"].setdefault(gid, _default_cfg())
            cfg["quiet_start"] = None
            cfg["quiet_end"] = None
            cfg["quiet_was_in"] = None
            current = dict(cfg)
        await interaction.response.send_message(
            "Daily quiet window cleared. " + _quiet_status_line(current) + ".",
            allowed_mentions=NO_PINGS,
        )
        return

    # --- set a daily window ------------------------------------------------
    if start_s is not None or end_s is not None:
        snap = await store.snapshot()
        cfg = guild_config(snap, interaction.guild_id) or {}
        tz_name = cfg.get("timezone")
        if not tz_name:
            await interaction.response.send_message(
                "❌ Set a timezone with `/joblinconfig` first so I know when "
                "that clock time is.",
                ephemeral=True,
            )
            return
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            await interaction.response.send_message(
                f"❌ Unknown timezone `{tz_name}`. Fix it with `/joblinconfig`.",
                ephemeral=True,
            )
            return
        try:
            start_hh = parse_quiet_clock(start_s) if start_s else "00:00"
            end_hh = parse_quiet_clock(end_s) if end_s else "23:59"
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ {e}\nTry a clock time like `22:00`, `10pm`, or `midnight`.",
                ephemeral=True,
            )
            return
        in_win = in_quiet_window(now, start_hh, end_hh, tz)
        async with store.txn() as data:
            live = data["configs"].setdefault(gid, _default_cfg())
            live["quiet_start"] = start_hh
            live["quiet_end"] = end_hh
            live["quiet_was_in"] = in_win
            if in_win:
                live["quiet"] = True
            current = dict(live)
        window = format_quiet_window(start_hh, end_hh)
        state = "🌙 **on** now" if current.get("quiet") else "**off** right now — it'll hush when the window starts"
        await interaction.response.send_message(
            f"Quiet window set to **{window}**. Currently {state}.\n"
            "Fires and nags stay silent while it's on; nightly bookkeeping still posts. "
            "`/quiettime` with no args toggles the switch.",
            allowed_mentions=NO_PINGS,
        )
        return

    # --- no args: toggle the switch ----------------------------------------
    async with store.txn() as data:
        cfg = data["configs"].setdefault(gid, _default_cfg())
        cfg["quiet"] = not bool(cfg.get("quiet"))
        current = dict(cfg)
    if current.get("quiet"):
        head = ("🌙 Quiet time is **on** — chores won't fire or nag "
                "(nightly bookkeeping still posts).")
    else:
        head = "Quiet time is **off** — fires and nags are back."
    extra = ""
    start_hh, end_hh = current.get("quiet_start"), current.get("quiet_end")
    if start_hh and end_hh:
        try:
            extra = f"\nDaily window still {format_quiet_window(start_hh, end_hh)}."
        except ValueError:
            extra = f"\nDaily window still {start_hh}–{end_hh}."
    await interaction.response.send_message(
        head + extra, allowed_mentions=NO_PINGS,
    )


_QUIET_CLOCK_SUGGESTIONS = (
    "22:00", "07:00", "00:00", "23:59", "21:00", "08:00",
    "noon", "midnight", "off",
)


@quiettime.autocomplete("start")
@quiettime.autocomplete("end")
async def _quiet_clock_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.strip()
    choices: list[app_commands.Choice] = []
    if cur:
        if _is_off(cur):
            choices.append(app_commands.Choice(
                name="off — clear the daily window", value="off"))
        else:
            try:
                hhmm = parse_quiet_clock(cur)
                choices.append(app_commands.Choice(
                    name=f"{hhmm}  (from {cur})"[:100], value=cur[:100]))
            except ValueError:
                choices.append(app_commands.Choice(
                    name="⚠️ e.g. 22:00 · 10pm · midnight · off",
                    value=cur[:100]))
    for text in _QUIET_CLOCK_SUGGESTIONS:
        if cur and cur.lower() not in text.lower():
            continue
        if _is_off(text):
            choices.append(app_commands.Choice(
                name="off — clear the daily window", value="off"))
            continue
        try:
            hhmm = parse_quiet_clock(text)
        except ValueError:
            continue
        choices.append(app_commands.Choice(
            name=f"{text}  →  {hhmm}"[:100], value=text))
    seen, out = set(), []
    for c in choices:
        if c.value in seen:
            continue
        seen.add(c.value)
        out.append(c)
    return out[:25]


__all__ = [
    "_quiet_clock_autocomplete",
    "_tz_autocomplete",
    "joblinconfig",
    "quiettime",
]
