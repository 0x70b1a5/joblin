"""Persistence: a small JSON document plus an append-only completion log.

Concurrency model
-----------------
The whole bot runs in a single asyncio event loop, so there is no OS-thread
parallelism to guard against. The only hazards are:

  1. Two coroutines interleaving a read-modify-write across an ``await`` point.
  2. A crash midway through writing the file, leaving it truncated/corrupt.

We handle (1) with an ``asyncio.Lock`` held across each transaction, and (2)
by always writing to a temp file, ``fsync``-ing it, and ``os.replace``-ing it
over the real file (an atomic rename on POSIX). The in-memory ``self.data`` is
the source of truth during a run; it is flushed to disk after every change.

Transactions deliberately contain **no** ``await`` of network I/O — callers do
Discord work outside the lock and only re-enter a short transaction to commit
the resulting state. That keeps the lock held for microseconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import tempfile
from typing import Any

EMPTY: dict[str, Any] = {
    "configs": {},
    "tasks": {},
    "messages": {},
    "undo": {},
    "requeue": {},  # completed-post message id -> {task_id, before, guild_id, channel_id}
    "snooze_panels": {},  # panel message id -> {task_id, anchor_id, unit, brief}
    "pitchins": {},  # pitch-in id -> pitch-in dict (see models.py)
    "doemups": {},  # do-em-up id -> do-em-up dict (see models.py)
    "game_messages": {},  # message id -> {"kind": "pitchin"|"doemup", "id": <game id>}
}


class Store:
    def __init__(self, path: pathlib.Path, log_path: pathlib.Path) -> None:
        self.path = path
        self.log_path = log_path
        self._lock = asyncio.Lock()
        self.data: dict[str, Any] = json.loads(json.dumps(EMPTY))

    # -- loading ------------------------------------------------------------
    def load(self) -> None:
        """Read the store from disk (call once at startup, before the loop)."""
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        for key, default in EMPTY.items():
            self.data.setdefault(key, json.loads(json.dumps(default)))

    # -- atomic write -------------------------------------------------------
    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)  # atomic on POSIX
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    # -- transactions -------------------------------------------------------
    @contextlib.asynccontextmanager
    async def txn(self):
        """Mutate ``self.data`` under the lock; flush atomically on clean exit.

        Keep the body free of network ``await``s. On exception the change is
        not flushed (so a failed handler can't half-write the store)."""
        async with self._lock:
            yield self.data
            self._flush()

    async def snapshot(self) -> dict[str, Any]:
        """A deep copy that callers can read freely without holding the lock."""
        async with self._lock:
            return json.loads(json.dumps(self.data))

    # -- completion log (append-only JSONL) ---------------------------------
    async def log_completion(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        async with self._lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())

    async def void_completion(self, completion_id: str) -> bool:
        """Remove a single logged completion by its ``id`` (used when an undo
        reverts a ✅ so the leaderboard stays honest).

        The log is normally append-only, but an undo is a genuine retraction, so
        we rewrite it: read every record, drop the matching one, and replace the
        file atomically (temp + ``fsync`` + ``os.replace``) just like ``_flush``.
        Returns True if a record was actually removed.
        """
        async with self._lock:
            if not self.log_path.exists():
                return False
            kept: list[str] = []
            removed = False
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s:
                    continue
                try:
                    rec = json.loads(s)
                except json.JSONDecodeError:
                    continue  # drop a torn/garbage line while we're rewriting
                if not removed and rec.get("id") == completion_id:
                    removed = True
                    continue
                kept.append(json.dumps(rec, ensure_ascii=False))
            if not removed:
                return False
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.log_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    if kept:
                        f.write("\n".join(kept) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.log_path)  # atomic on POSIX
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
                raise
            return True

    def read_completions(self) -> list[dict[str, Any]]:
        """Read every logged completion (tolerating a torn final line)."""
        out: list[dict[str, Any]] = []
        if not self.log_path.exists():
            return out
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
