"""The slash-command surface, one module per concern.

Wired exactly like ``bot/`` itself: children are imported in dependency order
below purely so their ``@bot.tree.command`` / ``@edit.command`` decorators run
and register against the shared tree, and every child's ``__all__`` is
re-exported flat — so ``bot.<name>`` (and the smoke tests) keep resolving
exactly as they did when this was a single ``commands.py``.

Layout
------
``lookup``  — free-text resolution: the task/game finders and the shared
              autocompletes (`at`/`repeat` live previews, task/game pickers).
``config``  — ``/joblinconfig`` (channel, timezone, reminder role, trinket bar).
``tasks``   — ``schedule_from_rule``, ``/newtask``, ``/deletetask``.
``games``   — ``/pitchin`` and ``/doemup`` (the round engine is ``bot.games``).
``edit``    — the ``/edit`` group (task / pitchin / doemup) and its shared
              engine; registers the group on the tree once all three exist.
"""

from . import lookup
from . import config
from . import tasks
from . import games
from . import edit

__all__: list[str] = []
for _mod in (lookup, config, tasks, games, edit):
    for _name in getattr(_mod, "__all__", ()):
        globals()[_name] = getattr(_mod, _name)
        __all__.append(_name)
