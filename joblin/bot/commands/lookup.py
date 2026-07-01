"""Free-text → thing resolution, shared across the slash commands.

Two families live here, both answering "what did the user mean by this text?":

* **Finders** resolve a command argument against the store — by id (the
  autocomplete value), then an exact brief match, then a substring — so a
  pasted id *or* a typed name both work.
* **Autocompletes** are the live pickers Discord shows while typing: resolved
  previews of `at`/`repeat` strings, and task/game lists filtered by the text
  so far.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from ...models import (
    UTC,
    describe_repeat,
    now_utc,
    parse_repeat,
    recurrence_of,
    resolve_when,
)
from ..core import store
from ..helpers import guild_config, schedule_label


# ---------------------------------------------------------------------------
# Finders — command-argument text to a stored task / game
# ---------------------------------------------------------------------------
def _find_task(snap: dict, guild_id: int, text: str) -> Optional[dict]:
    """Resolve a task by id (the autocomplete value) or, failing that, by an
    exact then substring match on its brief — so a pasted id *or* a typed name
    both work."""
    t = snap["tasks"].get(text)
    if t and str(t["guild_id"]) == str(guild_id):
        return t
    needle = (text or "").strip().lower()
    mine = [t for t in snap["tasks"].values() if str(t["guild_id"]) == str(guild_id)]
    for t in mine:
        if t["id"].lower() == needle or t["brief"].strip().lower() == needle:
            return t
    for t in mine:
        if needle and needle in t["brief"].lower():
            return t
    return None


def _find_game(snap: dict, guild_id: int, text: str) -> tuple[Optional[str], Optional[dict]]:
    """Resolve a pitch-in or do-em-up the same way :func:`_find_task` resolves a
    task — by id, then exact, then substring brief. Returns ``(kind, game)`` with
    ``kind`` ∈ {"pitchin", "doemup"}, or ``(None, None)``."""
    for kind, section in (("pitchin", "pitchins"), ("doemup", "doemups")):
        g = snap[section].get(text)
        if g and str(g["guild_id"]) == str(guild_id):
            return kind, g
    needle = (text or "").strip().lower()
    games = [("pitchin", g) for g in snap["pitchins"].values()
             if str(g["guild_id"]) == str(guild_id)]
    games += [("doemup", g) for g in snap["doemups"].values()
              if str(g["guild_id"]) == str(guild_id)]
    for matcher in (
        lambda g: g["id"].lower() == needle or g["brief"].strip().lower() == needle,
        lambda g: bool(needle) and needle in g["brief"].lower(),
    ):
        for kind, g in games:
            if matcher(g):
                return kind, g
    return None, None


def _find_game_in(snap: dict, guild_id: int, section: str, text: str) -> Optional[dict]:
    """Resolve a single section's game by id, then exact, then substring brief —
    the section-scoped twin of :func:`_find_game`, so ``/edit pitchin`` only ever
    matches pitch-ins (and ``/edit doemup`` only do-em-ups)."""
    g = snap[section].get(text)
    if g and str(g["guild_id"]) == str(guild_id):
        return g
    needle = (text or "").strip().lower()
    mine = [g for g in snap[section].values() if str(g["guild_id"]) == str(guild_id)]
    for g in mine:
        if g["id"].lower() == needle or g["brief"].strip().lower() == needle:
            return g
    for g in mine:
        if needle and needle in g["brief"].lower():
            return g
    return None


# ---------------------------------------------------------------------------
# Autocomplete helpers
# ---------------------------------------------------------------------------
async def _guild_tz(interaction: discord.Interaction) -> ZoneInfo:
    """Best-effort timezone for live autocomplete previews."""
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    if cfg and cfg.get("timezone"):
        try:
            return ZoneInfo(cfg["timezone"])
        except Exception:
            pass
    return UTC


def _human_until(now: dt.datetime, due: dt.datetime) -> str:
    secs = (due - now).total_seconds()
    if -60 <= secs <= 60:
        return "now"
    past, secs = secs < 0, abs(secs)
    if secs < 3600:
        v, u = round(secs / 60), "min"
    elif secs < 86400:
        v = round(secs / 3600)
        u = "hour" if v == 1 else "hours"
    else:
        v = round(secs / 86400)
        u = "day" if v == 1 else "days"
    return f"{v} {u} ago" if past else f"in {v} {u}"


def _when_label(due: dt.datetime, tz: ZoneInfo, now: dt.datetime, *, head: bool = True) -> str:
    local = due.astimezone(tz)
    prefix = "📅 " if head else ""
    return f"{prefix}{local:%a %b %d · %H:%M} ({_human_until(now, due)})"


def _dedup(choices: list[app_commands.Choice]) -> list[app_commands.Choice]:
    seen, out = set(), []
    for c in choices:
        if c.value in seen:
            continue
        seen.add(c.value)
        out.append(c)
    return out[:25]


def _repeat_label(rule: dict, now: dt.datetime) -> str:
    if rule["freq"] == "monthly" and not rule["monthdays"]:
        rule = {**rule, "monthdays": [now.day]}  # preview only; real day set at run time
    return "one-off" if rule["freq"] == "once" else describe_repeat(rule)


# ---------------------------------------------------------------------------
# The autocompletes themselves
# ---------------------------------------------------------------------------
async def at_autocomplete(interaction: discord.Interaction, current: str):
    tz, now = await _guild_tz(interaction), now_utc()
    cur = current.strip()
    choices: list[app_commands.Choice] = []
    if cur:
        try:
            choices.append(app_commands.Choice(
                name=_when_label(resolve_when(cur, tz, now), tz, now)[:100], value=cur[:100]))
        except ValueError:
            choices.append(app_commands.Choice(
                name="⚠️ e.g. now · in 2h · 18:00 · tomorrow 8am · Jun 20 14:00", value=cur[:100]))
    for text in ("now", "in 1 hour", "in 30 minutes", "tonight", "tomorrow 08:00",
                 "this saturday 09:00", "next monday 18:00"):
        if cur and cur.lower() not in text.lower():
            continue
        try:
            due = resolve_when(text, tz, now)
        except ValueError:
            continue
        choices.append(app_commands.Choice(
            name=f"{text}  →  {_when_label(due, tz, now, head=False)}"[:100], value=text))
    return _dedup(choices)


async def repeat_autocomplete(interaction: discord.Interaction, current: str):
    now = now_utc()
    cur = current.strip()
    choices: list[app_commands.Choice] = []
    if cur:
        try:
            choices.append(app_commands.Choice(
                name=f"🔁 {_repeat_label(parse_repeat(cur), now)}"[:100], value=cur[:100]))
        except ValueError:
            choices.append(app_commands.Choice(
                name="⚠️ e.g. daily · every 2 days · weekdays · mon,thu · monthly on the 1st",
                value=cur[:100]))
    for text in ("once", "daily", "every 2 days", "every 3 days", "weekdays", "weekends",
                 "mon,wed,fri", "every tuesday", "weekly", "monthly", "monthly on the 1st",
                 "monthly on the 1st,15th"):
        if cur and cur.lower() not in text.lower():
            continue
        try:
            choices.append(app_commands.Choice(
                name=f"{text}  →  {_repeat_label(parse_repeat(text), now)}"[:100], value=text))
        except ValueError:
            continue
    return _dedup(choices)


async def task_autocomplete(interaction: discord.Interaction, current: str):
    snap = await store.snapshot()
    cur = current.lower()
    out = []
    for tid, t in snap["tasks"].items():
        if str(t["guild_id"]) != str(interaction.guild_id):
            continue
        label = f"{t['brief']} ({schedule_label(t)})"
        if cur in label.lower() or cur in tid.lower():
            out.append(app_commands.Choice(name=label[:100], value=tid))
        if len(out) >= 25:
            break
    return out


async def delete_autocomplete(interaction: discord.Interaction, current: str):
    """Like :func:`task_autocomplete`, but also lists active pitch-ins / do-em-ups
    so ``/deletetask`` can tear those series down too."""
    out = await task_autocomplete(interaction, current)
    cur = current.lower()
    snap = await store.snapshot()
    for icon, section in (("🤝", "pitchins"), ("💪", "doemups")):
        for gid, g in snap[section].items():
            if len(out) >= 25:
                break
            if str(g["guild_id"]) != str(interaction.guild_id):
                continue
            sched = describe_repeat(recurrence_of(g)) if g.get("recurring") else "one-off"
            label = f"{icon} {g['brief']} ({sched})"
            if cur in label.lower() or cur in gid.lower():
                out.append(app_commands.Choice(name=label[:100], value=gid))
    return out[:25]


def _game_event_autocomplete(section: str, icon: str):
    """Build an autocomplete that lists just one section's games (pitch-ins or
    do-em-ups), so each /edit subcommand offers only its own kind."""
    async def _ac(interaction: discord.Interaction, current: str):
        snap = await store.snapshot()
        cur = current.lower()
        out: list[app_commands.Choice] = []
        for gid, g in snap[section].items():
            if str(g["guild_id"]) != str(interaction.guild_id):
                continue
            sched = describe_repeat(recurrence_of(g)) if g.get("recurring") else "one-off"
            label = f"{icon} {g['brief']} ({sched})"
            if cur in label.lower() or cur in gid.lower():
                out.append(app_commands.Choice(name=label[:100], value=gid))
            if len(out) >= 25:
                break
        return out
    return _ac


__all__ = [
    "_dedup",
    "_find_game",
    "_find_game_in",
    "_find_task",
    "_game_event_autocomplete",
    "_guild_tz",
    "_human_until",
    "_repeat_label",
    "_when_label",
    "at_autocomplete",
    "delete_autocomplete",
    "repeat_autocomplete",
    "task_autocomplete",
]
