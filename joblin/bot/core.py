"""Shared singletons: the JoblinBot instance, the Store, and constants.

Dependency-free (only stdlib + discord + Store) so every other module in the
package can import from it without import cycles.
"""
from __future__ import annotations

import logging
import os
import pathlib

import discord
from discord.ext import commands

from ..store import Store

log = logging.getLogger("joblin")

DATA_DIR = pathlib.Path(os.getenv("JOBLIN_DATA_DIR", "data"))
store = Store(DATA_DIR / "store.json", DATA_DIR / "completions.jsonl")

# Repo root (the directory containing pyproject.toml), used by /redeploy to
# git-pull and re-exec the bot.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

NO_PINGS = discord.AllowedMentions.none()

# When (guild-local wall clock) the nightly backup + auto-leaderboard post
# fires. Lives here — not in backup.py — because scoring's day-over-day frame
# (rank_spice) rolls at the same instant and the two must never drift.
NIGHTLY_HOUR, NIGHTLY_MINUTE = 23, 59

# A small curated list for the /joblinconfig timezone autocomplete. Any valid
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


class JoblinBot(commands.Bot):
    def __init__(self) -> None:
        # Default (non-privileged) intents cover guilds + reactions, which is
        # all we need: slash commands and raw reaction events. We do NOT need
        # message_content or members.
        intents = discord.Intents.default()
        super().__init__(command_prefix="!unused!", intents=intents, help_command=None)

    async def setup_hook(self) -> None:
        # Local imports avoid an import cycle (these modules import `bot`).
        from .scheduler import scheduler
        from .games import DoEmUpButton
        from ..web import start_web
        scheduler.start()
        # The web UI shares this event loop; it's a no-op unless configured
        # and never raises (a web failure must not take the bot down).
        await start_web()
        # Revive every do-em-up's buttons after a restart in one shot.
        self.add_dynamic_items(DoEmUpButton)
        dev_guild = os.getenv("DEV_GUILD_ID")
        if dev_guild:
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to dev guild %s", dev_guild)
        else:
            await self.tree.sync()
            log.info("Synced global commands (may take up to ~1h to appear)")


bot = JoblinBot()

__all__ = [
    "bot", "JoblinBot", "store", "log", "DATA_DIR", "REPO_ROOT",
    "NO_PINGS", "COMMON_TZS", "NIGHTLY_HOUR", "NIGHTLY_MINUTE",
]
