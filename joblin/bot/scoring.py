from __future__ import annotations

import itertools
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from .. import trinkets
from ..models import (
    UTC,
    now_utc,
)
from .core import (
    NO_PINGS,
    bot,
    store,
)
from .helpers import guild_config



# ---------------------------------------------------------------------------
# Leaderboard scoring — puntos (bounties count double) and monthly ⭐ stars
# ---------------------------------------------------------------------------
def _completion_points(rec: dict) -> int:
    """Puntos a logged completion is worth. Bounties record ``points: 2``; older
    records predate the field and count as the normal 1 punto."""
    p = rec.get("points")
    return int(p) if isinstance(p, (int, float)) and p > 0 else 1


def _rec_month(rec: dict) -> str:
    """The local-tz 'YYYY-MM' bucket a completion belongs to (tolerating very old
    records that only carry a 'ts')."""
    return rec.get("month") or str(rec.get("ts", ""))[:7]


def monthly_scores(records: list[dict], guild_id: int) -> dict[str, dict[int, dict]]:
    """Aggregate one guild's completions into
    ``{month: {user_id: {"points", "chores", "name"}}}``."""
    months: dict[str, dict[int, dict]] = {}
    for rec in records:
        if rec.get("guild_id") != guild_id:
            continue
        bucket = months.setdefault(_rec_month(rec), {})
        ent = bucket.setdefault(
            rec["user_id"], {"points": 0, "chores": 0, "name": str(rec["user_id"])}
        )
        ent["points"] += _completion_points(rec)
        ent["chores"] += 1
        ent["name"] = rec.get("user_name", ent["name"])
    return months


def star_counts(records: list[dict], guild_id: int, current_month: str) -> dict[int, int]:
    """Stars per user: one for each *past* month they led on puntos (a tie shares
    the star). The current (and any future) month isn't decided yet, so it's
    excluded — the title is still up for grabs until the month closes."""
    stars: dict[int, int] = {}
    for month, bucket in monthly_scores(records, guild_id).items():
        if not month or month >= current_month or not bucket:
            continue
        top = max(ent["points"] for ent in bucket.values())
        if top <= 0:
            continue
        for uid, ent in bucket.items():
            if ent["points"] == top:
                stars[uid] = stars.get(uid, 0) + 1
    return stars


def _guild_bar(cfg: Optional[dict]) -> int:
    """The guild's trinket bar (monthly puntos to earn one), defaulted & sane."""
    try:
        return max(1, int(cfg.get("item_bar")))  # type: ignore[union-attr]
    except (TypeError, ValueError, AttributeError):
        return trinkets.DEFAULT_BAR


def vitrine_for(records: list[dict], guild_id: int, user_id: int, bar: int,
                current_month: str) -> list[dict]:
    """Every trinket a user has earned: one deterministic roll per *whole multiple*
    of ``bar`` their puntos reached, for each *past* month (50 puntos against a
    25-punto bar → two). Like stars, it's derived from the log — the current
    month is still in play, so it's excluded. Sorted oldest→newest, idx 0…n−1
    within a month."""
    out: list[dict] = []
    for month, bucket in sorted(monthly_scores(records, guild_id).items()):
        if not month or month >= current_month:
            continue
        ent = bucket.get(user_id)
        if not ent:
            continue
        for idx in range(ent["points"] // bar):  # bar ≥ 1, guaranteed by _guild_bar
            out.append(trinkets.roll_for(guild_id, user_id, month, idx))
    return out


@bot.tree.command(name="leaderboard", description="Monthly chore puntos & ⭐ stars")
@app_commands.describe(month="Month as YYYY-MM (defaults to the current month)")
async def leaderboard(interaction: discord.Interaction, month: Optional[str] = None) -> None:
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    tz = ZoneInfo(cfg["timezone"]) if cfg and cfg.get("timezone") else UTC
    current_month = now_utc().astimezone(tz).strftime("%Y-%m")
    bar = _guild_bar(cfg)
    if month is None:
        month = current_month

    records = store.read_completions()
    months = monthly_scores(records, interaction.guild_id)
    stars = star_counts(records, interaction.guild_id, current_month)

    # All-time display names so a star holder shows even when idle this month.
    names = {uid: ent["name"] for bucket in months.values() for uid, ent in bucket.items()}
    star_line = ""
    if stars:
        holders = sorted(stars.items(), key=lambda kv: (-kv[1], names.get(kv[0], "").lower()))
        star_line = "⭐ **Stars** — " + " · ".join(f"<@{uid}> ×{n}" for uid, n in holders)

    bucket = months.get(month, {})
    if not bucket:
        msg = (f"No chores logged for **{month}** yet. Get to work! 🚜\n"
               + trinkets.zone_blurb(month, bar, past=month < current_month))
        if star_line:
            msg += "\n\n" + star_line
        await interaction.response.send_message(
            msg, ephemeral=True, allowed_mentions=NO_PINGS
        )
        return

    ranking = sorted(bucket.items(), key=lambda kv: (-kv[1]["points"], kv[1]["name"].lower()))
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, ent) in enumerate(ranking):
        badge = medals[i] if i < 3 else f"`{i + 1}.`"
        star = f" ⭐×{stars[uid]}" if stars.get(uid) else ""
        pts = ent["points"]
        lines.append(f"{badge} <@{uid}> — **{pts} punto{'' if pts == 1 else 's'}**{star}")

    total_pts = sum(ent["points"] for ent in bucket.values())
    total_chores = sum(ent["chores"] for ent in bucket.values())
    when = "this month" if month == current_month else f"in {month}"
    footer = (
        f"_{total_chores} chore{'' if total_chores == 1 else 's'} · "
        f"{total_pts} punto{'' if total_pts == 1 else 's'} {when}._"
    )
    if month == current_month:
        footer += "\n⭐ Whoever tops the board when the month ends earns a star."

    zone_note = trinkets.zone_blurb(month, bar, past=month < current_month)
    msg = f"🏆 **Chore leaderboard — {month}**\n{zone_note}\n" + "\n".join(lines)
    if star_line:
        msg += "\n\n" + star_line
    msg += "\n\n" + footer
    await interaction.response.send_message(msg, allowed_mentions=NO_PINGS)


@bot.tree.command(name="vitrine", description="Gaze upon a collection of trinkets won at month's end")
@app_commands.describe(user="Whose vitrine to view (default: yours)")
async def vitrine(interaction: discord.Interaction, user: Optional[discord.Member] = None) -> None:
    target = user or interaction.user
    snap = await store.snapshot()
    cfg = guild_config(snap, interaction.guild_id)
    tz = ZoneInfo(cfg["timezone"]) if cfg and cfg.get("timezone") else UTC
    current_month = now_utc().astimezone(tz).strftime("%Y-%m")
    bar = _guild_bar(cfg)

    records = store.read_completions()
    items = vitrine_for(records, interaction.guild_id, target.id, bar, current_month)

    whose = "Your" if target.id == interaction.user.id else f"{target.display_name}'s"
    header = f"🖼️ **{whose} vitrine** — {len(items)} trinket{'' if len(items) == 1 else 's'}"

    # Group by month, newest first: a header (with a ×N count when that month
    # yielded several) over its indented items. `items` is already month-sorted,
    # so consecutive grouping is sound. Rendered flat as (line, is_trinket) pairs
    # — body is always a prefix of these — then greedily trimmed to stay under
    # Discord's 2000-char message limit.
    blocks: list[list[tuple[str, bool]]] = []
    for month, grp in itertools.groupby(items, key=lambda t: t["month"]):
        group = list(grp)
        suffix = f"  ×{len(group)}" if len(group) > 1 else ""
        # Lead the header with that month's *featured* zone; each item line then
        # carries its own zone emoji, so an off-season stray stands out at a glance.
        season = trinkets.zone_emoji(trinkets.zone_for_month(month))
        block: list[tuple[str, bool]] = [(f"{season} **{month}**{suffix}", False)]
        block.extend((f"  {trinkets.render_line(t)}", True) for t in group)
        blocks.append(block)
    # Newest month on top, but each header still leads its own items (idx 0…n) —
    # reverse the *group order*, not the flat lines.
    rendered = [pair for block in reversed(blocks) for pair in block]

    body: list[str] = []
    shown = used = 0
    for line, is_trinket in rendered:
        if body and used + len(line) + 1 > 1700:
            break
        body.append(line)
        used += len(line) + 1
        shown += is_trinket
    # Never strand a month header whose items got trimmed away.
    if body and not rendered[len(body) - 1][1]:
        body.pop()
    if not items:
        body.append("_The cabinet stands empty… for now._")
    elif shown < len(items):
        n = len(items) - shown
        body.append(f"… and {n} older trinket{'' if n == 1 else 's'}.")

    # Progress toward this month's (still-pending) trinkets — one per multiple of
    # the bar, so a high scorer is already stacking several.
    ent = monthly_scores(records, interaction.guild_id).get(current_month, {}).get(target.id)
    pts = ent["points"] if ent else 0
    secured = pts // bar
    to_next = bar - pts % bar  # 1…bar: puntos until the next trinket tips over
    zk = trinkets.zone_for_month(current_month)
    z = f"{trinkets.zone_emoji(zk)} {current_month}: **{trinkets.zone_label(zk)}** in season"
    if secured == 0:
        foot = f"{z} — **{pts}/{bar} puntos**, {to_next} to go for your first trinket"
    else:
        foot = (f"{z} — at **{pts} puntos** you've secured "
                f"**{secured} trinket{'' if secured == 1 else 's'}** ✨, "
                f"**{to_next}** more for the next")

    msg = header + "\n" + "\n".join(body) + "\n\n" + foot
    await interaction.response.send_message(msg, allowed_mentions=NO_PINGS)


__all__ = [
    "_completion_points",
    "_guild_bar",
    "_rec_month",
    "leaderboard",
    "monthly_scores",
    "star_counts",
    "vitrine",
    "vitrine_for",
]
