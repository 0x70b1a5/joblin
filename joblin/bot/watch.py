"""Watches — let other things on this box raise a flag in Discord.

``WATCHES`` is a comma-separated list of ``kind:arg`` specs, e.g.
``file:/srv/app/job_failed.flag``. Each tick, every spec's *probe* runs and
reports either healthy (``None``) or a :class:`Finding`:

* finding new or changed since we last alerted -> post its body to every ready
  guild's channel, pinging the bot owner;
* healthy again after we alerted -> post a short all-clear (no ping).

Built-in kinds:

* ``file:<path>`` — alert while the file exists; the body is its contents, and
  rewriting the file (new mtime/size) alerts again.

Adding a kind is one decorated coroutine in this module::

    @watch_kind("name")
    async def _probe_name(arg: str) -> Finding | None: ...

``Finding.sig`` identifies the alerting state — a different sig re-alerts, the
same sig stays quiet. Restart-safe like the rest of the scheduler: the
last-alerted sig per spec is persisted in ``store["watches"]``, so a restart
never re-alerts on something already reported, and a problem that arose while
the bot was down is reported on the first tick. The sig is recorded only after
a successful post, so a failed send simply retries next tick. Quiet time defers
alerts (nothing is recorded) until the switch lifts.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import os
from typing import Awaitable, Callable, Optional

import discord

from .core import bot, log, store
from .helpers import config_ready

# Room left for the header line inside Discord's 2000-char message cap.
MAX_BODY = 1500


@dataclasses.dataclass(frozen=True)
class Finding:
    sig: str   # identity of the alerting state; a new sig means alert again
    body: str  # what to post (clipped and fenced by the engine)


Probe = Callable[[str], Awaitable[Optional[Finding]]]
WATCH_KINDS: dict[str, Probe] = {}


def watch_kind(name: str) -> Callable[[Probe], Probe]:
    def register(probe: Probe) -> Probe:
        WATCH_KINDS[name] = probe
        return probe
    return register


# ---------------------------------------------------------------------------
# Built-in kinds
# ---------------------------------------------------------------------------
@watch_kind("file")
async def _probe_file(path: str) -> Optional[Finding]:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            body = f.read(MAX_BODY + 1)
    except OSError as e:
        body = f"(could not read file: {e})"
    return Finding(sig=f"{st.st_mtime_ns}:{st.st_size}", body=body)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
_warned: set[str] = set()


def watch_specs() -> list[tuple[str, Probe, str]]:
    """``(spec, probe, arg)`` for each well-formed ``WATCHES`` entry. Unknown
    kinds are logged once and skipped."""
    out = []
    for spec in (s.strip() for s in (os.getenv("WATCHES") or "").split(",")):
        if not spec:
            continue
        kind, _, arg = spec.partition(":")
        probe = WATCH_KINDS.get(kind.strip())
        if probe is None or not arg.strip():
            if spec not in _warned:
                _warned.add(spec)
                log.warning("WATCHES: ignoring %r (known kinds: %s)",
                            spec, ", ".join(sorted(WATCH_KINDS)))
            continue
        out.append((spec, probe, arg.strip()))
    return out


def owner_id() -> Optional[int]:
    raw = os.getenv("OWNER_IDS") or os.getenv("OWNER_ID") or ""
    for part in raw.split(","):
        if part.strip().isdigit():
            return int(part.strip())
    app = getattr(bot, "application", None)
    if app is not None:
        owner = app.team.owner if app.team and app.team.owner else app.owner
        if owner is not None:
            return owner.id
    return None


def fence(body: str) -> str:
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n…"
    # Keep the body from closing our code block early.
    return body.replace("```", "ʼʼʼ").strip() or "(empty)"


def alert_text(spec: str, body: str, ping: Optional[int]) -> str:
    who = f"<@{ping}> " if ping else ""
    return f"⚠️ {who}**Alert:** `{spec}`\n```\n{fence(body)}\n```"


def clear_text(spec: str) -> str:
    return f"✅ **All clear:** `{spec}`"


async def run_watches(now: dt.datetime, snap: dict) -> None:
    """Called once per scheduler tick; a no-op unless WATCHES is set."""
    specs = watch_specs()
    if not specs:
        return
    channels = []
    for cfg in (snap.get("configs") or {}).values():
        if config_ready(cfg) and not cfg.get("quiet"):
            channel = bot.get_channel(int(cfg["channel_id"]))
            if channel is not None:
                channels.append(channel)
    if not channels:
        return  # quiet time or cache not warm yet — retry next tick

    seen = snap.get("watches") or {}
    for spec, probe, arg in specs:
        try:
            finding = await probe(arg)
            sig = finding.sig if finding else None
            if sig == seen.get(spec):
                continue
            if finding is None:
                content, mentions = clear_text(spec), discord.AllowedMentions.none()
            else:
                ping = owner_id()
                content = alert_text(spec, finding.body, ping)
                mentions = discord.AllowedMentions(
                    everyone=False, roles=False,
                    users=[discord.Object(id=ping)] if ping else False)
            for channel in channels:
                await channel.send(content, allowed_mentions=mentions)
            async with store.txn() as data:
                w = data.setdefault("watches", {})
                if sig is None:
                    w.pop(spec, None)
                else:
                    w[spec] = sig
        except Exception:  # never let a watch error kill the scheduler loop
            log.exception("watch failed for %s", spec)


__all__ = [
    "Finding",
    "WATCH_KINDS",
    "run_watches",
    "watch_kind",
]
