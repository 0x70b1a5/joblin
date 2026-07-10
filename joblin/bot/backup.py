"""Nightly self-backup — a poor-man's offsite copy posted to the guild channel.

At ~23:59 guild-local, **if anything got logged that day** (the completion
log's hash moved since the last backup), zip the two state files and post them
to the configured channel as an attachment, then auto-post the current
leaderboard. The channel becomes a dated archive of the economy's source of
truth — restore a data dir by un-zipping the latest attachment.

Two existing properties make this honest:

* Writes are atomic (temp + ``fsync`` + ``os.replace``), so reading the files at
  any instant yields a consistent snapshot — no need to quiesce the bot.
* The completion log is the source of truth for every derived stat (stars,
  trinkets), so the *trigger* is "did the log change" and the *payload* is the
  whole state (``store.json`` + ``completions.jsonl``).

We hash the log **alone**, not ``store.json``: the latter churns every time a
task merely *fires* (``next_due``/``pending`` move) and also holds the backup
bookkeeping we rewrite below, so hashing it would trip the gate almost every
night. The log changes only on a real punto event — a completion logged, or an
undo voiding one — which is exactly "anything got logged".

Restart-safe like the rest of the scheduler: the next run is a *persisted*
instant (``next_backup_at`` in the guild config) compared against ``now`` each
tick, never an in-memory timer. The deadline is rolled forward **before** the
Discord upload so a slow post can't double-fire on the next tick; the
change-detection baseline (``last_backup_sig``) is advanced only **after** a
successful post, so a crash mid-upload simply retries at the next nightly slot
(the append-only log means that next zip is a superset — nothing is lost).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import zipfile
from zoneinfo import ZoneInfo

import discord

from ..models import _next_clock, from_iso, to_iso
from .core import NIGHTLY_HOUR, NIGHTLY_MINUTE, NO_PINGS, bot, log, store
from .helpers import config_ready
from .scoring import build_leaderboard

# When (guild-local wall clock) the nightly backup fires — defined in core.py
# so scoring's day-over-day frame rolls at the same instant. The 30s scheduler
# tick means it actually posts within ~30s after this minute begins.
BACKUP_HOUR, BACKUP_MINUTE = NIGHTLY_HOUR, NIGHTLY_MINUTE


# ---------------------------------------------------------------------------
# Pure helpers (no Discord, no lock) — easy to unit-test
# ---------------------------------------------------------------------------
def _read_state() -> tuple[bytes, bytes]:
    """The on-disk state files as raw bytes ``(store.json, completions.jsonl)``.

    Read back-to-back with no ``await`` between them so — on the single event
    loop — the pair is a consistent snapshot of what's on disk."""
    store_bytes = store.path.read_bytes() if store.path.exists() else b"{}"
    log_bytes = store.log_path.read_bytes() if store.log_path.exists() else b""
    return store_bytes, log_bytes


def _log_sig(log_bytes: bytes) -> str:
    """Fingerprint of the completion log — the thing that means 'got logged'."""
    return hashlib.sha256(log_bytes).hexdigest()


def _build_zip(store_bytes: bytes, log_bytes: bytes) -> bytes:
    """A deflate-compressed zip holding both state files under stable names."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("store.json", store_bytes)
        z.writestr("completions.jsonl", log_bytes)
    return buf.getvalue()


def _next_backup_at(now: dt.datetime, tz: ZoneInfo) -> str:
    """Persisted UTC ISO instant of the next 23:59 local (DST-aware)."""
    return to_iso(_next_clock(now, tz, BACKUP_HOUR, BACKUP_MINUTE))


# ---------------------------------------------------------------------------
# The nightly routine
# ---------------------------------------------------------------------------
async def run_daily_backups(now: dt.datetime, snap: dict) -> None:
    """Called once per scheduler tick. For each fully-configured guild, fire the
    nightly backup when its persisted ``next_backup_at`` has passed."""
    for gid_str, cfg in list(snap["configs"].items()):
        if not config_ready(cfg):
            continue
        try:
            tz = ZoneInfo(cfg["timezone"])
        except Exception:
            continue

        nxt = cfg.get("next_backup_at")
        # Arm the schedule the first time we see a ready guild (also covers
        # configs that predate this feature). Wait for the next 23:59 rather
        # than backing up the instant a guild is configured.
        if not nxt:
            async with store.txn() as data:
                c = data["configs"].get(gid_str)
                if c is not None and not c.get("next_backup_at"):
                    c["next_backup_at"] = _next_backup_at(now, tz)
            continue

        try:
            if now < from_iso(nxt):
                continue
        except Exception:
            continue

        channel = bot.get_channel(int(cfg["channel_id"]))
        if channel is None:
            # Channel cache not warm yet — leave the deadline so we retry next
            # tick rather than silently dropping the day.
            continue

        try:
            await _do_backup(int(gid_str), cfg, channel, now, tz)
        except Exception:  # never let a backup error kill the scheduler loop
            log.exception("daily backup failed for guild %s", gid_str)


async def _do_backup(guild_id: int, cfg: dict, channel: discord.abc.Messageable,
                     now: dt.datetime, tz: ZoneInfo) -> None:
    # Roll the deadline forward FIRST (and read the prior baseline) so a slow
    # upload can't double-fire on the next tick.
    async with store.txn() as data:
        c = data["configs"].get(str(guild_id))
        if c is None:
            return
        c["next_backup_at"] = _next_backup_at(now, tz)
        prev_sig = c.get("last_backup_sig")

    store_bytes, log_bytes = _read_state()
    cur_sig = _log_sig(log_bytes)
    if cur_sig == prev_sig:
        return  # nothing logged since the last backup — stay quiet

    stamp = now.astimezone(tz).strftime("%Y-%m-%d")
    n_events = log_bytes.count(b"\n")
    kib = (len(store_bytes) + len(log_bytes) + 1023) // 1024
    caption = (
        f"🗄️ **Nightly backup — {stamp}** · {n_events} logged event"
        f"{'' if n_events == 1 else 's'} · ~{kib} KB\n"
        "_The whole economy in one zip — save it somewhere off this server._"
    )
    archive = _build_zip(store_bytes, log_bytes)
    file = discord.File(io.BytesIO(archive), filename=f"joblin-backup-{stamp}.zip")
    await channel.send(content=caption, file=file, allowed_mentions=NO_PINGS)

    # Auto-post the current month's leaderboard alongside the snapshot.
    records = store.read_completions()
    msg, empty = build_leaderboard(records, guild_id, cfg)
    if not empty:
        await channel.send(msg, allowed_mentions=NO_PINGS)

    # Advance the change-detection baseline only after a successful post, so a
    # crash before this point just retries (harmlessly) at the next slot.
    async with store.txn() as data:
        c = data["configs"].get(str(guild_id))
        if c is not None:
            c["last_backup_sig"] = cur_sig


__all__ = [
    "BACKUP_HOUR",
    "BACKUP_MINUTE",
    "run_daily_backups",
]
