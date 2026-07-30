from __future__ import annotations

import datetime as dt
import random
from typing import Optional
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
    store,
)
from .helpers import (
    Press,
    _game_tz,
    _tidy_stale,
    completed_view,
    post_view_for,
)



# ---------------------------------------------------------------------------
# Claps (👏)
# ---------------------------------------------------------------------------
# A finished post grows a 👏 button so the rest of the family can cheer the doers
# on: a tap from anyone who *didn't* take part tips every participant a +1 bonus
# punto — capped at one clap per outsider. The same button rides a ✅-completed
# chore (participant = the completer) and a closed pitch-in / do-em-up round
# (participants = its scorers / talliers), so one clap can tip several people at
# once. Each landed clap shows up twice: the finished post's tally line is
# updated in place (👏 ×n), and a fresh shout-out lands in the channel ("BAM!
# @doer got clapped by @outsider!"). Bonuses are written to the same completion
# log as chores (kind "clap", points 1), so /leaderboard totals them like any
# other punto, and undoing a chore's ✅ retracts them (games have no undo). Like
# ↩️/🔄, the table is keyed by the finished post's id, survives restarts, and
# only the most recent finished post per task/game carries a live 👏.
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


def clap_status(rec: dict) -> str:
    """The finished-post text with its running clap tally appended (the bare
    status until the first clap lands). With several participants the bonus is
    +n *each*, since every clap tips all of them."""
    n = len(rec.get("clappers", []))
    if n == 0:
        return rec["status"]
    parts = rec["participants"]
    names = ", ".join(p["user_name"] for p in parts)
    each = " each" if len(parts) > 1 else ""
    return f"{rec['status']}\n👏 ×{n} · +{n} punto{each} to {names}"


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
    status: str,
    participants: list[dict],
    *,
    tidy: Optional[set] = None,
) -> None:
    """Remember who a just-completed post's 👏 may tip (the button itself lands
    with the caller's status edit), retiring any 👏 left on this task's older
    completed posts (their already-paid bonuses stand — only the button is
    taken away; see _tidy_stale)."""
    stale: list[tuple[int, bool]] = []
    async with store.txn() as data:
        for mid, rec in list(data["claps"].items()):
            if rec.get("task_id") == tid and str(mid) != str(anchor_id):
                data["claps"].pop(mid, None)
                stale.append((int(mid), rec.get("ui") == "buttons"))
        data["claps"][str(anchor_id)] = {
            "task_id": tid,
            "guild_id": guild_id,
            "channel_id": getattr(channel, "id", None),
            "brief": brief,
            "status": status,
            "participants": participants,
            "clappers": [],  # outsider ids who've already clapped (the per-outsider cap)
            "log_ids": [],  # completion ids the claps logged (so an undo can void them)
            "ui": "buttons",
        }
    await _tidy_stale(channel, stale, EMOJI_CLAP, tidy)


async def _arm_game_clap(
    event: dict, kind: str, status: str, channel: discord.abc.Messageable
):
    """Arm a 👏 on a just-finalized pitch-in / do-em-up round so an outsider can
    tip its scorers a bonus punto each, returning the view the closed post
    should carry (None when the round closed with nobody in, or its post is
    gone). A recurring game's next round retires this one's 👏 the same way a
    chore's next completion does — keyed on the shared game id."""
    participants = _game_participants(event, kind)
    mid = event.get("message_id")
    if not participants or mid is None:
        return None
    await _arm_clap(
        event["id"], mid, channel, event["guild_id"], event["brief"], status, participants
    )
    return completed_view(event["id"], clap=True)


async def handle_clap_button(interaction: discord.Interaction) -> None:
    await _handle_clap(Press.from_interaction(interaction))


async def _handle_clap(press: Press) -> None:
    """A 👏 on a ✅-completed post. From a non-participant it awards every
    participant a +1 bonus punto (once per outsider), bumps the tally on the
    post (and the button's ×n label), and announces it with a fresh message; a
    participant clapping their own finish is refused."""
    channel = press.channel
    snap = await store.snapshot()
    rec0 = snap["claps"].get(str(press.message_id))
    if not rec0:
        if press.interaction is not None:
            await press.whisper("This post's 👏 has been retired.")
        return  # a 👏 on something we don't track — ignore
    if any(p["user_id"] == press.user_id for p in rec0["participants"]):
        # You can't clap your own chore.
        await press.retract()
        if press.interaction is not None:
            await press.whisper("👏 You can't clap your own work — that one's for the rest of the family.")
        return

    tz, now = _game_tz(snap, rec0["guild_id"]), now_utc()
    new_records: list[dict] = []
    body = announce = None
    repeat = gone = False
    async with store.txn() as data:
        rec = data["claps"].get(str(press.message_id))
        if not rec:
            gone = True  # retired/undone between the snapshot and here
        elif press.user_id in rec["clappers"]:
            repeat = True  # one clap per outsider — a repeat (or re-add) is a no-op
        elif any(p["user_id"] == press.user_id for p in rec["participants"]):
            gone = True  # a participant slipped in after the snapshot
        else:
            rec["clappers"].append(press.user_id)
            for p in rec["participants"]:
                r = _clap_record(rec, p, tz, now)
                rec.setdefault("log_ids", []).append(r["id"])
                new_records.append(r)
            body = clap_status(rec)
            announce = clap_announcement(rec["participants"], press.user_id)
    if repeat or gone:
        if press.interaction is not None:
            await press.whisper("👏 Once per person — you've already clapped this one."
                                if repeat else "This post's 👏 has been retired.")
        return
    for r in new_records:
        await store.log_completion(r)
    if body is not None:
        if rec0.get("ui") == "buttons":
            # One edit bumps both the tally line and the button's ×n label —
            # re-derived from the tables so armed ↩️/🔄 stay put.
            snap2 = await store.snapshot()
            await press.edit_pressed(
                content=body, view=post_view_for(snap2, press.message_id),
                allowed_mentions=NO_PINGS,
            )
        else:
            await press.edit_pressed(content=body, allowed_mentions=NO_PINGS)
    if announce is not None:
        try:
            await channel.send(announce, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            pass


__all__ = [
    "_arm_clap",
    "_arm_game_clap",
    "_clap_record",
    "_game_participants",
    "_handle_clap",
    "clap_announcement",
    "clap_status",
    "handle_clap_button",
]
