"""Task CRUD: ``schedule_from_rule`` (rule + ``at`` → concrete schedule
fields, shared with ``/edit task``), ``/newtask``, and ``/deletetask`` —
which also tears down a whole pitch-in / do-em-up series by name."""

from __future__ import annotations

import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from ...models import (
    discord_ts,
    first_due,
    new_id,
    now_utc,
    parse_repeat,
    pin_weekly,
    resolve_when,
    time_of_day_from,
    to_iso,
)
from ..core import NO_PINGS, bot, store
from ..helpers import config_ready, guild_config, schedule_label
from ..reactions import _delete_panels, _take_task_panels
from .lookup import (
    _find_game,
    _find_task,
    at_autocomplete,
    delete_autocomplete,
    repeat_autocomplete,
)


# ---------------------------------------------------------------------------
# Scheduling from user input (shared by /newtask and /edit task)
# ---------------------------------------------------------------------------
def schedule_from_rule(
    rule: dict,
    at: Optional[str],
    tz: ZoneInfo,
    now: dt.datetime,
    *,
    at_given: bool,
    default_tod: Optional[str] = None,
) -> dict:
    """Turn a parsed ``repeat`` rule + an ``at`` string into concrete task
    schedule fields. Raises ``ValueError`` for an explicit past one-off time.

    ``at_given`` distinguishes "user typed an `at`" from "defaulted to now": a
    one-off that lands in the past is an error *only* when the user asked for a
    specific past time; an omitted/`now` `at` simply fires on the next tick.
    ``default_tod`` is the recurring time to keep when ``at`` isn't supplied
    (used by edits that change only the repeat rule).
    """
    if rule["freq"] == "monthly" and not rule["monthdays"]:
        rule["monthdays"] = [now.astimezone(tz).day]  # bare "monthly" → today's date

    if rule["freq"] == "once":
        due = resolve_when(at, tz, now)
        if due <= now:
            explicit = at_given and (at or "").strip().lower() not in ("", "now")
            if explicit:
                raise ValueError("that time is already in the past")
            due = now  # fire on the next scheduler tick
        return {
            "recurring": False, "freq": "once", "interval_days": 0,
            "weekdays": [], "monthdays": [], "time_of_day": None, "next_due": due,
        }

    tod = time_of_day_from(at, tz, now) if at_given else (default_tod or time_of_day_from(None, tz, now))
    rule["time_of_day"] = tod
    pin_weekly(rule, at, tz, now, at_given=at_given)  # "sunday 22:00" weekly → Sundays
    return {
        "recurring": True, "freq": rule["freq"], "interval_days": rule["interval_days"],
        "weekdays": rule["weekdays"], "monthdays": rule["monthdays"],
        "time_of_day": tod, "next_due": first_due(rule, tz, now),
    }


@bot.tree.command(name="newtask", description="Create a one-off or recurring task")
@app_commands.describe(
    brief="Short text posted in the channel (required)",
    at="When/what time — now, in 2h, 18:00, tomorrow 8am, 2026-06-20 14:00 (default: now)",
    repeat="How often — once, daily, every 2 days, weekdays, mon/thu, monthly on the 1st (default: once)",
    description="Optional longer details, revealed by the ℹ️ reaction",
    bounty="Worth 2 puntos, and only someone other than you can complete it (default: off)",
)
async def newtask(
    interaction: discord.Interaction,
    brief: app_commands.Range[str, 1, 200],
    at: Optional[str] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
    bounty: bool = False,
) -> None:
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    if not config_ready(cfg):
        await interaction.response.send_message(
            "❌ Run `/joblinconfig` to set a channel and timezone first.", ephemeral=True
        )
        return

    tz, now = ZoneInfo(cfg["timezone"]), now_utc()
    try:
        sched = schedule_from_rule(parse_repeat(repeat), at, tz, now, at_given=at is not None)
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/joblinhelp` for the `at` and `repeat` formats.", ephemeral=True
        )
        return

    tid = new_id()
    task = {
        "id": tid,
        "guild_id": interaction.guild_id,
        "brief": str(brief),
        "description": description[:1500] if description else None,
        "bounty": bool(bounty),
        "recurring": sched["recurring"],
        "freq": sched["freq"],
        "interval_days": sched["interval_days"],
        "weekdays": sched["weekdays"],
        "monthdays": sched["monthdays"],
        "time_of_day": sched["time_of_day"],
        "next_due": to_iso(sched["next_due"]),
        "created_by": interaction.user.id,
        "created_at": to_iso(now),
        "pending": None,
    }
    async with store.txn() as data:
        data["tasks"][tid] = task

    fire = sched["next_due"]
    when = f"{discord_ts(fire, 'F')} ({discord_ts(fire, 'R')})"
    if sched["recurring"]:
        body = f"✅ Created **{brief}** — {schedule_label(task)}.\nFirst post: {when}"
    else:
        body = f"✅ Created one-off **{brief}**.\nDue: {when}"
    if description:
        body += "\nℹ️ Details attached."
    if bounty:
        body += "\n💰 **Bounty** — worth 2 puntos; anyone *but* you can complete it."
    body += f"\n· `{tid}` — change it any time with `/edit task`"
    # Public on purpose: the family should see when a chore is added.
    await interaction.response.send_message(body, allowed_mentions=NO_PINGS)


async def _cancel_game_message(
    channel: discord.abc.Messageable, brief: str, mid: int, *, is_doemup: bool
) -> None:
    """Make a deleted game's live post inert: strike it through as cancelled and
    strip its reactions/buttons. No puntos are awarded (delete ≠ close)."""
    pm = channel.get_partial_message(mid)
    try:
        await pm.edit(content=f"🗑️ ~~**{brief}**~~ — cancelled.",
                      view=None, allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass
    if not is_doemup:  # do-em-ups carry buttons (cleared by view=None); pitch-ins, reactions
        try:
            await pm.clear_reactions()
        except discord.HTTPException:
            pass


@bot.tree.command(
    name="deletetask",
    description="Permanently delete a task, pitch-in, or do-em-up (recurring or one-off)",
)
@app_commands.describe(task="Start typing to pick a task or game (or paste its id)")
async def deletetask(interaction: discord.Interaction, task: str) -> None:
    snap = await store.snapshot()
    found = _find_task(snap, interaction.guild_id, task)
    removed = None
    panels: list = []
    if found:
        async with store.txn() as data:
            tid = found["id"]
            t = data["tasks"].get(tid)
            if t and str(t["guild_id"]) == str(interaction.guild_id):
                pending = t.get("pending")
                if pending:
                    for mid in pending.get("message_ids", []):
                        data["messages"].pop(str(mid), None)
                for mid, rec in list(data["undo"].items()):
                    if rec.get("task_id") == tid:
                        data["undo"].pop(mid, None)
                for mid, rec in list(data["requeue"].items()):
                    if rec.get("task_id") == tid:
                        data["requeue"].pop(mid, None)
                for mid, rec in list(data["claps"].items()):
                    if rec.get("task_id") == tid:
                        data["claps"].pop(mid, None)
                panels = _take_task_panels(data, tid)
                removed = data["tasks"].pop(tid, None)
        await _delete_panels(panels)
    else:
        # Not a task — it may be a pitch-in or do-em-up (kills the whole series).
        kind, game = _find_game(snap, interaction.guild_id, task)
        if game:
            section = "pitchins" if kind == "pitchin" else "doemups"
            live_mid = None
            async with store.txn() as data:
                g = data[section].get(game["id"])
                if g and str(g["guild_id"]) == str(interaction.guild_id):
                    live_mid = g.get("message_id")
                    data["game_messages"].pop(str(live_mid), None)
                    for mid, rec in list(data["claps"].items()):
                        if rec.get("task_id") == game["id"]:
                            data["claps"].pop(mid, None)
                    removed = data[section].pop(game["id"], None)
            if removed and live_mid:
                ch = (bot.get_channel(int(removed["channel_id"]))
                      if removed.get("channel_id") else None)
                if ch is not None:
                    await _cancel_game_message(
                        ch, removed["brief"], live_mid, is_doemup=(kind == "doemup")
                    )
    if removed:
        await interaction.response.send_message(
            f"🗑️ Deleted **{removed['brief']}**.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ Not found. Use `/listtasks` to see current tasks.", ephemeral=True
        )


# The friendly live-preview autocompletes are shared with the other commands.
newtask.autocomplete("at")(at_autocomplete)
newtask.autocomplete("repeat")(repeat_autocomplete)
deletetask.autocomplete("task")(delete_autocomplete)


__all__ = [
    "_cancel_game_message",
    "deletetask",
    "newtask",
    "schedule_from_rule",
]
