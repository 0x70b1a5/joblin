from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

import discord

from ..models import (
    EMOJI_CLAP,
    new_id,
    now_utc,
    to_iso,
)
from .core import (
    NO_PINGS,
    bot,
    store,
)
from .helpers import (
    _game_tz,
    _remove_user_reaction,
)



# ---------------------------------------------------------------------------
# Claps (👏)
# ---------------------------------------------------------------------------
# A finished post grows a 👏 button so the rest of the family can cheer the doers
# on: a tap from anyone who *didn't* take part tips every participant a +1 bonus
# punto — capped at one clap per outsider. The same button rides a ✅-completed
# chore (participant = the completer) and a closed pitch-in / do-em-up round
# (participants = its scorers / talliers), so one clap can tip several people at
# once. Each landed clap is announced with a fresh message in the channel
# ("BAM! @doer got clapped by @outsider!"). Bonuses are written to the same
# completion log as chores (kind "clap", points 1), so /leaderboard totals them
# like any other punto, and undoing a chore's ✅ retracts them (games have no
# undo). Like ↩️/🔄, the table is keyed by the finished post's id, survives
# restarts, and only the most recent finished post per task/game carries a live 👏.
def _clap_record(rec: dict, participant: dict, tz: ZoneInfo, now: dt.datetime) -> dict:
    """A completion-log row for one clap bonus: +1 to a finished task's
    participant, shaped like a chore completion so /leaderboard reads it the
    same way."""
    return {
        "id": new_id(),  # lets an undo void exactly this bonus
        "ts": to_iso(now),
        "month": now.astimezone(tz).strftime("%Y-%m"),  # local-tz bucket
        "guild_id": rec["guild_id"],
        "task_id": rec["task_id"],
        "brief": rec.get("brief", ""),
        "user_id": participant["user_id"],
        "user_name": participant["user_name"],
        "kind": "clap",
        "points": 1,
        "due_at": to_iso(now),
        "late_seconds": 0,
    }


CLAP_OPENERS = [
    "BAM!", "YAHOO!", "WAHOO!", "WOW!", "YIKES!", "ZING!", "ZAP!",
    "POW!", "BOOM!", "WHAM!", "KAPOW!", "ZOWIE!", "YOWZA!", "HOT DOG!",
    "Oh.", "Huh.", "Well.", "My.",
]
CLAP_ENDINGS = ["!", "?", "?!", ".", "...", "...!", "...?"]


def clap_announcement(participants: list[dict], clapper_id: int) -> str:
    """One landed clap's shout-out line — a random opener and closer around who
    got clapped by whom. One clap tips *every* participant, so they all get
    named."""
    mentions = [f"<@{p['user_id']}>" for p in participants]
    who = (", ".join(mentions[:-1]) + " and " + mentions[-1]
           if len(mentions) > 1 else mentions[0])
    return (f"{random.choice(CLAP_OPENERS)} {who} got clapped by "
            f"<@{clapper_id}>{random.choice(CLAP_ENDINGS)}")


def _game_participants(event: dict, kind: str) -> list[dict]:
    """The scorers a finalized game round tips when clapped: a pitch-in's ✅
    scorers, or a do-em-up's talliers with a positive count."""
    if kind == "pitchin":
        return [{"user_id": s["user_id"], "user_name": s["user_name"]}
                for s in event.get("scorers", [])]
    return [{"user_id": int(uid), "user_name": e.get("name", str(uid))}
            for uid, e in event.get("tallies", {}).items() if e.get("count", 0) > 0]


async def _arm_clap(
    tid: str,
    anchor_id: int,
    channel: discord.abc.Messageable,
    guild_id: int,
    brief: str,
    participants: list[dict],
) -> None:
    """Add the 👏 button to a just-completed post and remember who may be tipped,
    retiring any 👏 left on this task's older completed posts (their already-paid
    bonuses stand — only the button is taken away)."""
    stale: list[int] = []
    async with store.txn() as data:
        for mid, rec in list(data["claps"].items()):
            if rec.get("task_id") == tid and str(mid) != str(anchor_id):
                data["claps"].pop(mid, None)
                stale.append(int(mid))
        data["claps"][str(anchor_id)] = {
            "task_id": tid,
            "guild_id": guild_id,
            "channel_id": getattr(channel, "id", None),
            "brief": brief,
            "participants": participants,
            "clappers": [],  # outsider ids who've already clapped (the per-outsider cap)
            "log_ids": [],  # completion ids the claps logged (so an undo can void them)
        }
    try:
        await channel.get_partial_message(anchor_id).add_reaction(EMOJI_CLAP)
    except discord.HTTPException:
        pass
    if bot.user:  # tidy now-dead 👏 buttons left on this task's older posts
        for mid in stale:
            try:
                await channel.get_partial_message(mid).remove_reaction(EMOJI_CLAP, bot.user)
            except discord.HTTPException:
                pass


async def _arm_game_clap(
    event: dict, kind: str, channel: discord.abc.Messageable
) -> None:
    """Arm a 👏 on a just-finalized pitch-in / do-em-up round so an outsider can
    tip its scorers a bonus punto each. No-op when the round closed with nobody in
    (or its post is gone). A recurring game's next round retires this one's 👏 the
    same way a chore's next completion does — keyed on the shared game id."""
    participants = _game_participants(event, kind)
    mid = event.get("message_id")
    if not participants or mid is None:
        return
    await _arm_clap(
        event["id"], mid, channel, event["guild_id"], event["brief"], participants
    )


async def _handle_clap(
    payload: discord.RawReactionActionEvent, channel: discord.abc.Messageable
) -> None:
    """A 👏 on a ✅-completed post. From a non-participant it awards every
    participant a +1 bonus punto (once per outsider) and announces it with a
    fresh message; a participant clapping their own finish is ignored."""
    snap = await store.snapshot()
    rec0 = snap["claps"].get(str(payload.message_id))
    if not rec0:
        return  # a 👏 on something we don't track — ignore
    reacted = channel.get_partial_message(payload.message_id)
    if any(p["user_id"] == payload.user_id for p in rec0["participants"]):
        # You can't clap your own chore — drop the reaction (needs Manage Messages).
        await _remove_user_reaction(reacted, payload)
        return

    tz, now = _game_tz(snap, rec0["guild_id"]), now_utc()
    new_records: list[dict] = []
    body = None
    async with store.txn() as data:
        rec = data["claps"].get(str(payload.message_id))
        if not rec:
            return  # retired/undone between the snapshot and here
        if payload.user_id in rec["clappers"]:
            return  # one clap per outsider — a repeat (or re-add) is a no-op
        if any(p["user_id"] == payload.user_id for p in rec["participants"]):
            return  # a participant slipped in after the snapshot
        rec["clappers"].append(payload.user_id)
        for p in rec["participants"]:
            r = _clap_record(rec, p, tz, now)
            rec.setdefault("log_ids", []).append(r["id"])
            new_records.append(r)
        body = clap_announcement(rec["participants"], payload.user_id)
    for r in new_records:
        await store.log_completion(r)
    if body is not None:
        try:
            await channel.send(body, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass


__all__ = [
    "_arm_clap",
    "_arm_game_clap",
    "_clap_record",
    "_game_participants",
    "_handle_clap",
    "clap_announcement",
]
