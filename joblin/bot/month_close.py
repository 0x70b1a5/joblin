"""Month-close ceremony — announce stars and trinkets when a guild-local month ends.

Trinkets and stars are *derived* (never stored), so nothing is "awarded" into
persistence at month close. What *does* happen is a channel announcement: once
the guild clock rolls into a new month, everyone who cleared the bar gets their
rolled 🖼️ trinkets named out loud, and the ⭐ star winner(s) are crowned.

Restart-safe like the nightly backup: a persisted ``last_month_close_announced``
(YYYY-MM) is compared against the guild's current local month each scheduler
tick. Months strictly after that watermark and strictly before the current
month are announced (oldest first), then the watermark advances — so a crash
mid-post simply retries the remaining months, and a long downtime only ever
announces the months it missed (never re-announces). First sight of a ready
guild *arms* the watermark at the previous month without posting, so installing
the bot mid-year never dumps years of history into the channel.

The announcement is an embed (or a short stack of them) so a big family with
stacked trinkets isn't capped by Discord's 2000-char plain-message limit.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from .. import trinkets
from .core import NO_PINGS, bot, log, store
from .helpers import config_ready
from .scoring import bar_for, monthly_scores

# Embed packing budget (leave headroom under Discord's 4096 description cap).
_DESC_BUDGET = 3900
_MAX_EMBEDS = 10
_COLOR = 0xC9A227  # warm gold — end-of-month ceremony


# ---------------------------------------------------------------------------
# Pure month arithmetic
# ---------------------------------------------------------------------------
def _prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


def _next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}"


def _months_between(after: str, before: str) -> list[str]:
    """YYYY-MM months strictly after ``after`` and strictly before ``before``."""
    out: list[str] = []
    try:
        cur = _next_month(after)
        # Cap the walk so a mangled watermark can't loop forever.
        for _ in range(240):  # 20 years is plenty
            if cur >= before:
                break
            out.append(cur)
            cur = _next_month(cur)
    except (TypeError, ValueError):
        return []
    return out


# ---------------------------------------------------------------------------
# Pure embed builder
# ---------------------------------------------------------------------------
def build_month_close_embeds(
    records: list[dict],
    guild_id: int,
    cfg: Optional[dict],
    closed_month: str,
) -> list[discord.Embed]:
    """Embeds announcing stars + trinkets for a just-closed ``YYYY-MM``.

    Returns ``[]`` when nobody logged any puntos that month (stay quiet).
    Pure apart from reading nothing — no store, no clock.
    """
    bucket = monthly_scores(records, guild_id).get(closed_month, {})
    if not bucket:
        return []

    bar = bar_for(cfg, closed_month)
    ranking = sorted(
        bucket.items(), key=lambda kv: (-kv[1]["points"], kv[1]["name"].lower())
    )
    top = ranking[0][1]["points"]
    star_uids = {uid for uid, ent in ranking if ent["points"] == top and top > 0}

    lines: list[str] = [trinkets.zone_blurb(closed_month, bar, past=True), ""]

    if star_uids:
        # Preserve ranking order so a multi-way tie lists highest-name-stable first.
        ordered = [uid for uid, _ in ranking if uid in star_uids]
        label = "Star of the month" if len(ordered) == 1 else "Stars of the month"
        lines.append(f"⭐ **{label}** — " + " · ".join(f"<@{uid}>" for uid in ordered))
        lines.append("")

    lines.append("**Final standings**")
    medals = ["🥇", "🥈", "🥉"]
    for i, (uid, ent) in enumerate(ranking):
        badge = medals[i] if i < 3 else f"`{i + 1}.`"
        star = " ⭐" if uid in star_uids else ""
        n_tri = ent["points"] // bar
        tri = f" · 🖼️×{n_tri}" if n_tri else ""
        pts = ent["points"]
        lines.append(
            f"{badge} **{pts} punto{'' if pts == 1 else 's'}** — <@{uid}>{star}{tri}"
        )

    # Per-person trinket rolls — same deterministic function /covet uses.
    awarded: list[tuple[int, dict, list[dict]]] = []
    for uid, ent in ranking:
        n = ent["points"] // bar
        if n <= 0:
            continue
        items = [trinkets.roll_for(guild_id, uid, closed_month, idx) for idx in range(n)]
        awarded.append((uid, ent, items))

    lines.append("")
    if awarded:
        lines.append("🖼️ **Trinkets awarded**")
        for uid, ent, items in awarded:
            n = len(items)
            lines.append(
                f"**{ent['name']}** · <@{uid}>"
                + (f" · ×{n}" if n > 1 else "")
            )
            for t in items:
                lines.append(f"  {trinkets.render_line(t)}")
    else:
        lines.append(
            f"_Nobody cleared the **{bar}**-punto bar — the vitrine waits._"
        )

    pages = _pack_lines(lines)
    title = f"🖼️ Month closed — {closed_month}"
    embeds = [
        discord.Embed(
            title=title if i == 0 else None,
            description=body,
            color=_COLOR,
        )
        for i, body in enumerate(pages[:_MAX_EMBEDS])
    ]
    if embeds:
        embeds[-1].set_footer(
            text=f"/covet to gaze · /leaderboard month:{closed_month} for the board"
        )
    if len(pages) > _MAX_EMBEDS:
        embeds[-1].description = (
            (embeds[-1].description or "")
            + "\n\n_…and more — open `/covet` for the full cabinet._"
        )
    return embeds


def _pack_lines(lines: list[str]) -> list[str]:
    """Greedy pack into description-sized pages (never splits mid-line)."""
    pages: list[str] = []
    cur = ""
    for line in lines:
        if cur and len(cur) + 1 + len(line) > _DESC_BUDGET:
            pages.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        pages.append(cur)
    return pages


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------
async def run_month_closes(now: dt.datetime, snap: dict) -> None:
    """Called once per scheduler tick. For each ready guild, announce any
    closed months that haven't been announced yet."""
    for gid_str, cfg in list(snap["configs"].items()):
        if not config_ready(cfg):
            continue
        try:
            tz = ZoneInfo(cfg["timezone"])
        except Exception:
            continue

        current = now.astimezone(tz).strftime("%Y-%m")
        last = cfg.get("last_month_close_announced")

        # First sight: arm the watermark without dumping history.
        if last is None:
            try:
                armed = _prev_month(current)
            except (TypeError, ValueError):
                continue
            async with store.txn() as data:
                c = data["configs"].get(gid_str)
                if c is not None and "last_month_close_announced" not in c:
                    c["last_month_close_announced"] = armed
            continue

        pending = _months_between(str(last), current)
        if not pending:
            continue

        channel = bot.get_channel(int(cfg["channel_id"]))
        if channel is None:
            # Cache not warm — leave the watermark so the next tick retries.
            continue

        try:
            await _announce(int(gid_str), cfg, channel, pending)
        except Exception:  # never let a ceremony error kill the scheduler loop
            log.exception("month-close announcement failed for guild %s", gid_str)


async def _announce(
    guild_id: int,
    cfg: dict,
    channel: discord.abc.Messageable,
    months: list[str],
) -> None:
    records = store.read_completions()
    for ym in months:
        embeds = build_month_close_embeds(records, guild_id, cfg, ym)
        if embeds:
            await channel.send(embeds=embeds, allowed_mentions=NO_PINGS)
        # Advance after each month so a crash mid-batch doesn't re-announce
        # the ones that already posted. An empty month still advances (quiet).
        async with store.txn() as data:
            c = data["configs"].get(str(guild_id))
            if c is not None:
                c["last_month_close_announced"] = ym


__all__ = [
    "build_month_close_embeds",
    "run_month_closes",
    "_months_between",
    "_next_month",
    "_prev_month",
]
