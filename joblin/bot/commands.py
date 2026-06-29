from __future__ import annotations

import datetime as dt
import json
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .. import trinkets
from ..models import (
    EMOJI_FLEX,
    EMOJI_HANDSHAKE,
    UTC,
    describe_repeat,
    discord_ts,
    first_due,
    from_iso,
    new_id,
    next_due,
    now_utc,
    parse_repeat,
    recurrence_of,
    render_doemup,
    render_pitchin,
    resolve_when,
    time_of_day_from,
    to_iso,
)
from .core import (
    COMMON_TZS,
    NO_PINGS,
    bot,
    store,
)
from .helpers import (
    config_ready,
    guild_config,
    schedule_label,
)
from .games import (
    _game_next_round,
    make_doemup_view,
    post_doemup,
    post_pitchin,
    schedule_doemup,
    schedule_pitchin,
)
from .reactions import (
    _delete_panels,
    _take_task_panels,
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
    return {
        "recurring": True, "freq": rule["freq"], "interval_days": rule["interval_days"],
        "weekdays": rule["weekdays"], "monthdays": rule["monthdays"],
        "time_of_day": tod, "next_due": first_due(rule, tz, now),
    }


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
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
            cfg["item_bar"] = item_bar
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
    if not config_ready(current):
        msg += "\n\n⚠️ Set **both** a channel and a timezone before creating tasks."
    await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=NO_PINGS)


@joblinconfig.autocomplete("timezone")
async def _tz_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    matches = [z for z in COMMON_TZS if cur in z.lower()][:25]
    return [app_commands.Choice(name=z, value=z) for z in matches]


# --- Autocomplete helpers (shared across commands) -------------------------
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


def _repeat_label(rule: dict, now: dt.datetime) -> str:
    if rule["freq"] == "monthly" and not rule["monthdays"]:
        rule = {**rule, "monthdays": [now.day]}  # preview only; real day set at run time
    return "one-off" if rule["freq"] == "once" else describe_repeat(rule)


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


# One /edit command with a subcommand per type (task / pitchin / doemup), so each
# only ever shows its own fields — no bounty on a pitch-in, no max_scorers on a
# task. Registered on the tree at the end of the file once all three are defined.
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


# Register the shared autocompletes onto each command that needs them.
for _cmd in (newtask, edit_task):
    _cmd.autocomplete("at")(at_autocomplete)
    _cmd.autocomplete("repeat")(repeat_autocomplete)
edit_task.autocomplete("task")(task_autocomplete)
deletetask.autocomplete("task")(delete_autocomplete)


def _game_recurrence_from(
    repeat: Optional[str], tz: ZoneInfo, now: dt.datetime, at: Optional[str] = None
) -> Optional[dict]:
    """Parse a game's ``repeat`` into a recurrence rule, or None for a one-off.
    Mirrors :func:`schedule_from_rule`: a bare ``monthly`` takes today's date, and
    every round fires at ``at`` (the wall-clock time the game is started at when
    ``at`` is omitted)."""
    rule = parse_repeat(repeat)  # raises ValueError on junk
    if rule["freq"] == "once":
        return None
    if rule["freq"] == "monthly" and not rule["monthdays"]:
        rule["monthdays"] = [now.astimezone(tz).day]
    rule["time_of_day"] = time_of_day_from(at, tz, now)
    return rule


@bot.tree.command(
    name="pitchin",
    description="Start a pitch-in: everyone who ✅s before it closes earns a punto",
)
@app_commands.describe(
    brief="What to pitch in on, e.g. 'laundry bonanza' (required)",
    at="When the first round opens — now, 06:00, tomorrow 8am. Recurring? sets the daily slot (default: now)",
    expires="When a round closes — in 5m, tonight, 18:00, tomorrow 8am (default: in 24h; recurring: at the next slot)",
    puntos="Puntos each pitcher-inner earns (default: 1)",
    max_scorers="Optional cap: only the first N to pitch in score",
    repeat="Repeat it — daily, weekdays, mon/thu, monthly on the 1st (default: once)",
    description="Optional extra details shown on the post",
)
async def pitchin(
    interaction: discord.Interaction,
    brief: app_commands.Range[str, 1, 200],
    at: Optional[str] = None,
    expires: Optional[str] = None,
    puntos: app_commands.Range[int, 1, 100] = 1,
    max_scorers: Optional[app_commands.Range[int, 1, 100]] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
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
        recurrence = _game_recurrence_from(repeat, tz, now, at)
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/joblinhelp` for the `repeat` formats.", ephemeral=True
        )
        return
    # With `at`, the first round is deferred to its scheduled slot (a recurring
    # one fires at that wall-clock time rather than whenever it was created, and a
    # one-off can be set for later); without it, the round opens right now.
    deferred = bool(at)
    duration_secs = None
    try:
        if deferred:
            start = first_due(recurrence, tz, now) if recurrence else resolve_when(at, tz, now)
            if start <= now:
                raise ValueError("that start time is already in the past")
        else:
            start = now
        if expires:
            exp = resolve_when(expires, tz, start)  # clock times land on the slot's day
        elif recurrence:  # no window given -> the round runs until the next slot
            exp = next_due(recurrence, tz, start, start)
        else:
            exp = start + dt.timedelta(hours=24)
        if exp <= start:
            raise ValueError("that close time is already in the past")
        # A repeating round, or any deferred round, stores its open span as a
        # duration the scheduler reuses each time it (re)opens the post.
        if recurrence:
            if expires:  # an explicit window each round repeats
                duration_secs = max(1, int((exp - start).total_seconds()))
        elif deferred:  # a one-off scheduled for later keeps its open span too
            duration_secs = max(1, int((exp - start).total_seconds()))
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/joblinhelp` for the time formats.", ephemeral=True
        )
        return
    channel = bot.get_channel(int(cfg["channel_id"]))
    if channel is None:
        await interaction.response.send_message(
            "❌ I can't see the configured channel — check `/joblinconfig`.", ephemeral=True
        )
        return

    if deferred:
        await schedule_pitchin(
            guild_id=interaction.guild_id, creator_id=interaction.user.id,
            channel_id=channel.id, brief=str(brief),
            description=(description[:1000] if description else None),
            points_each=int(puntos),
            max_scorers=(int(max_scorers) if max_scorers else None), now=now,
            recurrence=recurrence, duration_secs=duration_secs, starts_at=start,
        )
    else:
        await post_pitchin(
            channel, guild_id=interaction.guild_id, creator_id=interaction.user.id,
            brief=str(brief), description=(description[:1000] if description else None),
            expires_at=to_iso(exp), points_each=int(puntos),
            max_scorers=(int(max_scorers) if max_scorers else None), now=now,
            recurrence=recurrence, duration_secs=duration_secs,
        )
    cap = f" · first {max_scorers} score" if max_scorers else ""
    rep = f" · 🔁 {describe_repeat(recurrence)} (`/deletetask` to stop it)" if recurrence else ""
    if deferred:
        verb, when = "Scheduled", f"first round opens {discord_ts(start, 'R')}"
    elif recurrence:
        verb, when = "Posted", f"first round closes {discord_ts(exp, 'R')}"
    else:
        verb, when = "Posted", f"closes {discord_ts(exp, 'R')}"
    await interaction.response.send_message(
        f"🤝 {verb} **{brief}** in <#{cfg['channel_id']}> — {when}{cap}{rep}.",
        ephemeral=True,
    )


@bot.tree.command(
    name="doemup",
    description="Start a do-em-up: tap ➕ for each one you do; puntos tally live",
)
@app_commands.describe(
    brief="What's being done one-at-a-time, e.g. 'thistle bush removed' (required)",
    at="When the first round opens — now, 06:00, tomorrow 8am. Recurring? sets the daily slot (default: now)",
    puntos="Puntos per ➕ (default: 1)",
    deadline="Optional auto-close time — tonight, in 3h, tomorrow 18:00",
    point_limit="Optional cap: auto-close once this many puntos are tallied",
    repeat="Repeat it — daily, weekdays, mon/thu, monthly on the 1st (default: once)",
    description="Optional extra details shown on the post",
)
async def doemup(
    interaction: discord.Interaction,
    brief: app_commands.Range[str, 1, 200],
    at: Optional[str] = None,
    puntos: app_commands.Range[int, 1, 100] = 1,
    deadline: Optional[str] = None,
    point_limit: Optional[app_commands.Range[int, 1, 100000]] = None,
    repeat: Optional[str] = None,
    description: Optional[str] = None,
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
        recurrence = _game_recurrence_from(repeat, tz, now, at)
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/joblinhelp` for the `repeat` formats.", ephemeral=True
        )
        return
    # With `at`, the first round is deferred to its scheduled slot (see /pitchin);
    # without it, the round opens right now.
    deferred = bool(at)
    deadline_iso, duration_secs = None, None
    try:
        if deferred:
            start = first_due(recurrence, tz, now) if recurrence else resolve_when(at, tz, now)
            if start <= now:
                raise ValueError("that start time is already in the past")
        else:
            start = now
        if deadline:
            dl = resolve_when(deadline, tz, start)  # clock times land on the slot's day
            if dl <= start:
                raise ValueError("that deadline is already in the past")
            deadline_iso = to_iso(dl)
            # A repeating round, or any deferred round, stores its open span as a
            # duration the scheduler reuses each time it (re)opens the post.
            if recurrence or deferred:
                duration_secs = max(1, int((dl - start).total_seconds()))
        elif recurrence:  # recurring needs a close: run each round to the next slot
            deadline_iso = to_iso(next_due(recurrence, tz, start, start))
        # else: a plain one-off do-em-up stays open until 🏁 (even when deferred)
    except ValueError as e:
        await interaction.response.send_message(
            f"❌ {e}\nSee `/joblinhelp` for the time formats.", ephemeral=True
        )
        return
    channel = bot.get_channel(int(cfg["channel_id"]))
    if channel is None:
        await interaction.response.send_message(
            "❌ I can't see the configured channel — check `/joblinconfig`.", ephemeral=True
        )
        return

    if deferred:
        await schedule_doemup(
            guild_id=interaction.guild_id, creator_id=interaction.user.id,
            channel_id=channel.id, brief=str(brief),
            description=(description[:1000] if description else None),
            points_each=int(puntos),
            point_limit=(int(point_limit) if point_limit else None), now=now,
            recurrence=recurrence, duration_secs=duration_secs, starts_at=start,
        )
        opens = f" — first round opens {discord_ts(start, 'R')}"
        rep = f" · 🔁 {describe_repeat(recurrence)} (`/deletetask` to stop it)" if recurrence else ""
        await interaction.response.send_message(
            f"💪 Scheduled **{brief}** in <#{cfg['channel_id']}>{opens}{rep}.",
            ephemeral=True,
        )
        return

    await post_doemup(
        channel, guild_id=interaction.guild_id, creator_id=interaction.user.id,
        brief=str(brief), description=(description[:1000] if description else None),
        points_each=int(puntos), deadline=deadline_iso,
        point_limit=(int(point_limit) if point_limit else None), now=now,
        recurrence=recurrence, duration_secs=duration_secs,
    )
    if recurrence:
        closes = (
            f" — first round closes {discord_ts(from_iso(deadline_iso), 'R')}"
            f" · 🔁 {describe_repeat(recurrence)} (`/deletetask` to stop it)"
        )
    else:
        closes = f" — closes {discord_ts(from_iso(deadline_iso), 'R')}" if deadline_iso else ""
    await interaction.response.send_message(
        f"💪 Posted **{brief}** in <#{cfg['channel_id']}> — tap ➕ as you go{closes}.",
        ephemeral=True,
    )


# The friendly "when"/"repeat" autocompletes (live previews of the resolved
# instant / rule) are the same ones /newtask uses for `at` and `repeat`.
pitchin.autocomplete("at")(at_autocomplete)
pitchin.autocomplete("expires")(at_autocomplete)
pitchin.autocomplete("repeat")(repeat_autocomplete)
doemup.autocomplete("at")(at_autocomplete)
doemup.autocomplete("deadline")(at_autocomplete)
doemup.autocomplete("repeat")(repeat_autocomplete)


# --- /edit pitchin and /edit doemup (shared engine) ------------------------
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


async def _apply_game_edit(
    interaction: discord.Interaction, *, kind: str, section: str,
    close_field: str, cap_field: str, event_text: str,
    brief: Optional[str], at: Optional[str], repeat: Optional[str],
    description: Optional[str], clear_description: bool,
    close: Optional[str], puntos: Optional[int], cap: Optional[int],
) -> None:
    """Shared engine for /edit pitchin and /edit doemup — they differ only in the
    close field (expires_at vs deadline) and the cap field (max_scorers vs
    point_limit). A schedule change recomputes the next open slot for a
    scheduled/dormant round; for a live round it applies from the next round (the
    open post is left alone except for an explicit close-time change)."""
    noun = "pitch-in" if kind == "pitchin" else "do-em-up"
    snap = await store.snapshot()
    event = _find_game_in(snap, interaction.guild_id, section, event_text)
    if not event:
        await interaction.response.send_message(
            f"❌ {noun.capitalize()} not found. Use `/listtasks` to see ids.", ephemeral=True)
        return
    if (brief is None and at is None and repeat is None and description is None
            and not clear_description and close is None and puntos is None and cap is None):
        await interaction.response.send_message(
            "❌ Nothing to change — set at least one field.", ephemeral=True)
        return

    cfg = guild_config(snap, interaction.guild_id)
    recompute = at is not None or repeat is not None or close is not None
    if recompute and not config_ready(cfg):
        await interaction.response.send_message(
            "❌ Set a timezone with `/joblinconfig` before changing the schedule.", ephemeral=True)
        return
    tz = ZoneInfo(cfg["timezone"]) if (cfg and cfg.get("timezone")) else UTC
    now = now_utc()

    new_rec = None
    rec_changed = at is not None or repeat is not None
    if rec_changed:
        try:
            if repeat is not None:
                new_rec = _game_recurrence_from(repeat, tz, now, at)
            else:  # only `at` changed — keep the existing rule, move its slot
                new_rec = recurrence_of(event) if event.get("recurring") else None
                if new_rec is not None and at is not None:
                    new_rec = {**new_rec, "time_of_day": time_of_day_from(at, tz, now)}
        except ValueError as e:
            await interaction.response.send_message(
                f"❌ {e}\nSee `/joblinhelp` for the formats.", ephemeral=True)
            return

    updated = None
    err = None
    async with store.txn() as data:
        g = data[section].get(event["id"])
        if g and str(g["guild_id"]) == str(interaction.guild_id):
            live = bool(g.get("message_id"))
            if brief is not None:
                g["brief"] = str(brief)
            if clear_description:
                g["description"] = None
            elif description is not None:
                g["description"] = description[:1000]
            if puntos is not None:
                g["points_each"] = int(puntos)
            if cap is not None:
                g[cap_field] = int(cap)

            if rec_changed:
                _set_game_recurrence(g, new_rec)
                if not live:  # scheduled or dormant — recompute the next open slot
                    if new_rec:
                        g["next_due"] = to_iso(_game_next_round(g, tz, now))
                    elif at is not None:
                        start = resolve_when(at, tz, now)
                        if start <= now:
                            err = "that start time is already in the past"
                        else:
                            g["next_due"] = to_iso(start)

            if err is None and close is not None:
                base = now if live else (from_iso(g["next_due"]) if g.get("next_due") else now)
                new_close = resolve_when(close, tz, base)
                if new_close <= base:
                    err = "that close time is already in the past"
                else:
                    if live:
                        g[close_field] = to_iso(new_close)
                    if g.get("recurring") or not live:
                        g["duration_secs"] = max(1, int((new_close - base).total_seconds()))

            if err is None:
                updated = json.loads(json.dumps(g))

    if err:
        await interaction.response.send_message(
            f"❌ {err}\nSee `/joblinhelp` for the time formats.", ephemeral=True)
        return
    if not updated:
        await interaction.response.send_message(
            f"❌ {noun.capitalize()} not found.", ephemeral=True)
        return

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
        interaction, kind="pitchin", section="pitchins",
        close_field="expires_at", cap_field="max_scorers", event_text=event,
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
        interaction, kind="doemup", section="doemups",
        close_field="deadline", cap_field="point_limit", event_text=event,
        brief=brief, at=at, repeat=repeat, description=description,
        clear_description=clear_description, close=deadline, puntos=puntos, cap=point_limit,
    )


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
    "_cancel_game_message",
    "_dedup",
    "_find_game",
    "_find_game_in",
    "_find_task",
    "_game_event_autocomplete",
    "_game_recurrence_from",
    "_guild_tz",
    "_human_until",
    "_repeat_label",
    "_set_game_recurrence",
    "_tz_autocomplete",
    "_when_label",
    "at_autocomplete",
    "delete_autocomplete",
    "deletetask",
    "doemup",
    "edit",
    "edit_doemup",
    "edit_pitchin",
    "edit_task",
    "joblinconfig",
    "newtask",
    "pitchin",
    "repeat_autocomplete",
    "schedule_from_rule",
    "task_autocomplete",
]
