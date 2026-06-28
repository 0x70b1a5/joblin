from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord import app_commands

from .core import (
    REPO_ROOT,
    bot,
    log,
    store,
)



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


__all__ = [
    "_clip",
    "_is_bot_owner",
    "_owner_ids_from_env",
    "_redeploy_lock",
    "_run",
    "main",
    "on_app_command_error",
    "on_ready",
    "redeploy",
]
