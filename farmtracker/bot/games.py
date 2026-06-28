from __future__ import annotations

import datetime as dt
import json
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from ..models import (
    EMOJI_DONE,
    EMOJI_END,
    discord_ts,
    doemup_apply,
    emoji_key,
    from_iso,
    new_id,
    next_due,
    now_utc,
    pitchin_add,
    pitchin_remove,
    recurrence_of,
    render_doemup,
    render_pitchin,
    to_iso,
)
from .core import (
    NO_PINGS,
    bot,
    log,
    store,
)
from .helpers import (
    _clear_bot_reactions,
    _game_tz,
    _remove_user_reaction,
    safe_delete,
)
from .claps import _arm_game_clap



# ---------------------------------------------------------------------------
# Pitch-ins and do-em-ups  (ad-hoc point events; see models.py for the schemas)
# ---------------------------------------------------------------------------
# Both are posted immediately by their slash command into the configured farm
# channel, resolve by people reacting (pitch-in ✅) or clicking buttons (do-em-up
# ➕/➖), and close at an expiry/deadline, a point cap, or the creator's manual
# end. Closing awards points to the same completion log as chores (with a
# ``points`` field), so one /leaderboard totals chores and games alike. The
# ``ended`` flag is flipped inside the same txn that pops the row, so an
# expiry-tick and a manual end racing each other can only award once.


def _game_record(
    event: dict, kind: str, user_id: int, user_name: str, points: int,
    tz: ZoneInfo, now: dt.datetime,
) -> dict:
    """A completion-log row for one person's points from a pitch-in / do-em-up.
    Same shape as a chore completion (so /leaderboard reads them uniformly) with
    an added ``points`` count (chores omit it and are read as 1)."""
    return {
        "id": new_id(),
        "ts": to_iso(now),
        "month": now.astimezone(tz).strftime("%Y-%m"),  # local-tz bucket
        "guild_id": event["guild_id"],
        "task_id": event["id"],
        "brief": event["brief"],
        "user_id": user_id,
        "user_name": user_name,
        "kind": kind,  # "pitchin" | "doemup"
        "points": points,
        "due_at": to_iso(now),
        "late_seconds": 0,
    }


def game_records(event: dict, kind: str, tz: ZoneInfo, now: dt.datetime) -> list[dict]:
    """Every point-award row owed when an event closes: one per pitch-in scorer,
    or one per do-em-up tallier (worth their unit count × ``points_each``)."""
    pe = event.get("points_each", 1)
    recs: list[dict] = []
    if kind == "pitchin":
        for s in event.get("scorers", []):
            recs.append(_game_record(event, kind, s["user_id"], s["user_name"], pe, tz, now))
    else:  # doemup
        for key, e in event.get("tallies", {}).items():
            if e.get("count", 0) > 0:
                recs.append(_game_record(event, kind, int(key), e["name"], e["count"] * pe, tz, now))
    return recs


def _game_recurrence_fields(
    recurrence: Optional[dict], duration_secs: Optional[int]
) -> dict:
    """The recurrence columns shared by pitch-ins and do-em-ups. ``recurrence`` is
    a rule dict (carrying ``time_of_day``) for a repeating game, or None for a
    one-off. ``next_due`` starts None — it's only set later, while dormant between
    rounds."""
    if not recurrence:
        return {"recurring": False, "freq": "once", "interval_days": 0,
                "weekdays": [], "monthdays": [], "time_of_day": None,
                "next_due": None, "duration_secs": None}
    return {"recurring": True, "freq": recurrence["freq"],
            "interval_days": recurrence.get("interval_days", 0),
            "weekdays": recurrence.get("weekdays", []),
            "monthdays": recurrence.get("monthdays", []),
            "time_of_day": recurrence["time_of_day"],
            "next_due": None, "duration_secs": duration_secs}


async def _send_pitchin(channel: discord.abc.Messageable, p: dict) -> discord.Message:
    """Post a pitch-in's live body and self-react ✅ (pitch in) + 🏁 (end)."""
    msg = await channel.send(render_pitchin(p), allowed_mentions=NO_PINGS)
    try:
        await msg.add_reaction(EMOJI_DONE)
        await msg.add_reaction(EMOJI_END)
    except discord.HTTPException:
        pass
    return msg


async def _send_doemup(channel: discord.abc.Messageable, d: dict) -> discord.Message:
    """Post a do-em-up's live body with its ➕/➖/End buttons."""
    return await channel.send(
        render_doemup(d), view=make_doemup_view(d["id"]), allowed_mentions=NO_PINGS
    )


def _game_next_round(game: dict, tz: ZoneInfo, now: dt.datetime) -> dt.datetime:
    """When the next round of a recurring game should post: its next scheduled
    slot strictly after ``now``, anchored on the original creation time so the
    rounds stay phase-locked to the wall-clock they were started at."""
    return next_due(recurrence_of(game), tz, from_iso(game["created_at"]), now)


async def finalize_pitchin(pid: str, channel: discord.abc.Messageable) -> bool:
    """Close a pitch-in round: award every scorer, rewrite the post as a result
    line, and clear its reactions. A recurring pitch-in then rolls on — it goes
    dormant until its next slot (the whole series is torn down with
    ``/deletetask``). Returns False if it was already gone/closed."""
    snap = await store.snapshot()
    p0 = snap["pitchins"].get(pid)
    if not p0:
        return False
    tz, now = _game_tz(snap, p0["guild_id"]), now_utc()
    event = nxt = None
    async with store.txn() as data:
        p = data["pitchins"].get(pid)
        if not p or p.get("ended"):
            return False
        p["ended"] = True
        event = json.loads(json.dumps(p))
        data["game_messages"].pop(str(p.get("message_id")), None)
        if p.get("recurring"):
            nxt = _game_next_round(p, tz, now)
            p.update({"scorers": [], "ended": False, "message_id": None,
                      "expires_at": None, "next_due": to_iso(nxt)})
        else:
            data["pitchins"].pop(pid, None)
    for rec in game_records(event, "pitchin", tz, now):
        await store.log_completion(rec)
    body = render_pitchin(event, final=True)
    if nxt is not None:
        body += f"\n🔁 Next round {discord_ts(nxt, 'R')}."
    pm = channel.get_partial_message(event["message_id"])
    try:
        await pm.edit(content=body, allowed_mentions=NO_PINGS)
    except discord.HTTPException:
        pass
    # Take down only our ✅/🏁 buttons — a member's fun reaction (a 😄, a 🎉) stays.
    await _clear_bot_reactions(pm, (EMOJI_DONE, EMOJI_END))
    # Our buttons are gone, so add the 👏 (which _arm_clap does) afterwards.
    await _arm_game_clap(event, "pitchin", body, channel)
    return True


async def finalize_doemup(did: str, channel: discord.abc.Messageable) -> bool:
    """Close a do-em-up round: award each tallier their unit count × points_each,
    rewrite the post as a result line, and drop its buttons. A recurring do-em-up
    then rolls on — it goes dormant until its next slot (the whole series is torn
    down with ``/deletetask``)."""
    snap = await store.snapshot()
    d0 = snap["doemups"].get(did)
    if not d0:
        return False
    tz, now = _game_tz(snap, d0["guild_id"]), now_utc()
    event = nxt = None
    async with store.txn() as data:
        d = data["doemups"].get(did)
        if not d or d.get("ended"):
            return False
        d["ended"] = True
        event = json.loads(json.dumps(d))
        data["game_messages"].pop(str(d.get("message_id")), None)
        if d.get("recurring"):
            nxt = _game_next_round(d, tz, now)
            d.update({"tallies": {}, "ended": False, "message_id": None,
                      "deadline": None, "next_due": to_iso(nxt)})
        else:
            data["doemups"].pop(did, None)
    for rec in game_records(event, "doemup", tz, now):
        await store.log_completion(rec)
    body = render_doemup(event, final=True)
    if nxt is not None:
        body += f"\n🔁 Next round {discord_ts(nxt, 'R')}."
    try:
        await channel.get_partial_message(event["message_id"]).edit(
            content=body, view=None, allowed_mentions=NO_PINGS
        )
    except discord.HTTPException:
        pass
    await _arm_game_clap(event, "doemup", body, channel)
    return True


async def repost_pitchin(pid: str, channel: discord.abc.Messageable) -> None:
    """Open a fresh round of a dormant recurring pitch-in (its previous round has
    closed and ``next_due`` has come due)."""
    snap = await store.snapshot()
    p0 = snap["pitchins"].get(pid)
    if not p0 or p0.get("ended") or p0.get("message_id") or not p0.get("next_due"):
        return  # not actually dormant
    tz, now = _game_tz(snap, p0["guild_id"]), now_utc()
    dur = p0.get("duration_secs")
    exp = (now + dt.timedelta(seconds=int(dur))) if dur else _game_next_round(p0, tz, now)
    p_live = {**p0, "scorers": [], "ended": False,
              "expires_at": to_iso(exp), "next_due": None}
    msg = await _send_pitchin(channel, p_live)
    orphan = False
    async with store.txn() as data:
        p = data["pitchins"].get(pid)
        if not p or p.get("ended") or p.get("message_id") or not p.get("next_due"):
            orphan = True  # something raced us
        else:
            p.update({"message_id": msg.id, "scorers": [],
                      "expires_at": to_iso(exp), "next_due": None})
            data["game_messages"][str(msg.id)] = {"kind": "pitchin", "id": pid}
    if orphan:
        await safe_delete(msg)


async def repost_doemup(did: str, channel: discord.abc.Messageable) -> None:
    """Open a fresh round of a dormant recurring do-em-up."""
    snap = await store.snapshot()
    d0 = snap["doemups"].get(did)
    if not d0 or d0.get("ended") or d0.get("message_id") or not d0.get("next_due"):
        return
    tz, now = _game_tz(snap, d0["guild_id"]), now_utc()
    dur = d0.get("duration_secs")
    if dur:
        deadline_iso = to_iso(now + dt.timedelta(seconds=int(dur)))
    elif d0.get("recurring"):  # no fixed window -> run this round to the next slot
        deadline_iso = to_iso(_game_next_round(d0, tz, now))
    else:  # a deferred one-off with no deadline opens and stays open until 🏁
        deadline_iso = None
    d_live = {**d0, "tallies": {}, "ended": False,
              "deadline": deadline_iso, "next_due": None}
    msg = await _send_doemup(channel, d_live)
    orphan = False
    async with store.txn() as data:
        d = data["doemups"].get(did)
        if not d or d.get("ended") or d.get("message_id") or not d.get("next_due"):
            orphan = True
        else:
            d.update({"message_id": msg.id, "tallies": {},
                      "deadline": deadline_iso, "next_due": None})
            data["game_messages"][str(msg.id)] = {"kind": "doemup", "id": did}
    if orphan:
        await safe_delete(msg)


async def post_pitchin(
    channel: discord.abc.Messageable, *, guild_id: int, creator_id: int, brief: str,
    description: Optional[str], expires_at: str, points_each: int,
    max_scorers: Optional[int], now: dt.datetime,
    recurrence: Optional[dict] = None, duration_secs: Optional[int] = None,
) -> tuple[str, discord.Message]:
    pid = new_id()
    p = {
        "id": pid, "guild_id": guild_id, "channel_id": getattr(channel, "id", None),
        "message_id": None, "brief": brief, "description": description,
        "created_by": creator_id, "created_at": to_iso(now),
        "points_each": points_each, "max_scorers": max_scorers,
        "expires_at": expires_at, "scorers": [], "ended": False,
        **_game_recurrence_fields(recurrence, duration_secs),
    }
    msg = await _send_pitchin(channel, p)
    async with store.txn() as data:
        p["message_id"] = msg.id
        data["pitchins"][pid] = p
        data["game_messages"][str(msg.id)] = {"kind": "pitchin", "id": pid}
    return pid, msg


async def schedule_pitchin(
    *, guild_id: int, creator_id: int, channel_id: int, brief: str,
    description: Optional[str], points_each: int, max_scorers: Optional[int],
    now: dt.datetime, recurrence: Optional[dict], duration_secs: Optional[int],
    starts_at: dt.datetime,
) -> str:
    """Create a pitch-in whose first round is deferred: it sits dormant (no post)
    until the scheduler opens it at ``starts_at``. Used when ``/pitchin`` is given
    an ``at:`` — a recurring round then fires at its wall-clock slot, and a one-off
    can be scheduled for later — instead of posting the instant it's created."""
    pid = new_id()
    p = {
        "id": pid, "guild_id": guild_id, "channel_id": channel_id,
        "message_id": None, "brief": brief, "description": description,
        "created_by": creator_id, "created_at": to_iso(now),
        "points_each": points_each, "max_scorers": max_scorers,
        "expires_at": None, "scorers": [], "ended": False,
        **_game_recurrence_fields(recurrence, duration_secs),
        # The recurrence helper zeroes next_due/duration for the live-now path;
        # a deferred round needs its slot, and a one-off needs its open span kept.
        "next_due": to_iso(starts_at), "duration_secs": duration_secs,
    }
    async with store.txn() as data:
        data["pitchins"][pid] = p
    return pid


async def post_doemup(
    channel: discord.abc.Messageable, *, guild_id: int, creator_id: int, brief: str,
    description: Optional[str], points_each: int, deadline: Optional[str],
    point_limit: Optional[int], now: dt.datetime,
    recurrence: Optional[dict] = None, duration_secs: Optional[int] = None,
) -> tuple[str, discord.Message]:
    did = new_id()
    d = {
        "id": did, "guild_id": guild_id, "channel_id": getattr(channel, "id", None),
        "message_id": None, "brief": brief, "description": description,
        "created_by": creator_id, "created_at": to_iso(now),
        "points_each": points_each, "deadline": deadline, "point_limit": point_limit,
        "tallies": {}, "ended": False,
        **_game_recurrence_fields(recurrence, duration_secs),
    }
    msg = await _send_doemup(channel, d)
    async with store.txn() as data:
        d["message_id"] = msg.id
        data["doemups"][did] = d
        data["game_messages"][str(msg.id)] = {"kind": "doemup", "id": did}
    return did, msg


async def schedule_doemup(
    *, guild_id: int, creator_id: int, channel_id: int, brief: str,
    description: Optional[str], points_each: int, point_limit: Optional[int],
    now: dt.datetime, recurrence: Optional[dict], duration_secs: Optional[int],
    starts_at: dt.datetime,
) -> str:
    """Create a do-em-up whose first round is deferred to ``starts_at`` — the
    do-em-up analogue of :func:`schedule_pitchin`. It sits dormant (no post) until
    the scheduler opens it; a deferred one-off with no window then runs until 🏁."""
    did = new_id()
    d = {
        "id": did, "guild_id": guild_id, "channel_id": channel_id,
        "message_id": None, "brief": brief, "description": description,
        "created_by": creator_id, "created_at": to_iso(now),
        "points_each": points_each, "deadline": None, "point_limit": point_limit,
        "tallies": {}, "ended": False,
        **_game_recurrence_fields(recurrence, duration_secs),
        # The recurrence helper zeroes next_due/duration for the live-now path;
        # a deferred round needs its slot, and a windowed one needs its span kept.
        "next_due": to_iso(starts_at), "duration_secs": duration_secs,
    }
    async with store.txn() as data:
        data["doemups"][did] = d
    return did


async def _handle_pitchin_reaction(
    payload: discord.RawReactionActionEvent, channel: discord.abc.Messageable, pid: str
) -> None:
    """A ✅ (pitch in) or 🏁 (creator: end now) on a pitch-in post."""
    key = emoji_key(payload.emoji)
    reacted = channel.get_partial_message(payload.message_id)

    if key == emoji_key(EMOJI_END):
        snap = await store.snapshot()
        p = snap["pitchins"].get(pid)
        if not p or p.get("ended"):
            return
        if payload.user_id == p["created_by"]:
            # 🏁 closes this round early (awarding whoever's in). A recurring
            # pitch-in rolls on to its next slot; stop the series with /deletetask.
            await finalize_pitchin(pid, channel)
        else:
            await _remove_user_reaction(reacted, payload)  # only the creator ends it
        return

    if key != emoji_key(EMOJI_DONE):
        return  # any other reaction on the post — ignore

    member = payload.member
    name = member.display_name if member else str(payload.user_id)
    body = None
    do_finalize = False
    async with store.txn() as data:
        p = data["pitchins"].get(pid)
        if not p or p.get("ended"):
            return
        res = pitchin_add(p, payload.user_id, name)
        if res["changed"]:
            if res["full"]:
                do_finalize = True  # cap reached — close it now
            else:
                body = render_pitchin(p)
    if do_finalize:
        await finalize_pitchin(pid, channel)
    elif body is not None:
        try:
            await reacted.edit(content=body, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass


async def _handle_pitchin_unreact(
    payload: discord.RawReactionActionEvent, channel: discord.abc.Messageable, pid: str
) -> None:
    """A ✅ pulled off a still-open pitch-in drops that person before it closes."""
    body = None
    async with store.txn() as data:
        p = data["pitchins"].get(pid)
        if not p or p.get("ended"):
            return
        if pitchin_remove(p, payload.user_id):
            body = render_pitchin(p)
    if body is not None:
        try:
            await channel.get_partial_message(payload.message_id).edit(
                content=body, allowed_mentions=NO_PINGS
            )
        except discord.HTTPException:
            pass


async def _doemup_press(did: str, action: str, user_id: int, user_name: str) -> dict:
    """Apply a do-em-up button press inside a txn and report what to do next:
    ``status`` ∈ {gone, error, changed, final} (+ the new ``body`` when changed).
    ``final`` means the caller should run :func:`finalize_doemup`."""
    out = {"status": "gone", "body": None}
    async with store.txn() as data:
        d = data["doemups"].get(did)
        if not d or d.get("ended"):
            return out
        res = doemup_apply(d, action, user_id, user_name)
        if res["error"] == "not_creator":
            out["status"] = "error"
        elif res["final"]:
            out["status"] = "final"  # ➕ hit the cap, or the creator tapped End
        else:
            out["status"] = "changed"
            out["body"] = render_doemup(d)
    return out


async def handle_doemup_button(
    did: str, action: str, interaction: discord.Interaction
) -> None:
    res = await _doemup_press(did, action, interaction.user.id, interaction.user.display_name)
    status = res["status"]
    if status == "gone":
        await interaction.response.send_message(
            "That do-em-up has already closed.", ephemeral=True
        )
    elif status == "error":
        await interaction.response.send_message(
            "Only the person who started this do-em-up can end it.", ephemeral=True
        )
    elif status == "final":
        await interaction.response.defer()  # finalize edits the post itself
        # End and hitting the point_limit both just close this round; a recurring
        # do-em-up rolls on to its next slot (stop the series with /deletetask).
        await finalize_doemup(did, interaction.channel)
    else:  # changed — update the live tally in place
        await interaction.response.edit_message(content=res["body"], view=make_doemup_view(did))


class DoEmUpButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"doemup:(?P<action>plus|minus|end):(?P<did>[0-9a-f]+)",
):
    """A persistent ➕ / ➖ / 🏁End button. The do-em-up id rides in the
    custom_id, so a single ``add_dynamic_items`` registration on startup revives
    every do-em-up's buttons after a restart with no per-message bookkeeping."""

    LABELS = {"plus": "➕", "minus": "➖", "end": f"{EMOJI_END} End"}
    STYLES = {
        "plus": discord.ButtonStyle.success,
        "minus": discord.ButtonStyle.secondary,
        "end": discord.ButtonStyle.danger,
    }

    def __init__(self, did: str, action: str) -> None:
        self.did = did
        self.action = action
        super().__init__(
            discord.ui.Button(
                label=self.LABELS[action],
                style=self.STYLES[action],
                custom_id=f"doemup:{action}:{did}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):  # noqa: ANN001
        return cls(match["did"], match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_doemup_button(self.did, self.action, interaction)


def make_doemup_view(did: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DoEmUpButton(did, "plus"))
    view.add_item(DoEmUpButton(did, "minus"))
    view.add_item(DoEmUpButton(did, "end"))
    return view


async def sweep_games(now: dt.datetime, snap: dict) -> None:
    """Drive each game's clock from the scheduler tick: close a live round past
    its expiry/deadline, and re-post a dormant recurring round once its next slot
    comes due. Running off the tick means anything that fell due while the bot was
    down fires on the next tick — exactly like chore reminders."""
    for pid, p in list(snap.get("pitchins", {}).items()):
        try:
            if p.get("ended"):
                continue
            ch = bot.get_channel(int(p["channel_id"])) if p.get("channel_id") else None
            if ch is None:
                continue
            if p.get("next_due"):  # dormant between rounds -> re-post when due
                if now >= from_iso(p["next_due"]):
                    await repost_pitchin(pid, ch)
            elif p.get("expires_at") and now >= from_iso(p["expires_at"]):
                await finalize_pitchin(pid, ch)
        except Exception:
            log.exception("scheduler error on pitchin %s", pid)
    for did, d in list(snap.get("doemups", {}).items()):
        try:
            if d.get("ended"):
                continue
            ch = bot.get_channel(int(d["channel_id"])) if d.get("channel_id") else None
            if ch is None:
                continue
            if d.get("next_due"):  # dormant between rounds -> re-post when due
                if now >= from_iso(d["next_due"]):
                    await repost_doemup(did, ch)
            else:
                dl = d.get("deadline")
                if dl and now >= from_iso(dl):
                    await finalize_doemup(did, ch)
        except Exception:
            log.exception("scheduler error on doemup %s", did)


__all__ = [
    "DoEmUpButton",
    "_doemup_press",
    "_game_next_round",
    "_game_record",
    "_game_recurrence_fields",
    "_handle_pitchin_reaction",
    "_handle_pitchin_unreact",
    "_send_doemup",
    "_send_pitchin",
    "finalize_doemup",
    "finalize_pitchin",
    "game_records",
    "handle_doemup_button",
    "make_doemup_view",
    "post_doemup",
    "post_pitchin",
    "repost_doemup",
    "repost_pitchin",
    "schedule_doemup",
    "schedule_pitchin",
    "sweep_games",
]
