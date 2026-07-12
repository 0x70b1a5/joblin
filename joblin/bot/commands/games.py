"""``/pitchin`` and ``/doemup`` — start (or schedule) an ad-hoc punto round.

Only the command surface lives here: parsing the user's schedule/window into
concrete instants and handing off. The engine that posts, sweeps, reopens,
and closes the rounds is ``bot.games`` (one package level up)."""

from __future__ import annotations

import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from ...models import (
    describe_repeat,
    discord_ts,
    first_due,
    format_duration,
    from_iso,
    next_due,
    now_utc,
    parse_repeat,
    pin_weekly,
    resolve_close,
    resolve_when,
    time_of_day_from,
    to_iso,
)
from ..core import bot, store
from ..games import post_doemup, post_pitchin, schedule_doemup, schedule_pitchin
from ..helpers import config_ready, guild_config
from .lookup import at_autocomplete, close_autocomplete, repeat_autocomplete


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
    return pin_weekly(rule, at, tz, now, at_given=at is not None)


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
            # Phrase-class resolve: relative/bare clock vs start; calendar vs now
            # (so "at: tomorrow noon" + "expires: tomorrow 23:59" is ~12h, not ~36h).
            exp = resolve_close(expires, tz, now=now, start=start)
        elif recurrence:  # no window given -> the round runs until the next slot
            exp = next_due(recurrence, tz, start, start)
        else:
            exp = start + dt.timedelta(hours=24)
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
    win = f" · open {format_duration(int((exp - start).total_seconds()))}"
    if deferred:
        verb = "Scheduled"
        when = (
            f"opens {discord_ts(start, 'R')} · closes {discord_ts(exp, 'R')}{win}"
        )
    elif recurrence:
        verb = "Posted"
        when = f"first round closes {discord_ts(exp, 'R')}{win}"
    else:
        verb = "Posted"
        when = f"closes {discord_ts(exp, 'R')}{win}"
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
    dl = None  # concrete close instant when known (for the echo)
    try:
        if deferred:
            start = first_due(recurrence, tz, now) if recurrence else resolve_when(at, tz, now)
            if start <= now:
                raise ValueError("that start time is already in the past")
        else:
            start = now
        if deadline:
            # Same phrase-class rules as /pitchin expires (see resolve_close).
            dl = resolve_close(deadline, tz, now=now, start=start)
            deadline_iso = to_iso(dl)
            # A repeating round, or any deferred round, stores its open span as a
            # duration the scheduler reuses each time it (re)opens the post.
            if recurrence or deferred:
                duration_secs = max(1, int((dl - start).total_seconds()))
        elif recurrence:  # recurring needs a close: run each round to the next slot
            dl = next_due(recurrence, tz, start, start)
            deadline_iso = to_iso(dl)
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

    win = (
        f" · open {format_duration(int((dl - start).total_seconds()))}"
        if dl is not None else ""
    )
    rep = f" · 🔁 {describe_repeat(recurrence)} (`/deletetask` to stop it)" if recurrence else ""
    if deferred:
        await schedule_doemup(
            guild_id=interaction.guild_id, creator_id=interaction.user.id,
            channel_id=channel.id, brief=str(brief),
            description=(description[:1000] if description else None),
            points_each=int(puntos),
            point_limit=(int(point_limit) if point_limit else None), now=now,
            recurrence=recurrence, duration_secs=duration_secs, starts_at=start,
        )
        if dl is not None:
            when = f"opens {discord_ts(start, 'R')} · closes {discord_ts(dl, 'R')}{win}"
        else:
            when = f"opens {discord_ts(start, 'R')} · open until 🏁"
        await interaction.response.send_message(
            f"💪 Scheduled **{brief}** in <#{cfg['channel_id']}> — {when}{rep}.",
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
    if dl is not None:
        label = "first round closes" if recurrence else "closes"
        closes = f" — {label} {discord_ts(dl, 'R')}{win}{rep}"
    else:
        closes = rep if rep else ""
    await interaction.response.send_message(
        f"💪 Posted **{brief}** in <#{cfg['channel_id']}> — tap ➕ as you go{closes}.",
        ephemeral=True,
    )


# The friendly "when"/"repeat" autocompletes (live previews of the resolved
# instant / rule) are the same ones /newtask uses for `at` and `repeat`.
pitchin.autocomplete("at")(at_autocomplete)
pitchin.autocomplete("expires")(close_autocomplete)
pitchin.autocomplete("repeat")(repeat_autocomplete)
doemup.autocomplete("at")(at_autocomplete)
doemup.autocomplete("deadline")(close_autocomplete)
doemup.autocomplete("repeat")(repeat_autocomplete)


__all__ = [
    "_game_recurrence_from",
    "doemup",
    "pitchin",
]
