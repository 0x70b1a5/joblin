# 🚜 farmtracker

A small Discord bot for **small-scale farm chore logistics**. Create recurring
or one-off chores; when each is due the bot posts it to your farm channel and
self-reacts with buttons the family taps to complete, snooze, expand, or skip.
Completions are logged so you can run a monthly **leaderboard** — with **points**,
double-value **bounties**, and a **⭐ star** for each month's winner — to gamify
the family chores.

---

## What it does

- **Tasks** are one-off or recurring. Recurrence can be **every *N* days**
  (`1` = daily, `7` = weekly), specific **days of the week** (`mon,thu`,
  `weekdays`, `weekends`), or **days of the month** (`1st,15th`, `last day`).
  Each task has a mandatory **brief** and an optional longer **description**.
- A task can be flagged a **bounty** (`/newtask … bounty:true`): it's worth
  **2 points** instead of 1, and **only someone other than its creator** can
  complete it — handy for a chore you're putting up for the rest of the family.
- You say *when* with two friendly, optional fields:
  - **`at`** — a time or date in plain language: `now` (the default), `in 2h`,
    `tonight`, `18:00`, `6pm`, `tomorrow 8am`, `fri 19:00`, `next monday`,
    `Jun 20 14:00`, `2026-06-20 14:00`. Autocomplete shows the resolved instant
    as you type, so there's no guessing.
  - **`repeat`** — `once` (the default), `daily`, `every 2 days`, `weekly`,
    `weekdays`, `mon,thu`, `monthly on the 1st`, … Autocomplete previews it too.
- When a task is **due**, the bot posts the brief to the configured channel and
  self-reacts:
  | Reaction | Action |
  |---|---|
  | ✅ | **Complete** — logs *who* completed it. Recurring tasks roll to their next slot; one-offs are removed. |
  | ⏩ | **Snooze** — opens a small **number-pad panel** (a separate message): tap a number, with an ⏱️ hours / 📅 days toggle, or ❌ to cancel. |
  | ℹ️ | **Info** — replies with the full description. *(Only shown if the task has one.)* |
  | ❌ | **Skip** — skips just *this* occurrence of a recurring task (it returns next cycle); deletes a one-off. To remove a recurring task entirely, use `/deletetask`. |
  | ↩️ | **Undo** — appears right after a ✅/⏩/❌ and reverses it: a completion is un-logged (so it leaves the leaderboard too), a snooze is rolled back, and a skip/delete is restored. Survives restarts; only the most recent action on an occurrence is undoable, and only until that chore next comes due. |
  | 🔄 | **Requeue** — appears on a ✅-completed post; re-fires that chore **right now** as a fresh occurrence (handy when, say, the water trough is empty again an hour later) without waiting for its next scheduled slot. Finishing the re-run rolls the recurrence on to its normal next slot. Survives restarts; the most recent completed post per task carries the button. |
- If nobody completes or snoozes within the hour, the bot **re-posts hourly**
  until the chore is done (optionally pinging a role).
- Everything survives restarts: due times, pending occurrences, snooze timers,
  open snooze panels, and reaction handling are all driven from the persisted store.

## Commands

| Command | Who | What |
|---|---|---|
| `/farmconfig` | Manage Server | Set the post **channel**, **timezone** (IANA, e.g. `Europe/Berlin`), and an optional **reminder role**. Run with no options to view current config. |
| `/newtask` | anyone | `brief`, optional `at` (default **now**), optional `repeat` (default **once**), optional `description`, optional `bounty` (a 2-point chore the creator can't complete). Both `at` and `repeat` autocomplete with a live preview. Posts a **public** confirmation so the family sees the new chore. |
| `/edittask` | anyone | Change a task's `brief`, `at`, `repeat`, `description` (or `clear_description`), or `bounty`. Pick the task from autocomplete or paste its `id` from `/listtasks`. |
| `/deletetask` | anyone | Permanently delete a task (autocompletes existing tasks). |
| `/listtasks` | anyone | List all tasks with their **`id`**, schedule, and when each next posts. |
| `/leaderboard` | anyone | Monthly **points** per person — one per chore, **two** per bounty — with each past month's winner shown by their **⭐ stars** (`month` defaults to current). |
| `/farmhelp` | anyone | Quick reference for the commands, the `at`/`repeat` syntax, and the reactions. |
| `/redeploy` | bot owner | `git pull`, `uv sync`, then restart the bot in place (same tmux pane, so the log continues). Reports the pull result and aborts without restarting if the pull or sync fails. See [Running & updating on a VPS](#running--updating-on-a-vps). |

### Examples
- Every morning: `/newtask brief:"Put the animals out" at:08:00 repeat:daily`
- Every other day: `/newtask brief:"Refill animal water" at:07:30 repeat:"every 2 days"`
- Twice a week: `/newtask brief:"Take the trash out" at:19:00 repeat:"mon,thu"`
- Monthly: `/newtask brief:"Pay the feed bill" at:09:00 repeat:"monthly on the 1st"`
- Right now (one-off): `/newtask brief:"Move the sheep"` *(at defaults to now, repeat to once)*
- One-off later: `/newtask brief:"Vet visit" at:"tomorrow 14:00" description:"Bring vaccination records"`
- Fix a typo / reschedule: `/edittask task:<id> brief:"Refill the water trough" repeat:"every 3 days"`
- Put up a bounty: `/newtask brief:"Muck out the barn" bounty:true`

### Bounties & stars
- **Bounties** are chores you can't (or won't) do yourself. Create one with
  `/newtask brief:"Muck out the barn" bounty:true` (or toggle it on an existing
  task with `/edittask task:<id> bounty:true`). A bounty is **worth 2 points**
  and its **creator can't tap ✅** — if they try, the bot gently declines and
  leaves it for someone else. Bounty posts are tagged 💰 so everyone sees the prize.
- **Points & stars** drive the `/leaderboard`. Every completed chore is a point;
  bounties are two. At each month's end whoever has the most points wins and earns
  a permanent **⭐ star**, shown next to their name on every future leaderboard
  (a tie shares the star). The current month is still up for grabs, so its star
  isn't awarded until the month closes. Stars are derived from the completion log,
  so an **undo** that voids a completion also updates the standings honestly.

## Setup

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                       # create the venv & install deps
cp .env.example .env          # then paste your bot token into .env
uv run python -m farmtracker  # run the bot
```

## Running & updating on a VPS

On the server, launch the bot through the supervisor script **inside its tmux
window** instead of running it directly:

```bash
tmux new -s farmtracker       # (first time) make the window
./run.sh                      # pull + sync + run, and auto-restart on exit
# Ctrl-B D to detach; the bot keeps running.
```

`run.sh` is a small loop that `git pull`s, `uv sync`s, runs the bot in the
foreground, and — whenever the bot exits — pulls again and restarts it. Because
all of this happens in the *same* pane, the tmux scrollback (your log) stays
**continuous across restarts**.

To deploy a new version, just trigger a restart from **any** shell on the box
(no need to attach to tmux):

```bash
./redeploy.sh                 # stops the bot; run.sh then pulls & restarts it
```

The new logs flow straight on in the bot's tmux window, unbroken. `redeploy.sh`
only signals the bot to stop — the supervisor does the pull + restart, so the
order is always *stop → pull → sync → start*. A crash triggers the same
auto-restart after a few seconds; **Ctrl-C** in the tmux window stops the bot
*and* the loop for a full shutdown.

### Redeploy from Discord

You can also trigger a redeploy without touching the server, with the
owner-only **`/redeploy`** slash command. It runs `git pull` + `uv sync`, posts
the result back to you (ephemerally), and then re-execs the bot **in place** —
same PID, same tmux pane, so the log continues uninterrupted just like
`redeploy.sh`. If the pull or sync fails it reports the error and does *not*
restart. Pass `sync_deps:false` to skip `uv sync` for a code-only change.

Only the **application owner** can run it (Discord checks this via the bot's
app info); add extra user ids with the optional `OWNER_IDS` env var. The command
restarts itself whether or not you're using `run.sh`, but running under `run.sh`
is still recommended so that a crash on the *new* code auto-restarts rather than
leaving the bot down.

The store uses atomic writes and everything (due times, pending occurrences,
snooze panels, undo) is driven from the persisted store, so a signal-stop
mid-tick is safe and the bot resumes cleanly on restart.

> One-time setup on the VPS: these scripts ship in the repo, so `git pull` once
> (or clone fresh), then start the bot with `./run.sh` so future `./redeploy.sh`
> calls have a supervisor to hand the restart to.
>
> Tip: bump tmux's scrollback with `tmux set -g history-limit 50000` if you want
> a longer in-window log. For a permanent file log, start it as
> `./run.sh 2>&1 | tee -a data/bot.log` (note: with a `tee` pipeline, use
> `./redeploy.sh` rather than Ctrl-C to cycle the bot).

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
  models.py   # task schema, natural-language `at` parsing, `repeat` rules,
              #   and DST-aware recurrence (every-N-days / weekday / monthly)
  store.py    # JSON store (asyncio.Lock + atomic writes) + completion log
  bot.py      # commands, scheduler tick, reaction handlers, entry point
```

## Notes & caveats
- Recurring times are interpreted in the configured timezone and are DST-aware
  for every recurrence kind: the wall-clock time is re-pinned each cycle, so an
  `08:00` chore stays at 08:00 across spring-forward/fall-back.
- **Monthly** days beyond a given month's length clamp to the **last day** — so
  `31st` fires on Feb 28/29, Apr 30, etc. (`last day` is shorthand for this).
- The `at` field is resolved by a small dependency-free parser (`now`, `in 2h`,
  `tonight`, `tomorrow 8am`, weekday names, `Jun 20 14:00`, ISO datetimes, …);
  omitting it means **now**. `repeat` defaults to **once**. Slash-command
  autocomplete echoes the resolved instant / rule back so there are no surprises.
- **Snooze** is a separate "number-pad" **panel message** the ⏩ reaction opens,
  keyed in `store["snooze_panels"]` so it keeps working across restarts. Picking
  a number snoozes the occurrence by that many hours (or days, via the 📅 toggle),
  edits the task post with the result, and adds the ↩️ undo. Panels are cleaned
  up when their task is completed, skipped, or deleted.
- A recurring task holds **one** pending occurrence at a time; if it's still
  unresolved when the next cycle would start, the existing nag carries it and the
  schedule rolls forward (no double-posting, no backlog flood) once it's resolved.
- **Undo** works by stashing a snapshot of the task as it was just before each
  ✅/⏩/❌ (in a persisted `undo` table) and self-reacting ↩️ on the result; it's
  guarded so a stale ↩️ can't clobber a newer occurrence, and undoing a ✅ also
  voids that entry in the completion log. Only the latest action per occurrence
  is undoable.
- **Requeue** keeps a 🔄 on the ✅-completed post (in a persisted `requeue` table,
  keyed by that post's id). Tapping it fires a *fresh* occurrence right away —
  for a recurring task by setting `next_due` to now, for a completed one-off by
  rebuilding it from the saved snapshot — then leaves the recurrence to roll on
  to its normal next slot when the re-run is finished (it re-pins to
  `time_of_day`, so the schedule never drifts). If an occurrence is already live
  it declines (finish that one first). Only the most recent completed post per
  task carries the button.
- Editing a task's schedule recomputes its next post immediately — unless a
  reminder is **live right now**, in which case that occurrence is left alone and
  the new schedule takes effect from the next cycle.
- Reaction tidying (removing a clicker's ⏩/ℹ️ tap so it can be pressed again, and
  clearing reactions on completed/undone tasks) needs **Manage Messages**; without
  it the bot still works (it can always remove its own ↩️ and delete its own
  snooze panels), it just leaves other reactions in place.
