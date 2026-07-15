# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-guild-friendly Discord bot for small-farm/household chore logistics: one-off and recurring tasks that post to a channel when due, get resolved by emoji reactions, and feed a family puntos economy (leaderboard ⭐ stars, end-of-month 🖼️ trinkets, plus ad-hoc punto events). Pure stdlib + `discord.py` (+ its `aiohttp`); persistence is a JSON file plus an append-only log. No database. An optional phone-first web UI (`joblin/web/`, Discord-OAuth-gated) serves the schedule from *inside* the bot process when `WEB_BASE_URL`/`DISCORD_CLIENT_ID`/`DISCORD_CLIENT_SECRET` are set — otherwise no port is ever opened.

## Commands

Always use `uv` (never plain `pip`/`venv`).

```bash
uv run python -m joblin      # run the bot (needs DISCORD_TOKEN in .env)
uv run python tests/smoke.py      # run the whole test suite
uv sync                           # install/sync deps into .venv
```

- **Tests**: `tests/smoke.py` is a single script of ~45 plain-`assert` functions (no pytest). Running it imports `joblin.bot`, which executes every `@bot.tree.command` decorator — so it doubles as a smoke test that all slash commands still register. To run **one** test, there's no CLI selector; temporarily call just that function from `main()` at the bottom, or `uv run python -c "import tests.smoke as s; s.test_first_due()"`. **Add new tests by defining `test_*` and registering them in `main()`** (the list there is the runner).
- **Setup**: copy `.env.example` → `.env`, set `DISCORD_TOKEN`. Set `DEV_GUILD_ID` to sync slash commands to one guild instantly (global sync takes ~1h to propagate). `JOBLIN_DATA_DIR` (default `./data`) holds `store.json` + `completions.jsonl` (+ the auto-generated `web_secret`). The web UI needs `WEB_BASE_URL`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET` (and the redirect URI registered in the Developer Portal — see `.env.example`); `WEB_HOST`/`WEB_PORT` (default `0.0.0.0:8710`) move the listener.
- **Deploy**: production runs under `./run.sh` (a tmux supervisor loop: git pull → `uv sync` → run, restart on exit). `./redeploy.sh` (or the owner-only `/redeploy` slash command) just stops the bot so the loop pulls and restarts. There is no build/lint step.

## Architecture

**Single asyncio event loop, no threads.** This is the load-bearing assumption everywhere. There's no OS-thread parallelism to guard against — only coroutine interleaving across `await`.

**Persistence (`store.py`).** Two files: `store.json` (a single dict: configs, tasks, live games, and reaction-routing tables) and `completions.jsonl` (append-only ledger of every punto-earning event — the source of truth for all stats). The in-memory `store.data` is canonical during a run; every change is flushed atomically (temp file → `fsync` → `os.replace`). Two access patterns, and using the right one matters:
- `async with store.txn() as data:` — mutate under the lock, flush on clean exit. **Keep network/Discord `await`s OUT of the txn body.** The pattern across the codebase is: snapshot → do Discord I/O → re-enter a tiny txn to commit the result.
- `await store.snapshot()` — a deep copy you can read freely without holding the lock.

**Scheduling math (`models.py`).** The home of all the tricky parts: parsing free-form `at`/`repeat` strings and turning a recurrence rule into a concrete next-fire instant, **DST-aware**. Tasks are plain dicts (JSON round-trips with zero friction); the **task dict schema and the `pending` sub-schema are documented in the module docstring** — read it before touching task fields. Times are stored as ISO-8601 **UTC**; wall-clock interpretation always happens in the guild's timezone. `recurrence_of()` reads legacy tasks (pre-`freq`) as the equivalent rule, so nothing on disk needs migrating. `first_due`/`compute_first_due` give a brand-new task created "now" a one-minute grace so it fires immediately instead of next cycle; `next_due`/`roll_forward` keep strict semantics so occurrences never double-fire or replay a backlog.

**The bot package (`joblin/bot/`).** Submodules are imported in dependency order by `bot/__init__.py` purely so their `@bot.tree.command`/`@bot.event` decorators register against the one shared `bot` instance in `core.py`. Each submodule exposes `__all__`, which `__init__.py` re-exports flat (so `bot.<name>` and the tests resolve). Tests swap the store via `bot.store = ...`; `__init__.py` forwards that assignment to every submodule, so handlers always read the live store.

**Occurrence lifecycle (the heart of the system).** The scheduler (`@tasks.loop(seconds=30)`) compares `now` against persisted `next_due`/`remind_at`/game deadlines, which makes the whole thing **naturally restart-safe** — no in-memory timers to lose.
1. `now >= next_due` → **fire**: post the brief, self-react ✅ ⏩ ℹ️ ❌. Task flips to `pending` (`remind_at = due + 1h`); `next_due` cleared so it can't re-fire.
2. While pending, each tick checks `remind_at` → posts a fresh **nag** (optionally pinging a role, self-reacting an extra 🤫), resets `remind_at = now + 1h`, bumps `nag_count`. A task with `no_nag` set is never nagged (it still fires — only the reminders stop).
3. **Reactions** resolve/defer it (`reactions.py`): ✅ complete (logs it; recurring rolls to next slot, one-off is deleted), ⏩ snooze (opens a number-pad panel, doubling backoff), ℹ️ info, ❌ skip, ↩️ undo, 🔄 requeue, 👏 clap, 🤫 shush / 🔊 un-shush (set/clear the lifetime `no_nag` flag; a shushed task's posts self-react 🔊 instead of nagging with 🤫).

Everything keys off `store["messages"][message_id] → task_id`, so reactions keep working across restarts. **Undo** stashes a deep copy of the task *before* each mutating action in `store["undo"]` and self-reacts ↩️; it restores that snapshot (after `can_undo` confirms the occurrence hasn't moved on) and voids the matching completion-log entry.

**The puntos economy is sacred — puntos are never created from nothing or spent.** Each chore = 1 punto (bounties = 2). ⭐ stars and 🖼️ trinkets are **derived, never stored**: stars are recomputed from the completion log on each leaderboard draw (so undos correct the standings); trinkets are a *deterministic* `sha256(guild, user, year-month, idx)` roll, so the same trinket comes back on every view/restart/machine with no persisted award state. The trinket **bar** is a value-with-history (`bar_history` in the guild config, appended by `/joblinconfig`): each month is judged by the bar in force at its guild-local close (`scoring.bar_for`), so re-barring never rewrites a closed month, while the open month floats with the latest change. When changing scoring, preserve this — see the existing memory notes on the claps/trinkets exceptions.

## Module legend

| File | Responsibility |
|---|---|
| `models.py` | Task dict schema, emoji constants, free-form time parsing, DST-aware recurrence math. The only place "tricky" lives. |
| `store.py` | `Store`: the JSON doc + append-only JSONL log, `txn()`/`snapshot()`, atomic writes, completion logging/voiding. |
| `trinkets.py` | Deterministic end-of-month trinket generator + vitrine (SHA256-seeded; never builtin `hash()`). |
| `bot/core.py` | Shared singletons: the `JoblinBot`, the `store`, constants, timezone list. Dependency-free to avoid import cycles. |
| `bot/__init__.py` | Wires submodules together (decorator registration), flat re-export, store hot-swap for tests. Top docstring = the occurrence/undo lifecycle. |
| `bot/scheduler.py` | The 30s tick: fire due tasks, send nags, sweep games, run nightly backups. |
| `bot/backup.py` | Nightly (~23:59 guild-local) self-backup: if the completion log changed since the last run, zip `store.json` + `completions.jsonl` and post it to the channel as an attachment, then auto-post the leaderboard. Restart-safe via a persisted `next_backup_at`. |
| `bot/reactions.py` | Raw-reaction dispatcher → per-emoji handlers (done/snooze/info/skip/undo/requeue/clap). |
| `bot/commands/` | The slash-command surface, a subpackage wired like `bot/` itself (children imported for decorator registration, `__all__` re-exported flat): `lookup` (free-text task/game finders + shared autocompletes), `config` (`/joblinconfig`), `tasks` (`schedule_from_rule`, `/newtask`, `/deletetask`), `games` (`/pitchin`, `/doemup` — the round engine stays in `bot/games.py`), `edit` (the `/edit` group + shared engine). |
| `bot/games.py` | Pitch-ins & do-em-ups (ad-hoc punto events): posting, button views (`DoEmUpButton`), closing on expiry/cap/deadline/manual end. |
| `bot/claps.py` | 👏 bonus-punto tips from non-participants on completed posts/closed games. |
| `bot/scoring.py` | `/leaderboard` (monthly puntos + ⭐ stars) and `/vitrine`; star/score aggregation; the ⬆️/⬇️/🔥×N rank spice, replayed from log timestamps against a day frame that rolls at the nightly 23:59 post. |
| `bot/listing.py` | `/listtasks` (paginated), `/listopen`, `/joblinhelp`. |
| `bot/admin.py` | `main()` entry point, owner-only `/redeploy`, global app-command error handler, `on_ready`. |
| `bot/helpers.py` | Small formatting/occurrence-I/O helpers (schedule labels, post rendering, safe delete, reaction setup). |
| `web/` | The optional bundled web UI: `__init__.py` (aiohttp server on the bot's loop — Discord OAuth + signed-cookie sessions, JSON API mirroring `/newtask`, `/edit task`, `/deletetask`, `/edit pitchin|doemup` (via the shared `apply_game_edit` engine in `bot/commands/edit.py`) and the game branch of `/deletetask`; started by `core.setup_hook`, always reads `core.store` so the tests' store swap holds) and `index.html` (the whole frontend: one vanilla-JS mobile-first page, no build step). View + task/game CRUD — completing chores/earning puntos stays Discord-only. |

## Domain concepts (vocabulary you'll meet)

- **Task / chore** — one-off or recurring; recurrence is "every N days", specific weekdays, or specific month-days (31 clamps to the real last day), each with an optional `time_of_day`.
- **Bounty** — a 2-punto chore the creator is barred from completing (so someone *else* does it).
- **Pitch-in** — a shared call to action posted immediately; everyone who taps ✅ before it closes (expiry / max scorers / 🏁 manual end) earns its punto value.
- **Do-em-up** — a live unit tally posted immediately with ➕/➖ buttons; scorers earn per unit, closes on deadline / punto limit / manual end.
- **Clap (👏)** — an outsider tap on a finished post that tips each doer +1 (once per outsider per post).
- Pitch-ins/do-em-ups live in their own store sections (`pitchins`/`doemups`) and resolve by people clicking rather than the nag machinery, but write to the same `completions.jsonl` so one leaderboard totals everything.

## Conventions & gotchas

- **Emoji comparison**: always normalize with `models.emoji_key()` (strips the U+FE0F variation selector) — raw `==` on emoji is unreliable across how Discord echoes them.
- **Intents**: only default (non-privileged) intents — guilds + raw reactions. Do **not** add `message_content` or `members`; the design deliberately avoids needing them.
- **Time**: store/compare in UTC; only convert to the guild tz for wall-clock display/parsing. Use `discord_ts()` for timestamps so each viewer sees their own zone.
- **Restart-safety is a feature, not luck**: it falls out of comparing `now` against persisted instants. Anything new that schedules work should persist its deadline, not hold a timer.
</content>
</invoke>
