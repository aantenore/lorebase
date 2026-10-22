"""Durable memory of the structured output mode each chat endpoint needs, in a JSON file.

`lorebase_inference.chat.ModeMemory` for Guide, which has no database: one small file of
`{"<hex digest of endpoint, model and output settings>": {"index": mode index, "at": epoch
seconds}}`, so no endpoint address is stored in the clear. A mode only moves towards the weaker
ones; an entry older than the caller's maximum age counts as absent and is dropped by the next
write (entries of an older format, a bare number, are ignored). Writers take an exclusive
`flock` on a lock file next to the data file around read-modify-write, so several Guide
processes do not lose each other's entries (without `fcntl` the lock is a no-op); the data is
written to a temporary file in the same directory, flushed and renamed over the old one, so a
crash never leaves a partial file. Every I/O or format problem is logged and ignored: the client
then keeps the mode it learned in the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # no flock on this platform: the lock is a no-op
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

LOCK_SUFFIX = ".lock"


class FileModeMemory:
    """Modes in one JSON file; safe for concurrent calls and for several processes."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def recall(self, digest: bytes, max_age_s: float) -> int | None:
        modes = await asyncio.to_thread(self._load)
        entry = modes.get(digest.hex())
        if entry is None or self._clock() - entry[1] > max_age_s:
            return None
        return entry[0]

    async def remember(self, digest: bytes, index: int, max_age_s: float) -> None:
        async with self._lock:
            await asyncio.to_thread(self._update, digest.hex(), index, max_age_s)

    def clear(self) -> int:
        """Forget every entry; returns how many were removed. Raises on I/O errors."""
        with self._exclusive():
            removed = len(self._load())
            self._path.unlink(missing_ok=True)
        return removed

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        lock = self._path.with_name(self._path.name + LOCK_SUFFIX)
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)  # closing releases the lock

    def _load(self) -> dict[str, tuple[int, float]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError:
            logger.warning("structured output mode file unreadable", exc_info=True)
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            logger.warning("structured output mode file is not valid JSON; ignored")
            return {}
        if not isinstance(data, dict):
            return {}
        modes: dict[str, tuple[int, float]] = {}
        for key, entry in data.items():
            parsed = _entry(entry)
            if isinstance(key, str) and parsed is not None:
                modes[key] = parsed
        return modes

    def _update(self, key: str, index: int, max_age_s: float) -> None:
        try:
            with self._exclusive():
                self._write(key, index, max_age_s)
        except OSError:
            logger.warning("structured output mode file not written", exc_info=True)

    def _write(self, key: str, index: int, max_age_s: float) -> None:
        now = self._clock()
        modes = {k: v for k, v in self._load().items() if now - v[1] <= max_age_s}
        held = modes.get(key)
        if held is not None and now - held[1] <= max_age_s and held[0] > index:
            index = held[0]
        modes[key] = (index, now)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.", suffix=".tmp", dir=self._path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {k: {"index": i, "at": at} for k, (i, at) in modes.items()},
                    handle,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def _entry(entry: Any) -> tuple[int, float] | None:
    """`(index, at)` of a well-formed entry; None for anything else (old bare numbers too)."""
    if not isinstance(entry, dict):
        return None
    index, at = entry.get("index"), entry.get("at")
    if isinstance(index, bool) or not isinstance(index, int | float):
        return None
    if isinstance(at, bool) or not isinstance(at, int | float):
        return None
    return int(index), float(at)


def mode_memory_path(data_dir: str, file_name: str | None) -> Path | None:
    """Where the modes are kept: `file_name` under `data_dir` (`~` expanded) unless absolute;
    None when the memory is turned off."""
    if not file_name:
        return None
    path = Path(file_name).expanduser()
    if path.is_absolute():
        return path
    return Path(data_dir).expanduser() / path
