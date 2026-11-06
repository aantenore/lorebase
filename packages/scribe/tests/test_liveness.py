"""The liveness check reads what the job loop wrote and says why it fails."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from lorebase_scribe.liveness import (
    STALE_INTERVALS,
    Liveness,
    check_liveness,
    main,
    write_liveness,
)

NOW = 1_800_000_000.0


def state(**overrides: object) -> Liveness:
    values: dict[str, object] = {
        "worker": "host:1",
        "interval_s": 10.0,
        "live_slots": 2,
        "configured_slots": 2,
        "running": (7,),
        "store_reached_at": NOW - 3,
        "written_at": NOW - 1,
    }
    values.update(overrides)
    return Liveness(**values)  # type: ignore[arg-type]


def test_a_fresh_file_with_live_slots_passes(tmp_path: Path) -> None:
    path = tmp_path / "liveness.json"
    write_liveness(path, state())
    live, reason = check_liveness(path, now=NOW)
    assert live
    assert reason == "live: 2 of 2 slots live, 1 jobs running, written 1.0 s ago"
    assert not list(tmp_path.glob("*.tmp"))  # written in one step


def test_a_stale_file_fails_after_three_intervals(tmp_path: Path) -> None:
    path = tmp_path / "liveness.json"
    write_liveness(path, state(written_at=NOW - 10 * STALE_INTERVALS - 1))
    live, reason = check_liveness(path, now=NOW)
    assert not live
    assert reason.startswith("liveness file is 31.0 s old (limit 30 s)")
    assert check_liveness(path, now=NOW, max_age_s=60)[0]


def test_no_live_slot_fails(tmp_path: Path) -> None:
    path = tmp_path / "liveness.json"
    write_liveness(path, state(live_slots=0))
    live, reason = check_liveness(path, now=NOW)
    assert not live
    assert reason.startswith("fewer than 1 live slots: 0 of 2 slots live")
    assert check_liveness(path, now=NOW, min_slots=0)[0]


def test_the_store_age_is_checked_only_when_asked(tmp_path: Path) -> None:
    path = tmp_path / "liveness.json"
    write_liveness(path, state(store_reached_at=NOW - 120))
    assert check_liveness(path, now=NOW)[0]
    live, reason = check_liveness(path, now=NOW, max_store_age_s=60)
    assert not live
    assert reason.startswith("the store was last reached 120.0 s ago (limit 60 s)")
    write_liveness(path, state(store_reached_at=None))
    assert "not reached yet" in check_liveness(path, now=NOW, max_store_age_s=60)[1]


def test_a_missing_or_broken_file_fails_with_the_reason(tmp_path: Path) -> None:
    missing = tmp_path / "none.json"
    live, reason = check_liveness(missing)
    assert not live
    assert reason.startswith(f"no liveness file at {missing}")
    broken = tmp_path / "broken.json"
    broken.write_text("{}")
    live, reason = check_liveness(broken)
    assert not live
    assert "not a liveness file" in reason


def test_main_exits_with_the_verdict(tmp_path: Path) -> None:
    path = tmp_path / "liveness.json"
    write_liveness(path, state(written_at=time.time() - 10))
    assert main([str(path), "--max-age", str(10**9)]) == 0
    assert main([str(path), "--max-age", "1"]) == 1
    assert main([str(path), "--max-age", str(10**9), "--min-slots", "3"]) == 1


def test_the_module_runs_without_the_rest_of_scribe_imported(tmp_path: Path) -> None:
    """A healthcheck pays for one interpreter start, not for Scribe's imports."""
    path = tmp_path / "liveness.json"
    write_liveness(path, state())
    code = (
        "import sys; from lorebase_scribe.liveness import main; "
        f"status = main([{str(path)!r}, '--max-age', '1000000000']); "
        "heavy = [m for m in sys.modules if m.startswith(('sqlalchemy', 'lorebase_store'))]; "
        "print(status, heavy)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.splitlines()[-1] == "0 []"
