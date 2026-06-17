# 🚜 farmtracker

A small Discord bot for **small-scale farm chore logistics**. Create recurring
or one-off chores; when each is due the bot posts it to your farm channel and
self-reacts with buttons the family taps to complete, snooze, expand, or skip.
Completions are logged so you can run a monthly **leaderboard** and gamify the
family chores.

---

## What it does

- **Tasks** are recurring (a wall-clock time, every *N* days — `1` = daily) or
  one-off (a `YYYY-MM-DD HH:MM` deadline). Each has a mandatory **brief** and an
  optional longer **description**.
- When a task is **due**, the bot posts the brief to the configured channel and
  self-reacts:
  | Reaction | Action |
  |---|---|
  | ✅ | **Complete** — logs *who* completed it. Recurring tasks roll to their next slot; one-offs are removed. |
  | ⏩ | **Fast-forward / snooze** — 1h, then 2h, 4h, 8h… (doubles each press). |
  | ℹ️ | **Info** — replies with the full description. *(Only shown if the task has one.)* |
  | ❌ | **Skip** — skips just *this* occurrence of a recurring task (it returns next cycle); deletes a one-off. To remove a recurring task entirely, use `/deletetask`. |
  | ↩️ | **Undo** — appears right after a ✅/⏩/❌ and reverses it: a completion is un-logged (so it leaves the leaderboard too), a snooze is rolled back, and a skip/delete is restored. Survives restarts; only the most recent action on an occurrence is undoable, and only until that chore next comes due. |
- If nobody completes or snoozes within the hour, the bot **re-posts hourly**
  until the chore is done (optionally pinging a role).
- Everything survives restarts: due times, pending occurrences, snooze timers,
  and reaction handling are all driven from the persisted store.

## Commands

| Command | Who | What |
|---|---|---|
| `/farmconfig` | Manage Server | Set the post **channel**, **timezone** (IANA, e.g. `Europe/Berlin`), and an optional **reminder role**. Run with no options to view current config. |
| `/newtask` | anyone | `brief`, `at` (`HH:MM` recurring **or** `YYYY-MM-DD HH:MM` one-off), `every_days` (recurring, default 1), optional `description`. |
| `/deletetask` | anyone | Permanently delete a task (autocompletes existing tasks). |
| `/listtasks` | anyone | List all tasks and when each next posts. |
| `/leaderboard` | anyone | Monthly completion counts per person (`month` defaults to current). |

### Examples
- Every morning: `/newtask brief:"Put the animals out" at:08:00`
- Every other day: `/newtask brief:"Refill animal water" at:07:30 every_days:2`
- One-off: `/newtask brief:"Vet visit" at:2026-06-20 14:00 description:"Bring vaccination records"`

## Setup

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # create the venv & install deps
cp .env.example .env          # then paste your bot token into .env
uv run python -m farmtracker  # run the bot
```

### Discord application
1. **Developer Portal → Applications → New Application → Bot.** Copy the **token**
   into `.env` as `DISCORD_TOKEN`.
2. No privileged intents are required (the bot uses slash commands and reactions,
   not message content).
3. **Invite the bot** with the `bot` and `applications.commands` scopes and these
   permissions: **View Channel, Send Messages, Read Message History, Add
   Reactions, Manage Messages** (Manage Messages lets it tidy reactions on
   resolved tasks; if you configure a reminder role, also add **Mention
   @everyone/roles**). Permissions integer: `207936`.
4. In your server, run `/farmconfig channel:#farm timezone:Europe/Berlin` first.
   - Tip: set `DEV_GUILD_ID` in `.env` to your server id so commands appear
     instantly while you test (global sync can take up to ~1h the first time).

## How it’s built

- **`discord.py`** for the gateway, slash commands, and raw reaction events.
- A **30-second scheduler tick** (`discord.ext.tasks`) fires due tasks and sends
  hourly nags. It compares `now` against each task's persisted `next_due` /
  `remind_at`, so it's naturally restart-safe and never replays a backlog.
- **Storage** is a single JSON document (`data/store.json`) for config + tasks,
  plus an append-only JSONL **completion log** (`data/completions.jsonl`) for
  stats. The bot is a single asyncio process, so concurrency safety is just an
  `asyncio.Lock` around each read-modify-write plus **atomic writes** (temp file
  + `fsync` + `os.replace`) so a crash can't corrupt the store. See the module
  docstring in `farmtracker/store.py`. Swapping to SQLite later is easy if the
  stats grow.

### Layout
```
farmtracker/
  models.py   # task schema, time parsing, DST-aware recurrence math
  store.py    # JSON store (asyncio.Lock + atomic writes) + completion log
  bot.py      # commands, scheduler tick, reaction handlers, entry point
```

## Notes & caveats
- Recurring times are interpreted in the configured timezone and are DST-aware
  (the wall-clock time is re-pinned each cycle).
- A recurring task holds **one** pending occurrence at a time; if it's still
  unresolved when the next cycle would start, the existing nag carries it and the
  schedule rolls forward (no double-posting, no backlog flood) once it's resolved.
- **Undo** works by stashing a snapshot of the task as it was just before each
  ✅/⏩/❌ (in a persisted `undo` table) and self-reacting ↩️ on the result; it's
  guarded so a stale ↩️ can't clobber a newer occurrence, and undoing a ✅ also
  voids that entry in the completion log. Only the latest action per occurrence
  is undoable.
- Reaction tidying (removing a clicker's ⏩/ℹ️ tap so it can be pressed again, and
  clearing reactions on completed/undone tasks) needs **Manage Messages**; without
  it the bot still works (it can always remove its own ↩️), it just leaves other
  reactions in place.
