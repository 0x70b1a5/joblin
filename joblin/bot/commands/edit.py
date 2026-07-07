"""The ``/edit`` group — one subcommand per type (task / pitchin / doemup),
so each only ever shows its own fields: no bounty on a pitch-in, no
max_scorers on a task. The two game subcommands share one engine
(:func:`apply_game_edit`, also the web UI's game-edit backend); the group is
registered on the tree at the bottom of this module, once all three
subcommands are defined."""

from __future__ import annotations

import json
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from ...models import (
    EMOJI_FLEX,
    EMOJI_HANDSHAKE,
    UTC,
    describe_repeat,
    discord_ts,
    from_iso,
    now_utc,
    parse_repeat,
    recurrence_of,
    render_doemup,
    render_pitchin,
    resolve_when,
    time_of_day_from,
    to_iso,
)
from ..core import NO_PINGS, bot, store
from ..games import _game_next_round, make_doemup_view
from ..helpers import config_ready, guild_config, schedule_label
from .games import _game_recurrence_from
from .lookup import (
    _find_game_in,
    _find_task,
    _game_event_autocomplete,
    at_autocomplete,
    repeat_autocomplete,
    task_autocomplete,
)
from .tasks import schedule_from_rule


edit = app_commands.Group(name="edit", description="Edit a task, pitch-in, or do-em-up")


@edit.command(name="task", description="Edit a task's text, time, repeat, or bounty")
@app_commands.describe(
    task="The task to edit — pick from the list, or paste its id",
    brief="New short text (optional)",
    at="New time/date — now, in 2h, 18:00, tomorrow 8am, 2026-06-20 14:00 (optional)",
    repeat="New repeat — once, daily, every 2 days, weekdays, mon/thu, monthly on the 1st (optional)",
    description="New longer details (optional)",
    clear_description="Remove the existing long description",
    bounty="Make this a 2-punto bounty the creator can't complete (or turn it off)",
)
async def edit_task(
    interaction: discord.Interaction,
    task: str,
    brief: Optional[app_commands.Range[str, 1, 200]] = None,
    at: Optional[str] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
    clear_description: bool = False,
    bounty: Optional[bool] = None,
) -> None:
    snap = await store.snapshot()
    live = _find_task(snap, interaction.guild_id, task)
    if not live:
        await interaction.response.send_message(
            "❌ Task not found. Use `/listtasks` to see ids.", ephemeral=True
        )
        return
    tid = live["id"]

    if (brief is None and at is None and repeat is None and description is None
            and not clear_description and bounty is None):
        await interaction.response.send_message(
            "❌ Nothing to change — set at least one of brief, at, repeat, description, or bounty.",
            ephemeral=True,
        )
        return

    cfg = guild_config(snap, interaction.guild_id)
    recompute = at is not None or repeat is not None
    if recompute and not config_ready(cfg):
        await interaction.response.send_message(
            "❌ Set a timezone with `/joblinconfig` before changing the schedule.", ephemeral=True
        )
        return

    now = now_utc()
    sched = None
    if recompute:
        tz = ZoneInfo(cfg["timezone"])
        new_rule = parse_repeat(repeat) if repeat is not None else recurrence_of(live)
        try:
            sched = schedule_from_rule(
                new_rule, at, tz, now, at_given=at is not None, default_tod=live.get("time_of_day")
            )
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ {e}\nSee `/joblinhelp` for the `at` and `repeat` formats.", ephemeral=True
            )
            return

    updated = None
    pending_note = False
    async with store.txn() as data:
        t = data["tasks"].get(tid)
        if t:
            if brief is not None:
                t["brief"] = str(brief)
            if clear_description:
                t["description"] = None
            elif description is not None:
                t["description"] = description[:1500]
            if bounty is not None:
                t["bounty"] = bool(bounty)
            if sched is not None:
                t["recurring"] = sched["recurring"]
                t["freq"] = sched["freq"]
                t["interval_days"] = sched["interval_days"]
                t["weekdays"] = sched["weekdays"]
                t["monthdays"] = sched["monthdays"]
                t["time_of_day"] = sched["time_of_day"]
                if t.get("pending"):
                    pending_note = True  # don't disturb a live occurrence
                else:
                    t["next_due"] = to_iso(sched["next_due"])
            updated = json.loads(json.dumps(t))

    if not updated:
        await interaction.response.send_message("❌ Task not found.", ephemeral=True)
        return

    body = f"✏️ Updated **{updated['brief']}** — {schedule_label(updated)}."
    if pending_note:
        body += "\n(A reminder is live now; the new schedule applies from the next cycle.)"
    elif sched is not None and updated.get("next_due"):
        nd = from_iso(updated["next_due"])
        body += f"\nNext post: {discord_ts(nd, 'F')} ({discord_ts(nd, 'R')})"
    if bounty is not None:
        body += (
            "\n💰 Now a **bounty** — worth 2 puntos; you can't complete it yourself."
            if bounty else "\n💰 Bounty removed — back to a normal 1-punto chore."
        )
    # Public on purpose: shared chores changing is something the family should see.
    await interaction.response.send_message(body, allowed_mentions=NO_PINGS)


# --- /edit pitchin and /edit doemup (shared engine) ------------------------
def _set_game_recurrence(g: dict, rec: Optional[dict]) -> None:
    """Write the recurrence columns onto a game from a rule (or None for a
    one-off), leaving next_due / duration_secs / message_id to the caller."""
    if rec:
        g.update({"recurring": True, "freq": rec["freq"],
                  "interval_days": rec.get("interval_days", 0),
                  "weekdays": rec.get("weekdays", []),
                  "monthdays": rec.get("monthdays", []),
                  "time_of_day": rec["time_of_day"]})
    else:
        g.update({"recurring": False, "freq": "once", "interval_days": 0,
                  "weekdays": [], "monthdays": [], "time_of_day": None})


# (section, close field, cap field, cap ceiling) per game kind — the only
# structural differences between a pitch-in and a do-em-up edit.
_GAME_SURFACE = {
    "pitchin": ("pitchins", "expires_at", "max_scorers", 100),
    "doemup": ("doemups", "deadline", "point_limit", 100000),
}


_UNSET = object()  # "no schedule change" — distinct from an explicit None


async def apply_game_edit(
    guild_id: int, kind: str, event_id: str, fields: dict
) -> tuple[Optional[dict], Optional[str], Optional[str]]:
    """Edit a pitch-in / do-em-up ("pitchin" | "doemup") by id: the engine
    behind /edit pitchin, /edit doemup, and the web UI. Key *presence* in
    ``fields`` is intent (brief, description — empty clears, at, repeat,
    close, puntos, cap), mirroring the web's task edit. A schedule change
    recomputes the next open slot for a scheduled/dormant round; for a live
    round it applies from the next round (the open post is left alone except
    for an explicit close-time change, and is re-rendered so any text/puntos
    change shows immediately). Returns (updated, note, error)."""
    section, close_field, cap_field, cap_max = _GAME_SURFACE[kind]
    noun = "pitch-in" if kind == "pitchin" else "do-em-up"
    snap = await store.snapshot()
    event = snap[section].get(event_id)
    if not event or str(event["guild_id"]) != str(guild_id):
        return None, None, f"{noun.capitalize()} not found."
    if not any(k in fields for k in
               ("brief", "description", "at", "repeat", "close", "puntos", "cap")):
        return None, None, "Nothing to change — set at least one field."

    puntos = cap = None
    if fields.get("puntos") is not None:
        try:
            puntos = int(fields["puntos"])
        except (TypeError, ValueError):
            puntos = 0
        if not 1 <= puntos <= 100:
            return None, None, "puntos must be between 1 and 100."
    if fields.get("cap") is not None:
        try:
            cap = int(fields["cap"])
        except (TypeError, ValueError):
            cap = 0
        if not 1 <= cap <= cap_max:
            return None, None, f"the cap must be between 1 and {cap_max}."

    cfg = guild_config(snap, guild_id)
    recompute = "at" in fields or "repeat" in fields or "close" in fields
    if recompute and not config_ready(cfg):
        return None, None, "Set a timezone with /joblinconfig before changing the schedule."
    tz = ZoneInfo(cfg["timezone"]) if (cfg and cfg.get("timezone")) else UTC
    now = now_utc()

    at = fields.get("at")
    new_rec = None
    rec_changed = "at" in fields or "repeat" in fields
    if rec_changed:
        try:
            if "repeat" in fields:
                new_rec = _game_recurrence_from(fields["repeat"], tz, now, at)
            else:  # only `at` changed — keep the existing rule, move its slot
                new_rec = recurrence_of(event) if event.get("recurring") else None
                if new_rec is not None and at is not None:
                    new_rec = {**new_rec, "time_of_day": time_of_day_from(at, tz, now)}
        except ValueError as e:
            return None, None, str(e)

    updated = None
    err = None
    async with store.txn() as data:
        g = data[section].get(event_id)
        if g and str(g["guild_id"]) == str(guild_id):
            live = bool(g.get("message_id"))

            # Resolve every fallible instant BEFORE touching g: in-memory data
            # is canonical during a run, so a mutation made before an error
            # would stick (and get flushed by the next clean txn) — validate
            # everything first, commit only when the whole edit is good.
            new_next_due = _UNSET
            if rec_changed and not live:  # scheduled/dormant — next open slot moves
                if new_rec:
                    probe = {**g}
                    _set_game_recurrence(probe, new_rec)
                    new_next_due = to_iso(_game_next_round(probe, tz, now))
                elif at is not None:
                    try:
                        start = resolve_when(at, tz, now)
                        if start <= now:
                            err = "that start time is already in the past"
                        else:
                            new_next_due = to_iso(start)
                    except ValueError as e:
                        err = str(e)

            new_close = None
            if err is None and fields.get("close") is not None:
                nd_iso = new_next_due if new_next_due is not _UNSET else g.get("next_due")
                base = now if live else (from_iso(nd_iso) if nd_iso else now)
                try:
                    new_close = resolve_when(str(fields["close"]), tz, base)
                    if new_close <= base:
                        err = "that close time is already in the past"
                except ValueError as e:
                    err = str(e)

            if err is None:
                if "brief" in fields:
                    brief = str(fields["brief"] or "").strip()[:200]
                    if brief:
                        g["brief"] = brief
                if "description" in fields:
                    desc = str(fields["description"] or "").strip()
                    g["description"] = desc[:1000] if desc else None
                if puntos is not None:
                    g["points_each"] = puntos
                if cap is not None:
                    g[cap_field] = cap
                if rec_changed:
                    _set_game_recurrence(g, new_rec)
                    if new_next_due is not _UNSET:
                        g["next_due"] = new_next_due
                if new_close is not None:
                    if live:
                        g[close_field] = to_iso(new_close)
                    if g.get("recurring") or not live:
                        g["duration_secs"] = max(1, int((new_close - base).total_seconds()))
                updated = json.loads(json.dumps(g))

    if err:
        return None, None, err
    if not updated:
        return None, None, f"{noun.capitalize()} not found."

    # Re-render a live post so any text/puntos/close change shows immediately.
    live_mid = updated.get("message_id")
    if live_mid:
        channel = (bot.get_channel(int(updated["channel_id"]))
                   if updated.get("channel_id") else None)
        if channel is not None:
            try:
                if kind == "pitchin":
                    await channel.get_partial_message(int(live_mid)).edit(
                        content=render_pitchin(updated), allowed_mentions=NO_PINGS)
                else:
                    await channel.get_partial_message(int(live_mid)).edit(
                        content=render_doemup(updated),
                        view=make_doemup_view(updated["id"]), allowed_mentions=NO_PINGS)
            except discord.HTTPException:
                pass

    note = None
    if live_mid and rec_changed:
        note = "A round is live in Discord now; the new schedule applies from the next round."
    return updated, note, None


async def _apply_game_edit(
    interaction: discord.Interaction, *, kind: str, section: str, event_text: str,
    brief: Optional[str], at: Optional[str], repeat: Optional[str],
    description: Optional[str], clear_description: bool,
    close: Optional[str], puntos: Optional[int], cap: Optional[int],
) -> None:
    """Interaction front-end for /edit pitchin and /edit doemup: resolve the
    free-text event, translate the options into :func:`apply_game_edit`'s
    presence-is-intent fields, and word the outcome for Discord."""
    noun = "pitch-in" if kind == "pitchin" else "do-em-up"
    snap = await store.snapshot()
    event = _find_game_in(snap, interaction.guild_id, section, event_text)
    if not event:
        await interaction.response.send_message(
            f"❌ {noun.capitalize()} not found. Use `/listtasks` to see ids.", ephemeral=True)
        return

    fields: dict = {}
    if brief is not None:
        fields["brief"] = str(brief)
    if clear_description:
        fields["description"] = ""
    elif description is not None:
        fields["description"] = description
    if at is not None:
        fields["at"] = at
    if repeat is not None:
        fields["repeat"] = repeat
    if close is not None:
        fields["close"] = close
    if puntos is not None:
        fields["puntos"] = int(puntos)
    if cap is not None:
        fields["cap"] = int(cap)
    if not fields:
        await interaction.response.send_message(
            "❌ Nothing to change — set at least one field.", ephemeral=True)
        return

    updated, _note, err = await apply_game_edit(
        interaction.guild_id, kind, event["id"], fields)
    if err:
        await interaction.response.send_message(
            f"❌ {err}\nSee `/joblinhelp` for the formats.", ephemeral=True)
        return

    rec_changed = at is not None or repeat is not None
    live_mid = updated.get("message_id")
    sched = describe_repeat(recurrence_of(updated)) if updated.get("recurring") else "one-off"
    tail = ""
    if updated.get("next_due"):
        nd = from_iso(updated["next_due"])
        tail = f" · next round {discord_ts(nd, 'F')} ({discord_ts(nd, 'R')})"
    elif live_mid and rec_changed:
        tail = " · live now — the new schedule applies from the next round"
    await interaction.response.send_message(
        f"✏️ Updated **{updated['brief']}** ({sched}){tail}.",
        ephemeral=True, allowed_mentions=NO_PINGS)


@edit.command(name="pitchin", description="Edit a pitch-in's text, schedule, puntos, or cap")
@app_commands.describe(
    event="The pitch-in to edit — pick from the list, or paste its id",
    brief="New short text (optional)",
    at="New open time / recurring slot — 06:00, tonight, tomorrow 8am (optional)",
    repeat="New repeat — once, daily, weekdays, mon/thu, monthly on the 1st (optional)",
    description="New extra details (optional)",
    clear_description="Remove the existing description",
    expires="New close time for the (next) round — in 5m, 18:00, tonight (optional)",
    puntos="New puntos each pitcher-inner earns (optional)",
    max_scorers="New cap: only the first N score (optional)",
)
async def edit_pitchin(
    interaction: discord.Interaction,
    event: str,
    brief: Optional[app_commands.Range[str, 1, 200]] = None,
    at: Optional[str] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
    clear_description: bool = False,
    expires: Optional[str] = None,
    puntos: Optional[app_commands.Range[int, 1, 100]] = None,
    max_scorers: Optional[app_commands.Range[int, 1, 100]] = None,
) -> None:
    await _apply_game_edit(
        interaction, kind="pitchin", section="pitchins", event_text=event,
        brief=brief, at=at, repeat=repeat, description=description,
        clear_description=clear_description, close=expires, puntos=puntos, cap=max_scorers,
    )


@edit.command(name="doemup", description="Edit a do-em-up's text, schedule, puntos, or limit")
@app_commands.describe(
    event="The do-em-up to edit — pick from the list, or paste its id",
    brief="New short text (optional)",
    at="New open time / recurring slot — 06:00, tonight, tomorrow 8am (optional)",
    repeat="New repeat — once, daily, weekdays, mon/thu, monthly on the 1st (optional)",
    description="New extra details (optional)",
    clear_description="Remove the existing description",
    deadline="New auto-close time for the (next) round — in 3h, tonight, 18:00 (optional)",
    puntos="New puntos per ➕ (optional)",
    point_limit="New cap: auto-close once this many puntos tally (optional)",
)
async def edit_doemup(
    interaction: discord.Interaction,
    event: str,
    brief: Optional[app_commands.Range[str, 1, 200]] = None,
    at: Optional[str] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
    clear_description: bool = False,
    deadline: Optional[str] = None,
    puntos: Optional[app_commands.Range[int, 1, 100]] = None,
    point_limit: Optional[app_commands.Range[int, 1, 100000]] = None,
) -> None:
    await _apply_game_edit(
        interaction, kind="doemup", section="doemups", event_text=event,
        brief=brief, at=at, repeat=repeat, description=description,
        clear_description=clear_description, close=deadline, puntos=puntos, cap=point_limit,
    )


# Register the shared autocompletes onto each subcommand.
edit_task.autocomplete("task")(task_autocomplete)
edit_task.autocomplete("at")(at_autocomplete)
edit_task.autocomplete("repeat")(repeat_autocomplete)
edit_pitchin.autocomplete("event")(_game_event_autocomplete("pitchins", EMOJI_HANDSHAKE))
edit_pitchin.autocomplete("at")(at_autocomplete)
edit_pitchin.autocomplete("expires")(at_autocomplete)
edit_pitchin.autocomplete("repeat")(repeat_autocomplete)
edit_doemup.autocomplete("event")(_game_event_autocomplete("doemups", EMOJI_FLEX))
edit_doemup.autocomplete("at")(at_autocomplete)
edit_doemup.autocomplete("deadline")(at_autocomplete)
edit_doemup.autocomplete("repeat")(repeat_autocomplete)

# All three subcommands are defined — register the group on the tree.
bot.tree.add_command(edit)


__all__ = [
    "_apply_game_edit",
    "_set_game_recurrence",
    "apply_game_edit",
    "edit",
    "edit_doemup",
    "edit_pitchin",
    "edit_task",
]
