"""The Discord bot: slash commands, the scheduler tick, and the button/reaction UI.

Lifecycle of a task occurrence
------------------------------
1. The scheduler tick (every 30s) notices ``now >= next_due`` and *fires* it:
   posts the brief to the configured channel with an action row of buttons —
   ✅ Done, ⏩ Snooze, ℹ️ Info (only if the task has a long description),
   ⏭️ Skip on recurring tasks / ❌ Cancel on one-offs, and 🎁 Award. The task flips to
   "pending" with ``remind_at = due + 1h``; ``next_due`` is cleared so it
   can't re-fire.
2. While pending, every tick checks ``remind_at``. When it passes, the bot
   posts a fresh nag (optionally pinging a role) and sets ``remind_at = now+1h``.
   Nags additionally carry a 🤫 Shush button; a task whose ``no_nag`` flag is
   set is never nagged (it still fires — only the reminders stop). Each fire
   and nag then calls ``games.bump_live_games``: every open pitch-in /
   do-em-up round is re-posted beneath the chore post (old post swept) so
   games stay in view without noise of their own; 🤫 on a game sets its own
   ``no_nag`` to keep it put.
3. Buttons resolve or defer the occurrence:
     ✅  done      -> log the completer; recurring tasks roll to the next slot,
                      one-offs are removed.
     🎁  award     -> ephemeral picker (family chips + User Select) that
                      completes as someone else; the presser is ``awarded_by``.
                      On a described nag, 🎁 wears 🤫🎁 / 🔊🎁 and the panel
                      carries 🤫/🔊 too.
     ⏩  snooze    -> opens an ephemeral numpad (hours/days); 1h, 2h, 4h ...
     ℹ️  info      -> whisper the long description (ephemeral).
     ⏭️  skip      -> recurring only: skip just this occurrence.
     ❌  cancel    -> one-off only: delete the task.
                      (Deleting an entire recurring task is /deletetask.)
     ↩️  undo      -> reverse the most recent ✅/⏩/⏭️/❌ on that occurrence. The
                      bot puts this button on the message showing the result.
     🔄  requeue   -> rides a ✅-completed post; re-fires the chore right now
                      (a fresh occurrence) without waiting for its next slot.
                      Closed pitch-in / do-em-up rounds carry the same button —
                      it opens a fresh round on the spot (games.py).
     🤫  shush     -> sets the task's lifetime ``no_nag`` flag: stop the hourly
                      reminders while occurrences keep firing on schedule. A
                      shushed chore's posts carry 🔊 instead.
     🔊  un-shush  -> clears ``no_nag``: the hourly reminders resume (with a
                      fresh cadence).

Puntobombs (bombs.py) ride this same lifecycle with a fuse on top: a strictly
one-off task carrying ``explodes_at``. ✅ before the fuse runs out is the
defusal (1 punto, kind "puntobomb"); past it the scheduler blows the bomb and
everyone in the guild's completion log is docked puntos (kind "kaboom").
❌ / ``/deletetask`` stays available — The Coward's Way Out — and moves nothing.

Buttons are ``DynamicItem``s whose custom_id carries the task id, so a single
``add_dynamic_items`` call on startup revives every post's buttons after a
restart. Live posts are additionally keyed in
``store["messages"][message_id] -> task_id`` (the stale-button guard), and the
persisted ``remind_at`` means nags survive restarts too.

Posts made before the button migration self-reacted the same emoji instead;
``on_raw_reaction_add`` still routes those (and any manually added emoji on a
live post) through the very same per-action handlers, via the ``Press``
abstraction in helpers.py. An occurrence's ``pending["ui"]`` says which era it
fired in, so resolution knows whether an emoji sweep is still needed.

Declutter & the 📜 Daily Log (daily_log.py)
-------------------------------------------
Superseded posts are swept away instead of piling up: each nag deletes the
posts it replaces, resolution deletes everything but the anchor, and a resolved
post whose last ↩️/🔄/👏 retires is deleted too — *except* any post a member
has reacted to or replied to (the ``touched`` table, fed by decoration
reactions and ``on_message`` reply references; no content intent needed).
The day's record lives in the 📜 Daily Log instead: one embed per guild-day,
re-derived in full from completions.jsonl on every change (so ↩️/👏 correct
it), chronological, rolling with scoring's nightly ~23:59 frame. Sweeps are
per-guild switchable (``/joblinconfig declutter:False``); the log always runs.

Undo
----
Each of the three mutating actions stashes a deep copy of the task *as it was
just before the action* into ``store["undo"][anchor_message_id]`` (plus the
completion-log id for ✅) and lands ↩️ on the message showing the result.
Undo simply restores that snapshot — after first checking the occurrence hasn't
moved on (``can_undo``), so we never clobber a newer occurrence — and voids the
logged completion when reverting a ✅. Like the rest of the store it survives
restarts, so the ↩️ button keeps working after a reboot.
"""

from __future__ import annotations

import sys as _sys
import types as _types

from . import core
# Import submodules in dependency order so every @bot.tree.command / @bot.event
# decorator runs and registers against the shared bot instance.
from . import helpers
from . import claps
from . import games
from . import scoring
from . import daily_log
from . import backup
from . import month_close
from . import scheduler
from . import reactions
from . import bombs
from . import commands
from . import listing
from . import admin

# Re-export every submodule's public surface (incl. the ``main`` entry point
# used by __main__.py) so `import joblin.bot as bot; bot.<name>` and the
# smoke tests keep resolving exactly as before the split.
_SUBMODULES = (core, helpers, claps, games, scoring, daily_log, backup,
               month_close, scheduler, reactions, bombs, commands, listing,
               admin)
for _mod in _SUBMODULES:
    for _name in getattr(_mod, "__all__", ()):
        globals()[_name] = getattr(_mod, _name)


class _BotPackage(_types.ModuleType):
    """`store` is swapped wholesale by the test-suite via ``bot.store = ...``
    and every handler reads its module global at call time. Forward the
    reassignment to all submodules so the swap is observed everywhere."""

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == "store":
            _prefix = __name__ + "."
            for _mn, _m in list(_sys.modules.items()):
                if _mn.startswith(_prefix) and hasattr(_m, "store"):
                    _m.store = value


_sys.modules[__name__].__class__ = _BotPackage
