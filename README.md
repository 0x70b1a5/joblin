# 🚜 farmtracker

A small Discord bot for **small-scale farm chore logistics**. Create recurring
or one-off chores; when each is due the bot posts it to your farm channel and
self-reacts with buttons the family taps to complete, snooze, expand, or skip.
Completions are logged so you can run a monthly **leaderboard** — with **points**,
double-value **bounties**, a **⭐ star** for each month's winner, and a rolled
**🖼️ trinket** for everyone who clears the month's points bar — to gamify
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
  | 👏 | **Clap** — appears on a finished chore, **pitch-in, or do-em-up** post; anyone who *didn't* take part can tap it to tip **every** doer a **+1 bonus point** (one clap per outsider, so a crowd can stack several). The bonus lands on the leaderboard like any other point; undoing a chore's ✅ retracts its claps. Survives restarts; the most recent finished post per task/game carries the button. |
- If nobody completes or snoozes within the hour, the bot **re-posts hourly**
  until the chore is done (optionally pinging a role).
- Everything survives restarts: due times, pending occurrences, snooze timers,
  open snooze panels, and reaction handling are all driven from the persisted store.

## Commands

| Command | Who | What |
|---|---|---|
| `/farmconfig` | Manage Server | Set the post **channel**, **timezone** (IANA, e.g. `Europe/Berlin`), an optional **reminder role**, and the **`item_bar`** — points per trinket each month, where every whole multiple earns another (default **25**). Run with no options to view current config. |
| `/newtask` | anyone | `brief`, optional `at` (default **now**), optional `repeat` (default **once**), optional `description`, optional `bounty` (a 2-point chore the creator can't complete). Both `at` and `repeat` autocomplete with a live preview. Posts a **public** confirmation so the family sees the new chore. |
| `/pitchin` | anyone | Post a **pitch-in**: `brief`, optional `expires` (default **24h**), `points` each (default 1), `max_scorers`, `description`. Everyone who taps ✅ before it closes earns a point. See [Pitch-ins & do-em-ups](#pitch-ins--do-em-ups). |
| `/doemup` | anyone | Post a **do-em-up**: `brief`, optional `points` per ➕ (default 1), `deadline`, `point_limit`, `description`. Tap ➕ once per thing you did; the tally updates live. See [Pitch-ins & do-em-ups](#pitch-ins--do-em-ups). |
| `/edit` | anyone | One command with a subcommand per type — **`/edit task`**, **`/edit pitchin`**, **`/edit doemup`** — each showing only its own fields (so `bounty` appears only on tasks, `max_scorers` only on pitch-ins, etc.). Change the `brief`, `at`/slot, `repeat`, `description`, points, cap, or close time; pick the item from autocomplete or paste its `id` from `/listtasks`. A schedule change to a **live** round applies from the next round. |
| `/deletetask` | anyone | Permanently delete a task, pitch-in, or do-em-up (autocompletes all three; deleting a recurring game stops the whole series). |
| `/listtasks` | anyone | List all tasks **plus pitch-ins (🤝) and do-em-ups (💪)** with their **`id`**, schedule, and state (next post / 🟢 open / next round) — **paged** with ◀/▶ buttons so every id stays reachable, and showing 🔔×_n_, the lifetime number of times each chore has had to be nagged. |
| `/listopen` | anyone | Post a public checklist of everything **open right now** — pending chores plus live pitch-ins / do-em-ups — each an **inline jump link** to the original post where it's done (never a nag), grouped and ordered by when it's due. Cuts the scrollback when lots are doable any time of day. |
| `/leaderboard` | anyone | Monthly **points** per person — one per chore, **two** per bounty, plus pitch-in / do-em-up points — with each past month's winner shown by their **⭐ stars**, and the month's bountiful **zone** (`month` defaults to current). |
| `/vitrine` | anyone | Gaze upon a collection of **trinkets** — the inert *objets d'art* earned at each month's end, one per whole multiple of the bar cleared, grouped by month. `user` defaults to yourself. |
| `/farmhelp` | anyone | Quick reference for the commands, the `at`/`repeat` syntax, and the reactions. |
| `/redeploy` | bot owner | `git pull`, `uv sync`, then restart the bot in place (same tmux pane, so the log continues). Reports the pull result and aborts without restarting if the pull or sync fails. See [Running & updating on a VPS](#running--updating-on-a-vps). |

### Examples
- Every morning: `/newtask brief:"Put the animals out" at:08:00 repeat:daily`
- Every other day: `/newtask brief:"Refill animal water" at:07:30 repeat:"every 2 days"`
- Twice a week: `/newtask brief:"Take the trash out" at:19:00 repeat:"mon,thu"`
- Monthly: `/newtask brief:"Pay the feed bill" at:09:00 repeat:"monthly on the 1st"`
- Right now (one-off): `/newtask brief:"Move the sheep"` *(at defaults to now, repeat to once)*
- One-off later: `/newtask brief:"Vet visit" at:"tomorrow 14:00" description:"Bring vaccination records"`
- Fix a typo / reschedule: `/edit task task:<id> brief:"Refill the water trough" repeat:"every 3 days"`
- Open a recurring game later in the day: `/edit doemup event:<id> at:22:30` *(moves the daily slot; any live round finishes on its old time first)*
- Put up a bounty: `/newtask brief:"Muck out the barn" bounty:true`

### Bounties, stars & trinkets
- **Bounties** are chores you can't (or won't) do yourself. Create one with
  `/newtask brief:"Muck out the barn" bounty:true` (or toggle it on an existing
  task with `/edit task task:<id> bounty:true`). A bounty is **worth 2 points**
  and its **creator can't tap ✅** — if they try, the bot gently declines and
  leaves it for someone else. Bounty posts are tagged 💰 so everyone sees the prize.
- **Points & stars** drive the `/leaderboard`. Every completed chore is a point;
  bounties are two. At each month's end whoever has the most points wins and earns
  a permanent **⭐ star**, shown next to their name on every future leaderboard
  (a tie shares the star). The current month is still up for grabs, so its star
  isn't awarded until the month closes. Stars are derived from the completion log,
  so an **undo** that voids a completion also updates the standings honestly.
- **Trinkets 🖼️** are a *parallel* reward to the star: at each month's close, a
  worker earns one **inert** *objet d'art* into their `/vitrine` for **every whole
  multiple** of the **bar** (`/farmconfig item_bar:`, default **25**) their monthly
  points reached — 50 points on a 25 bar earns two. They cost no points and do
  nothing — so the chore economy stays sealed and no points are ever created from
  nothing. Each month a different **zone** is *in season*
  (the Bean Zone, the Vaults, the Menagerie, the Scriptorium…), chosen
  deterministically from the year-month and announced on the `/leaderboard`. The
  in-season zone is a **bonus, not a monopoly**: each trinket independently lands
  on it ~70% of the time (`FEATURED_WEIGHT`) and otherwise strays in from one of
  the other zones, so a month's collection is mostly — but not only — the featured
  taxon (`/vitrine` leads each month with the season's emoji, and every item shows
  its own). Like stars, trinkets are **derived from the completion log** — the zone
  is drawn from a `sha256("zone-pick", guild, user, month, idx)` seed and the item
  from a `sha256("trinket", guild, user, month, zone, idx)` seed, so the same
  collection comes back every time, with no stored award state and nothing to
  reconcile after an undo. The tables are blended from *Vaults of Vaarn* and *Flayed Sun*;
  see `farmtracker/trinkets.py`.

## Pitch-ins & do-em-ups

Two lightweight, **post-now** task types for ad-hoc bursts of work — unlike chores
they don't schedule or recur; you fire one off and the family piles on. Both award
points to the same monthly **`/leaderboard`** as chores (a chore completion is
worth 1 point).

- **`/pitchin`** — a shared call to action (a "laundry bonanza"). The bot posts it
  and self-reacts ✅; **everyone who taps ✅ before it closes earns a point** — so a
  bonanza with three pitcher-inners is +1 to all three. It closes at its `expires`
  time (**default 24h**) or when the creator taps 🏁. Options: `points` (worth more
  than one each) and `max_scorers` (only the first *N* score, and it closes the
  moment it fills). Pull your ✅ back off before it closes and you drop out.
  - `/pitchin brief:"Laundry bonanza" expires:tonight`
  - `/pitchin brief:"Stack the firewood" points:2 max_scorers:4`

- **`/doemup`** — one point **per thing done** ("1 pt per thistle bush removed").
  The post carries **➕ / ➖** buttons: tap ➕ once for each one you do (➖ to fix a
  miscount) and the message **edits itself** to show a live per-person tally and
  running total. It stays open until an optional `deadline`, an optional
  `point_limit` (auto-closes once that many points are tallied), or the creator
  taps **🏁 End**. Option: `points` (per ➕).
  - `/doemup brief:"Thistle bush removed"`
  - `/doemup brief:"Bale stacked" deadline:"tomorrow 18:00" point_limit:200`

When a pitch-in or do-em-up closes, its post is rewritten in place as a one-line
result (e.g. *"🤝 Laundry bonanza — pitched in! +1 each to Ann, Bo & Cy"*) and its
points are written to the leaderboard. Both **survive restarts** like everything
else: the live state lives in the store, the do-em-up buttons are re-registered on
startup, and each close is driven by the same 30-second tick as chore reminders —
so a close that fell due while the bot was down simply fires on the next tick.

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

- **`discord.py`** for the gateway, slash commands, raw reaction events, and
  persistent message buttons (the do-em-up ➕/➖/🏁, revived after a restart with
  one `add_dynamic_items` call — the do-em-up id rides in each button's custom_id).
- A **30-second scheduler tick** (`discord.ext.tasks`) fires due tasks, sends
  hourly nags, and closes expired pitch-ins / past-deadline do-em-ups. It compares
  `now` against each task's persisted `next_due` / `remind_at` (and each game's
  `expires_at` / `deadline`), so it's naturally restart-safe and never replays a
  backlog.
- **Storage** is a single JSON document (`data/store.json`) for config, tasks, and
  live pitch-ins / do-em-ups, plus an append-only JSONL **completion log**
  (`data/completions.jsonl`) for stats — chore completions and pitch-in / do-em-up
  points both land there (the latter carry a `points` count), so one query totals
  the leaderboard. The bot is a single asyncio process, so concurrency safety is just an
  `asyncio.Lock` around each read-modify-write plus **atomic writes** (temp file
  + `fsync` + `os.replace`) so a crash can't corrupt the store. See the module
  docstring in `farmtracker/store.py`. Swapping to SQLite later is easy if the
  stats grow.

### Layout
```
farmtracker/
  models.py   # task schema, natural-language `at` parsing, `repeat` rules,
              #   DST-aware recurrence, and pitch-in / do-em-up render + tally
  store.py    # JSON store (asyncio.Lock + atomic writes) + completion log
  bot/        # the Discord bot, split by concern (re-exports a flat surface):
    core.py        # FarmBot instance, the Store, shared constants
    helpers.py     # small formatting + occurrence I/O helpers
    scheduler.py   # the 30s tick: fire tasks, post nags
    reactions.py   # reaction events: done/skip/ffwd/snooze, undo, requeue
    claps.py       # 👏 bonus-point claps on finished work
    games.py       # pitch-ins & do-em-ups (ad-hoc point events)
    commands.py    # the slash commands + autocompletes
    listing.py     # /listtasks, /listopen, /farmhelp + paginator
    scoring.py     # /leaderboard, ⭐ stars, /vitrine
    admin.py       # /redeploy, error handler, entry point
  trinkets.py # the end-of-month objet-d'art generator + the vitrine (derived)
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
- **Claps** keep a 👏 on the finished post (in a persisted `claps` table, keyed by
  that post's id, recording the participants and which outsiders have already
  clapped). It rides on a ✅-completed chore as well as a closed **pitch-in** or
  **do-em-up** round — for a game the participants are its scorers/talliers, so one
  clap tips *all* of them. A tap from anyone who isn't a participant appends a
  `clap` row (worth 1 point) to the completion log for each participant — capped at
  one clap per outsider, and ignored entirely from a participant. Undoing a chore's
  ✅ voids its bonus rows along with the completion (games have no undo; `/deletetask`
  retires the button without touching already-awarded points). Only the most recent
  finished post per task/game carries the button.
- Editing a task's schedule recomputes its next post immediately — unless a
  reminder is **live right now**, in which case that occurrence is left alone and
  the new schedule takes effect from the next cycle.
- Reaction tidying (removing a clicker's ⏩/ℹ️ tap so it can be pressed again, and
  taking the bot's buttons off completed/undone tasks) needs **Manage Messages**;
  without it the bot still works (it can always remove its own ↩️ and delete its
  own snooze panels), it just leaves other reactions in place.
- When an occurrence or pitch-in **closes**, only the bot's own functional buttons
  (✅ ⏩ ℹ️ ❌ 🏁) are taken down — any reaction a family member piled on for fun (a
  😄, a 🎉) is deliberately left in place rather than swept away.
