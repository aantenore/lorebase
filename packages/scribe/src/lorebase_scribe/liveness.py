"""Liveness of a job loop through a file it rewrites (`scribe.worker.liveness_file`).

Scribe serves no HTTP, so a container healthcheck or a Kubernetes probe reads the file the
worker rewrites every `scribe.worker.liveness_interval_s`:

    python -m lorebase_scribe.liveness /tmp/scribe-liveness.json [--max-age SECONDS]
                                       [--min-slots N] [--max-store-age SECONDS]

The check passes (exit 0, one line on stdout) while the file was written within `--max-age`
(default: `STALE_INTERVALS` times the interval recorded in it) by a loop with at least
`--min-slots` live slots (default 1); with `--max-store-age` the loop must also have reached
the store, by a claim or a heartbeat, within that long (a readiness probe). Otherwise it exits
1 and says why: no file (the worker has not started, or the setting is unset), a stale file
(the process hangs or is gone), no live slot, or a store not reached. The module imports the
standard library only, so a check costs one interpreter start.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STALE_INTERVALS = 3
"""Default `--max-age`, as a multiple of the interval the file records."""


@dataclass(frozen=True)
class Liveness:
    """What the loop reports: written by `write_liveness`, read back by `check_liveness`."""

    worker: str
    interval_s: float
    live_slots: int
    configured_slots: int
    running: tuple[int, ...]
    store_reached_at: float | None
    """Wall-clock time (`time.time()`) of the last claim or heartbeat round trip."""
    written_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker,
            "interval_s": self.interval_s,
            "slots": {"live": self.live_slots, "configured": self.configured_slots},
            "running": list(self.running),
            "store_reached_at": self.store_reached_at,
            "written_at": self.written_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Liveness:
        slots = data["slots"]
        reached = data.get("store_reached_at")
        return cls(
            worker=str(data["worker"]),
            interval_s=float(data["interval_s"]),
            live_slots=int(slots["live"]),
            configured_slots=int(slots["configured"]),
            running=tuple(int(job) for job in data.get("running", [])),
            store_reached_at=None if reached is None else float(reached),
            written_at=float(data["written_at"]),
        )


def write_liveness(path: Path, state: Liveness) -> None:
    """Write `state` to `path` in one step (a reader never sees a partial file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state.as_dict(), sort_keys=True))
    os.replace(tmp, path)


def remove_liveness(path: Path) -> None:
    """Remove the file (a loop that stopped leaves none behind for a probe to trust)."""
    path.unlink(missing_ok=True)


def read_liveness(path: Path) -> Liveness:
    """The state in `path`; `OSError` when it cannot be read, `ValueError` when it is not one."""
    try:
        return Liveness.from_dict(json.loads(path.read_text()))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"not a liveness file: {exc}") from exc


def check_liveness(
    path: Path,
    *,
    now: float | None = None,
    max_age_s: float | None = None,
    min_slots: int = 1,
    max_store_age_s: float | None = None,
) -> tuple[bool, str]:
    """Whether the file says the loop is live, and one line saying why."""
    try:
        state = read_liveness(path)
    except OSError as exc:
        return (
            False,
            f"no liveness file at {path} ({exc.strerror or exc}): the worker has not started",
        )
    except ValueError as exc:
        return False, f"{path}: {exc}"
    at = time.time() if now is None else now
    age = at - state.written_at
    limit = state.interval_s * STALE_INTERVALS if max_age_s is None else max_age_s
    summary = (
        f"{state.live_slots} of {state.configured_slots} slots live, "
        f"{len(state.running)} jobs running, written {age:.1f} s ago"
    )
    if age > limit:
        return False, f"liveness file is {age:.1f} s old (limit {limit:g} s): {summary}"
    if state.live_slots < min_slots:
        return False, f"fewer than {min_slots} live slots: {summary}"
    if max_store_age_s is not None:
        if state.store_reached_at is None:
            return False, f"the store was not reached yet: {summary}"
        store_age = at - state.store_reached_at
        if store_age > max_store_age_s:
            return (
                False,
                f"the store was last reached {store_age:.1f} s ago (limit "
                f"{max_store_age_s:g} s): {summary}",
            )
    return True, f"live: {summary}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lorebase_scribe.liveness", description=__doc__.split("\n")[0]
    )
    parser.add_argument("path", type=Path, help="The file named by scribe.worker.liveness_file.")
    parser.add_argument(
        "--max-age",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"Oldest file accepted (default: {STALE_INTERVALS} times the interval it records).",
    )
    parser.add_argument(
        "--min-slots", type=int, default=1, metavar="N", help="Live slots required (default 1)."
    )
    parser.add_argument(
        "--max-store-age",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Also require the store reached within this long (a readiness probe).",
    )
    args = parser.parse_args(argv)
    live, reason = check_liveness(
        args.path,
        max_age_s=args.max_age,
        min_slots=args.min_slots,
        max_store_age_s=args.max_store_age,
    )
    print(reason)
    return 0 if live else 1


if __name__ == "__main__":
    raise SystemExit(main())
