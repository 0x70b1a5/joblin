# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-guild-friendly Discord bot for small-farm/household chore logistics: one-off and recurring tasks that post to a channel when due, get resolved by their posts' buttons (emoji reactions on pre-button posts still work), and feed a family puntos economy (leaderboard ⭐ stars, end-of-month 🖼️ trinkets, plus ad-hoc punto events). Pure stdlib + `discord.py` (+ its `aiohttp`); persistence is a JSON file plus an append-only log. No database. An optional phone-first web UI (`joblin/web/`, Discord-OAuth-gated) serves the schedule from *inside* the bot process when `WEB_BASE_URL`/`DISCORD_CLIENT_ID`/`DISCORD_CLIENT_SECRET` are set — otherwise no port is ever opened.

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

**Persistence (`store.py`).** Two files: `store.json` (a single dict: configs, tasks, live games, and reaction-routing tables) and `completions.jsonl` (append-only ledger of every punto-earning event, plus zero-punto 🔄-requeue marker rows — the source of truth for all stats and title badges). The in-memory `store.data` is canonical during a run; every change is flushed atomically (temp file → `fsync` → `os.replace`). Two access patterns, and using the right one matters:
- `async with store.txn() as data:` — mutate under the lock, flush on clean exit. **Keep network/Discord `await`s OUT of the txn body.** The pattern across the codebase is: snapshot → do Discord I/O → re-enter a tiny txn to commit the result.
- `await store.snapshot()` — a deep copy you can read freely without holding the lock.

**Scheduling math (`models.py`).** The home of all the tricky parts: parsing free-form `at`/`repeat` strings and turning a recurrence rule into a concrete next-fire instant, **DST-aware**. Tasks are plain dicts (JSON round-trips with zero friction); the **task dict schema and the `pending` sub-schema are documented in the module docstring** — read it before touching task fields. Times are stored as ISO-8601 **UTC**; wall-clock interpretation always happens in the guild's timezone. `recurrence_of()` reads legacy tasks (pre-`freq`) as the equivalent rule, so nothing on disk needs migrating. `first_due`/`compute_first_due` give a brand-new task created "now" a one-minute grace so it fires immediately instead of next cycle; `next_due`/`roll_forward` keep strict semantics so occurrences never double-fire or replay a backlog.

**The bot package (`joblin/bot/`).** Submodules are imported in dependency order by `bot/__init__.py` purely so their `@bot.tree.command`/`@bot.event` decorators register against the one shared `bot` instance in `core.py`. Each submodule exposes `__all__`, which `__init__.py` re-exports flat (so `bot.<name>` and the tests resolve). Tests swap the store via `bot.store = ...`; `__init__.py` forwards that assignment to every submodule, so handlers always read the live store.

**Occurrence lifecycle (the heart of the system).** The scheduler (`@tasks.loop(seconds=30)`) compares `now` against persisted `next_due`/`remind_at`/game deadlines, which makes the whole thing **naturally restart-safe** — no in-memory timers to lose.
1. `now >= next_due` → **fire**: post the brief with its button row (✅ Done, ⏩ Snooze, ℹ️ Info, ⏭️ Skip/❌ Cancel). Task flips to `pending` (`remind_at = due + 1h`, `ui: "buttons"`); `next_due` cleared so it can't re-fire.
2. While pending, each tick checks `remind_at` → posts a fresh **nag** (optionally pinging a role, carrying an extra 🤫 Shush button), resets `remind_at = now + 1h`, bumps `nag_count`. A task with `no_nag` set is never nagged (it still fires — only the reminders stop).
3. **Buttons** resolve/defer it: ✅ complete (logs it; recurring rolls to next slot, one-off is deleted; the status edit lands ↩️ 🔄 👏 in the same call), ⏩ snooze (an *ephemeral* button numpad — deliberately unpersisted, unlike the legacy public panel), ℹ️ info (ephemeral), ⏭️/❌ skip, ↩️ undo, 🔄 requeue, 👏 clap (its label counts ×n), 🤫 shush / 🔊 un-shush (set/clear the lifetime `no_nag` flag; a shushed task's posts carry 🔊 instead).

**The button/reaction split.** All buttons are `DynamicItem`s with ids like `task:done:<tid>` / `post:undo:<tid>` / `pitchin:join:<pid>`, revived after restarts by one `add_dynamic_items` call in `core.setup_hook` — no per-message bookkeeping. Posts made **before** the migration self-reacted emoji instead; `on_raw_reaction_add` still routes those (and manually-added emoji) into the *same* per-action handlers via the `Press` abstraction in `helpers.py` (normalizes reaction vs. button: `retract`/`whisper`/`ack`/`edit_pressed`). `pending["ui"]`, and the `ui` field on undo/requeue/claps/pitchin records, say which era something was posted in, so resolution knows whether a legacy emoji sweep is still needed — never add new self-reactions.

Live posts still key off `store["messages"][message_id] → task_id` — for buttons it's the stale-press guard (a button proves the task id, not that the post is current), and it keeps legacy reactions working across restarts. The resolved-post row (↩️/🔄/👏) is *derived* from the `undo`/`requeue`/`claps` tables by `post_view_for`, so retiring a record and re-editing the view can never disagree. **Undo** stashes a deep copy of the task *before* each mutating action in `store["undo"]`; it restores that snapshot (after `can_undo` confirms the occurrence hasn't moved on) and voids the matching completion-log entry.

**The puntos economy is sacred — puntos are never created from nothing or spent.** The one sanctioned negative is a blown 💣 puntobomb's `kind: "kaboom"` penalty rows (−5 per player; `bot/bombs.py`) — `scoring._completion_points` keeps their sign and floors every *other* kind at +1. Each chore = 1 punto (bounties = 2, a puntobomb defusal = 1, `kind: "puntobomb"`; a 🧾 list writes one 1-punto `kind: "list"` row *per distinct ticker* — the deliberate multi-payout, like a pitch-in — and ↩️ voids them all together). ⭐ stars and 🖼️ trinkets are **derived, never stored**: stars are recomputed from the completion log on each leaderboard draw (so undos correct the standings); trinkets are a *deterministic* `sha256(guild, user, year-month, idx)` roll, so the same trinket comes back on every view/restart/machine with no persisted award state. The trinket **bar** is a value-with-history (`bar_history` in the guild config, appended by `/joblinconfig`): each month is judged by the bar in force at its guild-local close (`scoring.bar_for`), so re-barring never rewrites a closed month, while the open month floats with the latest change. When changing scoring, preserve this — see the existing memory notes on the claps/trinkets exceptions.

## Module legend

| File | Responsibility |
|---|---|
| `models.py` | Task dict schema, emoji constants, free-form time parsing, DST-aware recurrence math. The only place "tricky" lives. |
| `store.py` | `Store`: the JSON doc + append-only JSONL log, `txn()`/`snapshot()`, atomic writes, completion logging/voiding. |
| `trinkets.py` | Deterministic end-of-month trinket generator + vitrine (SHA256-seeded; never builtin `hash()`). |
| `bot/core.py` | Shared singletons: the `JoblinBot`, the `store`, constants, timezone list. Dependency-free to avoid import cycles. |
| `bot/__init__.py` | Wires submodules together (decorator registration), flat re-export, store hot-swap for tests. Top docstring = the occurrence/undo lifecycle. |
| `bot/scheduler.py` | The 30s tick: fire due tasks, blow spent puntobombs, send nags, sweep games, run nightly backups. |
| `bot/bombs.py` | Puntobombs: `explode_puntobomb` (the kaboom — pops the task, docks everyone in `scoring.puntobomb_casualties`, rewrites the post as the blast) and the Coward's-Way-Out post strike shared by `/deletetask` and the web delete. The defusal itself is just `reactions._handle_done` (kind `"puntobomb"`). |
| `bot/backup.py` | Nightly (~23:59 guild-local) self-backup: if the completion log changed since the last run, zip `store.json` + `completions.jsonl` and post it to the channel as an attachment, then auto-post the leaderboard. Restart-safe via a persisted `next_backup_at`. |
| `bot/reactions.py` | The action handlers (done/snooze/info/skip/shush/undo/requeue) plus both entry ways into them: `handle_task_button`/`handle_post_button` (buttons) and the raw-reaction dispatcher (legacy posts + manual emoji). Also the ephemeral `SnoozeView` numpad. |
| `bot/commands/` | The slash-command surface, a subpackage wired like `bot/` itself (children imported for decorator registration, `__all__` re-exported flat): `lookup` (free-text task/game finders + shared autocompletes), `config` (`/joblinconfig`), `tasks` (`schedule_from_rule`, `/newtask`, `/puntobomb`, `/deletetask`), `games` (`/pitchin`, `/doemup` — the round engine stays in `bot/games.py`), `edit` (the `/edit` group + shared engine). |
| `bot/games.py` | Pitch-ins & do-em-ups (ad-hoc punto events): posting, button views (`DoEmUpButton`), closing on expiry/cap/deadline/manual end. |
| `bot/claps.py` | 👏 bonus-punto tips from non-participants on completed posts/closed games. |
| `bot/scoring.py` | `/leaderboard` (monthly puntos + ⭐ stars) and `/vitrine`; star/score aggregation; the ⬆️/⬇️/🔥×N rank spice, replayed from log timestamps against a day frame that rolls at the nightly 23:59 post. |
| `bot/listing.py` | `/listtasks` (paginated), `/listopen`, `/joblinhelp`. |
| `bot/admin.py` | `main()` entry point, owner-only `/redeploy`, global app-command error handler, `on_ready`. |
| `bot/helpers.py` | Small formatting/occurrence-I/O helpers (schedule labels, post rendering, safe delete) plus the button layer: `Press`, the `TaskButton`/`PostButton` DynamicItems, view builders (`make_task_view`, `completed_view`, `post_view_for`). |
| `web/` | The optional bundled web UI: `__init__.py` (aiohttp server on the bot's loop — Discord OAuth + signed-cookie sessions, JSON API mirroring `/newtask`, `/edit task`, `/deletetask`, `/edit pitchin|doemup` (via the shared `apply_game_edit` engine in `bot/commands/edit.py`) and the game branch of `/deletetask`; started by `core.setup_hook`, always reads `core.store` so the tests' store swap holds) and `index.html` (the whole frontend: one vanilla-JS mobile-first page, no build step). View + task/game CRUD — completing chores/earning puntos stays Discord-only. |

## Domain concepts (vocabulary you'll meet)

- **Task / chore** — one-off or recurring; recurrence is "every N days", specific weekdays, or specific month-days (31 clamps to the real last day), each with an optional `time_of_day`.
- **Bounty** — a 2-punto chore the creator is barred from completing (so someone *else* does it).
- **List (🧾)** — a chore with 2–20 `items`, each its own button on the post (`task:item:<idx>:<tid>`; controls keep the bottom row). Tick yours as you go — only you can untick it — and the tick that turns the last box green (or ✅, which sweeps the unticked remainder as the presser's) completes it through the ordinary done path: every distinct ticker earns 1 punto. Ticks live in `pending["ticks"]` (`{idx: {user_id, user_name}}`), so a recurring list resets each cycle and undo restores them. Never a bounty or a puntobomb; editing items under a live occurrence resets ticks and redraws the post.
- **Puntobomb** — a strictly one-off, non-bounty chore with a **required fuse** (`explodes_at`, min 1h from arming). First ✅ defuses it for 1 punto; past the fuse the scheduler blows it and *everyone in the game* — every user in the guild's completion log — is docked 5 puntos (`kind: "kaboom"`). Deleting it instead (❌ on the post, `/deletetask`, web) is always allowed and moves nothing: **The Coward's Way Out**. No 🔄 on a defused bomb, and ↩️ refuses once the fuse is spent (either would re-arm it into an instant kaboom).
- **Pitch-in** — a shared call to action posted immediately; everyone who taps ✅ before it closes (expiry / max scorers / 🏁 manual end) earns its punto value.
- **Do-em-up** — a live unit tally posted immediately with ➕/➖ buttons; scorers earn per unit, closes on deadline / punto limit / manual end. New ones require a **verb**; it heads the display title everywhere ("«verb»-'em-up: «brief»", `models.doemup_title`) while the stored `brief` and every log row stay bare. Pre-verb rows have `verb: None` and read with the generic "do" swapped in.
- **Clap (👏)** — an outsider tap on a finished post that tips each doer +1 (once per outsider per post).
- Pitch-ins/do-em-ups live in their own store sections (`pitchins`/`doemups`) and resolve by people clicking rather than the nag machinery, but write to the same `completions.jsonl` so one leaderboard totals everything. A closed round's post keeps a 🔄 (beside any 👏) that opens a fresh round on the spot — same `requeue` table as chores, records tagged `kind`, handled in `games.py`; a recurring game just plays its next round early, a one-off is rebuilt with its original window.

## Conventions & gotchas

- **Emoji comparison**: always normalize with `models.emoji_key()` (strips the U+FE0F variation selector) — raw `==` on emoji is unreliable across how Discord echoes them. (Only the legacy reaction path compares emoji; buttons dispatch on their custom_id.)
- **Buttons must answer within 3s — ack first**: every button entry point (`handle_task_button`, `handle_list_button`, `handle_post_button`, `handle_clap_button`, the game handlers, the snooze numpad's digits) calls `press.ack()` (defer) as its *first act*, before any store or Discord work; visible replies then go out as ephemeral followups (`press.whisper`) or plain message edits (`press.edit_pressed`), and never respond twice. A failed interaction response is logged (`log.warning`), never swallowed — a 10062 in the log means the press reached us after its 3s token died (gateway lag or a resume replay). Only the numpad's unit-toggle/cancel still ride the free interaction response (nothing slow in front of them). Every guard that refuses a press must say so (whisper), not silently return.
- **Intents**: only default (non-privileged) intents — guilds + raw reactions. Do **not** add `message_content` or `members`; the design deliberately avoids needing them.
- **Time**: store/compare in UTC; only convert to the guild tz for wall-clock display/parsing. Use `discord_ts()` for timestamps so each viewer sees their own zone.
- **Restart-safety is a feature, not luck**: it falls out of comparing `now` against persisted instants. Anything new that schedules work should persist its deadline, not hold a timer.
</content>
</invoke>
