"""The Discord bot: slash commands, the scheduler tick, and reactions.

Lifecycle of a task occurrence
------------------------------
1. The scheduler tick (every 30s) notices ``now >= next_due`` and *fires* it:
   posts the brief to the configured channel and self-reacts ✅ ⏩ ℹ️ ⏭️/❌
   (ℹ️ only if the task has a long description; ⏭️ on recurring tasks, ❌ on
   one-offs). The task flips to "pending"
   with ``remind_at = due + 1h``; ``next_due`` is cleared so it can't re-fire.
2. While pending, every tick checks ``remind_at``. When it passes, the bot
   posts a fresh nag (optionally pinging a role) and sets ``remind_at = now+1h``.
   Nags additionally self-react 🤫; a task whose ``no_nag`` flag is set is
   never nagged (it still fires — only the reminders stop).
3. Reactions resolve or defer the occurrence:
     ✅  complete  -> log the completer; recurring tasks roll to the next slot,
                      one-offs are removed.
     ⏩  fast-fwd  -> snooze 1h, then 2h, 4h, 8h ... (doubling each press).
     ℹ️  info      -> reply with the long description.
     ⏭️  skip      -> recurring only: skip just this occurrence.
     ❌  delete    -> one-off only: delete the task.
                      (Deleting an entire recurring task is /deletetask.)
     ↩️  undo      -> reverse the most recent ✅/⏩/⏭️/❌ on that occurrence. The
                      bot adds this button right after one of those actions.
     🔄  requeue   -> appears on a ✅-completed post; re-fires the chore right
                      now (a fresh occurrence) without waiting for its next slot.
     🤫  shush     -> sets the task's lifetime ``no_nag`` flag: stop the hourly
                      reminders while occurrences keep firing on schedule. A
                      shushed chore's posts self-react 🔊 instead.
     🔊  un-shush  -> clears ``no_nag``: the hourly reminders resume (with a
                      fresh cadence).

Everything is keyed off ``store["messages"][message_id] -> task_id`` so that
reactions keep working across restarts, and the persisted ``remind_at`` means
nags survive restarts too.

Undo
----
Each of the three mutating actions stashes a deep copy of the task *as it was
just before the action* into ``store["undo"][anchor_message_id]`` (plus the
completion-log id for ✅) and self-reacts ↩️ on the message showing the result.
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
from . import backup
from . import scheduler
from . import reactions
from . import commands
from . import listing
from . import admin

# Re-export every submodule's public surface (incl. the ``main`` entry point
# used by __main__.py) so `import joblin.bot as bot; bot.<name>` and the
# smoke tests keep resolving exactly as before the split.
_SUBMODULES = (core, helpers, claps, games, scoring, backup, scheduler,
               reactions, commands, listing, admin)
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
