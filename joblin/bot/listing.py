from __future__ import annotations

import datetime as dt
from typing import Optional

import discord

from ..models import (
    EMOJI_FLEX,
    EMOJI_HANDSHAKE,
    describe_repeat,
    discord_ts,
    from_iso,
    now_utc,
    recurrence_of,
)
from .core import (
    NO_PINGS,
    bot,
    store,
)
from .helpers import (
    guild_config,
    schedule_label,
    web_base_url,
)



# ---------------------------------------------------------------------------
# Listing — every task (paginated) and just the open ones (with jump links)
# ---------------------------------------------------------------------------
def message_link(guild_id, channel_id, message_id) -> Optional[str]:
    """A click-to-jump URL for a posted message, or None if we lack a piece."""
    if not (guild_id and channel_id and message_id):
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _safe_link_label(text: str) -> str:
    """Escape a brief for use as a masked-link label: a label ends at the first
    unescaped ``]``, so a chore like ``buy milk [today]`` would otherwise break
    or truncate the jump link."""
    return text.replace("[", "\\[").replace("]", "\\]")


def _chunk_rows(rows: list[str], *, budget: int = 1700) -> list[list[str]]:
    """Greedily pack rows into groups that each render well under Discord's
    2000-char message limit (leaving headroom for the header + a page footer)."""
    chunks: list[list[str]] = []
    cur: list[str] = []
    used = 0
    for r in rows:
        if cur and used + len(r) + 1 > budget:
            chunks.append(cur)
            cur, used = [], 0
        cur.append(r)
        used += len(r) + 1
    if cur:
        chunks.append(cur)
    return chunks


class ListPaginator(discord.ui.View):
    """◀/▶ pager over pre-rendered page bodies for an ephemeral list. The list is
    ephemeral, so only its requester can see (or press) it; ``interaction_check``
    is a belt-and-suspenders guard. The buttons grey out once the view times out so
    a dead page can't mislead."""

    def __init__(self, pages: list[str], *, user_id: int, timeout: float = 300.0) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages
        self.user_id = user_id
        self.index = 0
        self.origin: Optional[discord.Interaction] = None
        self.prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary)
        self.next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary)
        self.prev_btn.callback = self._go_prev
        self.next_btn.callback = self._go_next
        self.add_item(self.prev_btn)
        self.add_item(self.next_btn)
        self._sync()

    def _sync(self) -> None:
        self.prev_btn.disabled = self.index <= 0
        self.next_btn.disabled = self.index >= len(self.pages) - 1

    def body(self) -> str:
        return self.pages[self.index]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "That isn't your list — run the command yourself to page through it.",
                ephemeral=True,
            )
            return False
        return True

    async def _go_prev(self, interaction: discord.Interaction) -> None:
        self.index = max(0, self.index - 1)
        self._sync()
        await interaction.response.edit_message(content=self.body(), view=self)

    async def _go_next(self, interaction: discord.Interaction) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync()
        await interaction.response.edit_message(content=self.body(), view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.origin is not None:
            try:
                await self.origin.edit_original_response(view=self)
            except discord.HTTPException:
                pass


@bot.tree.command(name="listtasks", description="List every task, pitch-in & do-em-up with its id, schedule, and state")
async def listtasks(interaction: discord.Interaction) -> None:
    snap = await store.snapshot()
    mine = [t for t in snap["tasks"].values() if str(t["guild_id"]) == str(interaction.guild_id)]

    def sort_key(t: dict):
        if t.get("pending"):
            return (0, from_iso(t["pending"]["due_at"]))
        if t.get("next_due"):
            return (1, from_iso(t["next_due"]))
        return (2, now_utc())

    mine.sort(key=sort_key)

    rows = []
    for t in mine:
        if t.get("pending"):
            state = f"⏳ pending since {discord_ts(from_iso(t['pending']['due_at']), 'R')}"
        elif t.get("next_due"):
            state = f"next {discord_ts(from_iso(t['next_due']), 'R')}"
        else:
            state = "—"
        info = " ℹ️" if t.get("description") else ""
        flag = " 💰" if t.get("bounty") else ""
        shush = " 🤫" if t.get("no_nag") else ""
        nags = t.get("nag_count", 0)
        nag = f" · 🔔×{nags}" if nags else ""
        rows.append(
            f"• `{t['id']}` **{t['brief']}**{info}{flag}{shush} — {schedule_label(t)} · {state}{nag}"
        )

    # Pitch-ins and do-em-ups list alongside tasks now (each with its id) so they
    # can be retimed/renamed with /edit or torn down with /deletetask. Sorted
    # together by their next relevant time, appended after the chores.
    game_rows: list[tuple[dt.datetime, str]] = []
    for section, icon, close_key in (("pitchins", EMOJI_HANDSHAKE, "expires_at"),
                                     ("doemups", EMOJI_FLEX, "deadline")):
        for g in snap[section].values():
            if str(g["guild_id"]) != str(interaction.guild_id) or g.get("ended"):
                continue
            sched = describe_repeat(recurrence_of(g)) if g.get("recurring") else "one-off"
            close = g.get(close_key)
            if g.get("message_id"):  # a round is open right now
                when = from_iso(close) if close else now_utc()
                state = f"🟢 open · closes {discord_ts(from_iso(close), 'R')}" if close else "🟢 open"
            elif g.get("next_due"):  # scheduled, or recurring & dormant between rounds
                when = from_iso(g["next_due"])
                state = f"next {discord_ts(when, 'R')}"
            else:
                when, state = now_utc(), "—"
            info = " ℹ️" if g.get("description") else ""
            game_rows.append(
                (when, f"• `{g['id']}` {icon} **{g['brief']}**{info} — {sched} · {state}")
            )
    rows.extend(row for _, row in sorted(game_rows, key=lambda x: x[0]))

    # The web UI (when configured) is the comfier place to browse this list —
    # <angle brackets> keep the repeated link from unfurling an embed each time.
    web = web_base_url()
    web_note = f"\n_✏️ Browse & edit any time at <{web}>_" if web else ""

    if not rows:
        await interaction.response.send_message(
            "No tasks or events yet. Create one with `/newtask`, `/pitchin`, or `/doemup`."
            + web_note,
            ephemeral=True,
        )
        return

    head = f"**Farm tasks & events** — edit with `/edit`, remove with `/deletetask`{web_note}\n"
    chunks = _chunk_rows(rows)
    multi = len(chunks) > 1
    pages: list[str] = []
    for i, chunk in enumerate(chunks):
        foot = (
            f"\n\n_Page {i + 1}/{len(chunks)} · {len(rows)} items · 🔔 = times nagged_"
            if multi else ""
        )
        pages.append(head + "\n".join(chunk) + foot)

    # One page: a plain ephemeral message. Several: attach the ◀/▶ pager so every
    # id stays reachable instead of being cut off at a character budget.
    if not multi:
        await interaction.response.send_message(
            pages[0], ephemeral=True, allowed_mentions=NO_PINGS
        )
        return
    view = ListPaginator(pages, user_id=interaction.user.id)
    view.origin = interaction
    await interaction.response.send_message(
        pages[0], ephemeral=True, allowed_mentions=NO_PINGS, view=view
    )


def _open_embeds(blocks: list[str], total: int) -> list[discord.Embed]:
    """Pack section blocks into as few embeds as fit Discord's 4096-char
    description cap (masked jump links render only inside embeds), the first titled
    with the open count. Packs line-by-line so a header never separates from its
    items, and caps at 10 embeds — far beyond any real farm — flagging overflow
    rather than dropping it silently."""
    title = f"🗒️ Open tasks ({total})"
    pages: list[str] = []
    cur = ""
    for line in "\n\n".join(blocks).split("\n"):
        if cur and len(cur) + 1 + len(line) > 3900:
            pages.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        pages.append(cur)
    embeds = [
        discord.Embed(title=title if i == 0 else None, description=body, color=0x6B8E23)
        for i, body in enumerate(pages[:10])
    ]
    if len(pages) > 10:
        embeds[-1].description += "\n\n_…and more — run `/listopen` again after clearing some._"
    return embeds or [discord.Embed(title=title, description="—", color=0x6B8E23)]


@bot.tree.command(
    name="listopen",
    description="Post a checklist of everything open now, each linking to where to do it",
)
async def listopen(interaction: discord.Interaction) -> None:
    snap = await store.snapshot()
    gid = interaction.guild_id
    cfg = guild_config(snap, gid)
    fallback_ch = cfg.get("channel_id") if cfg else None

    chores: list[tuple[dt.datetime, str]] = []
    pitch: list[tuple[dt.datetime, str]] = []
    doem: list[tuple[dt.datetime, str]] = []

    # Open chores = an occurrence that has fired and is awaiting a ✅. The jump
    # link points at the ORIGINAL post (message_ids[0]) where it gets resolved —
    # never one of the hourly nags below it (that's the whole point: skip the scroll).
    for t in snap["tasks"].values():
        if str(t["guild_id"]) != str(gid):
            continue
        p = t.get("pending")
        mids = (p or {}).get("message_ids") or []
        if not p or not mids:
            continue
        link = message_link(t["guild_id"], p.get("channel_id") or fallback_ch, mids[0])
        due = from_iso(p["due_at"])
        info = " ℹ️" if t.get("description") else ""
        flag = " 💰" if t.get("bounty") else ""
        label = _safe_link_label(t["brief"])
        head = f"[{label}]({link})" if link else f"**{label}**"
        chores.append((due, f"• {head}{info}{flag} — ⏳ since {discord_ts(due, 'R')}"))

    # Live pitch-ins / do-em-ups: posted, not yet closed (a dormant recurring round
    # has no message_id, so it's skipped — nothing to act on until it re-opens).
    for section, bucket, icon, key in (
        ("pitchins", pitch, EMOJI_HANDSHAKE, "expires_at"),
        ("doemups", doem, EMOJI_FLEX, "deadline"),
    ):
        for g in snap[section].values():
            if str(g["guild_id"]) != str(gid) or g.get("ended") or not g.get("message_id"):
                continue
            link = message_link(g["guild_id"], g.get("channel_id") or fallback_ch, g["message_id"])
            label = _safe_link_label(g["brief"])
            head = f"[{label}]({link})" if link else f"**{label}**"
            when = g.get(key)
            sort_at = from_iso(when) if when else now_utc()
            closes = f" — closes {discord_ts(from_iso(when), 'R')}" if when else ""
            bucket.append((sort_at, f"• {icon} {head}{closes}"))

    web = web_base_url()
    total = len(chores) + len(pitch) + len(doem)
    if total == 0:
        note = f"\n_✏️ Peek at what's ahead: <{web}>_" if web else ""
        await interaction.response.send_message(
            "✅ Nothing's open right now — you're all caught up! 🎉" + note,
            allowed_mentions=NO_PINGS,
        )
        return

    sections: list[tuple[str, list[tuple[dt.datetime, str]]]] = [
        ("⏳ **Chores awaiting a ✅**", chores),
        (f"{EMOJI_HANDSHAKE} **Pitch-ins open now**", pitch),
        (f"{EMOJI_FLEX} **Do-em-ups open now**", doem),
    ]
    blocks = [
        title + "\n" + "\n".join(ln for _, ln in sorted(items, key=lambda x: x[0]))
        for title, items in sections
        if items
    ]
    if web:  # masked links render fine inside an embed, no unfurl to suppress
        blocks.append(f"✏️ [Browse & edit the schedule]({web})")
    await interaction.response.send_message(
        embeds=_open_embeds(blocks, total), allowed_mentions=NO_PINGS
    )


@bot.tree.command(name="joblinhelp", description="How to use Joblin — commands, scheduling, and reactions")
async def joblinhelp(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="🚜 Joblin help",
        description=(
            "Create one-off or recurring chores. When one is due I post it in the "
            "farm channel and self-react with buttons the family taps."
        ),
        color=0x6B8E23,
    )
    embed.add_field(
        name="Commands",
        value=(
            "• `/newtask` — add a chore (see scheduling below; `bounty:true` for a 2-punto bounty)\n"
            "• `/pitchin` — group task: everyone who ✅s before it closes scores\n"
            "• `/doemup` — per-unit task: tap ➕ for each one you do\n"
            "• `/listtasks` — list chores with their ids (paged; 🔔×n = times nagged)\n"
            "• `/listopen` — post what's open right now, each a tap-through link to its post\n"
            "• `/edit` — change a task, pitch-in, or do-em-up (`/edit task|pitchin|doemup`)\n"
            "• `/deletetask` — remove a chore for good\n"
            "• `/leaderboard` — monthly puntos ranking & ⭐ stars 🏆\n"
            "• `/covet` — your cabinet of month's-end trinkets 🖼️\n"
            "• `/joblinconfig` — channel, timezone, reminder role, trinket bar *(Manage Server)*\n"
            "• `/joblinhelp` — this message"
        ),
        inline=False,
    )
    embed.add_field(
        name="`at` — when / what time (defaults to now)",
        value=(
            "`now` · `in 2h` · `+3d` · `tonight` · `18:00` · `6pm` · `tomorrow 8am` · "
            "`fri 19:00` · `next monday` · `Jun 20 14:00` · `2026-06-20 14:00`"
        ),
        inline=False,
    )
    embed.add_field(
        name="`repeat` — how often (defaults to once)",
        value=(
            "`once` · `daily` · `every 2 days` · `weekly` · `weekdays` · `weekends` · "
            "`mon,thu` · `every tuesday` · `monthly` · `monthly on the 1st` · `1st,15th`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Reactions on a posted chore",
        value=(
            "✅ **Done** — logs who did it (counts on the leaderboard)\n"
            "⏩ **Snooze** — opens a number-pad panel; pick hours or days\n"
            "ℹ️ **Info** — shows the longer description, if any\n"
            "⏭️ **Skip** — recurring chore: skips just this time\n"
            "❌ **Delete** — one-off chore: cancels it\n"
            "🤫 **Shush** — appears on a reminder: mutes the hourly nags for that "
            "chore for good (it still posts when due); tap 🤫 on a live post to "
            "turn them back on\n"
            "↩️ **Undo** — appears after ✅/⏩/⏭️/❌ to reverse it\n"
            "🔄 **Requeue** — appears on a completed chore; re-posts it right now\n"
            "👏 **Clap** — on a finished chore, pitch-in, or do-em-up; anyone who "
            "*didn't* do it taps to tip every doer a bonus punto (one clap each)"
        ),
        inline=False,
    )
    embed.add_field(
        name="💰 Bounties & ⭐ stars",
        value=(
            "Mark a chore you can't do yourself with `bounty:true`: it's worth "
            "**2 puntos** and only **someone else** can tap ✅ on it. Every completed "
            "chore is a punto (bounties two); whoever leads the month's `/leaderboard` "
            "earns a permanent **⭐ star** shown there for keeps."
        ),
        inline=False,
    )
    embed.add_field(
        name="Pitch-ins & do-em-ups (bonus puntos 🏆)",
        value=(
            "• `/pitchin brief:\"laundry bonanza\"` — everyone who taps ✅ before it "
            "closes earns a punto. Optional `expires` (default 24h), `puntos` each, "
            "and `max_scorers` (only the first N score). 🏁 ends it early.\n"
            "• Add `repeat:` to either (same as a chore — `daily`, `weekdays`, "
            "`mon,thu`, `monthly on the 1st`) and it re-posts a fresh round each "
            "slot. 🏁 just closes the current round (it rolls on); stop the whole "
            "series with `/deletetask`.\n"
            "• Add `at:` to either to set the slot — e.g. `/pitchin … at:06:00 "
            "expires:06:05 repeat:daily` opens 06:00–06:05 every day. The first round "
            "waits for that time instead of posting the moment you create it.\n"
            "• `/doemup brief:\"thistle bush removed\"` — tap ➕ once per one you did "
            "(➖ to fix); the tally updates live. Optional `puntos` each, `deadline`, "
            "and `point_limit` (auto-closes at that total). 🏁 ends it.\n"
            "• Change one later with `/edit pitchin` / `/edit doemup` — retime (`at`), "
            "rename, or adjust puntos/cap; a schedule change applies from the next "
            "round.\n"
            "Puntos from both feed the `/leaderboard` — and a closed round grows a "
            "👏 anyone who sat it out can tap to tip every scorer a bonus punto."
        ),
        inline=False,
    )
    embed.add_field(
        name="🖼️ Trinkets & the vitrine",
        value=(
            "Clear the month's **bar** of puntos (default **25**, set with "
            "`/joblinconfig item_bar:`; closed months keep the bar they ended under) "
            "and when the month closes an inert **trinket** "
            "— a rolled *objet d'art* — lands in your `/covet`; clear it several "
            "times over (50 puntos on a 25-punto bar) and you collect that many. Each "
            "month a different **zone** is *in season* (the Bean Zone, the Vaults, the "
            "Menagerie…), shown on the `/leaderboard`: ~7 in 10 of your trinkets are "
            "rolled from it, the rest stray in from other zones. Trinkets cost no "
            "puntos and do nothing but delight; the ⭐ star still goes to the top scorer."
        ),
        inline=False,
    )
    embed.set_footer(
        text="e.g.  /newtask brief:Trash out at:19:00 repeat:mon,thu   ·   "
        "/pitchin brief:Laundry bonanza expires:tonight"
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


__all__ = [
    "ListPaginator",
    "_chunk_rows",
    "_open_embeds",
    "_safe_link_label",
    "joblinhelp",
    "listopen",
    "listtasks",
    "message_link",
]
