"""examples/merge_demo.py runs end to end and tells the same story every time."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "examples" / "merge_demo.py"


def run_demo() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DEMO)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_merge_demo_runs_and_reports_the_story() -> None:
    first = run_demo()
    assert first.returncode == 0, first.stderr
    lines = first.stdout.splitlines()
    for expected in (
        "schema: 6 Kinds, 7 Relations, 0 problems",
        "fast_forward: 34 changes in 4 partitions",
        "DV1 ATTRIBUTE_VALUE (1)",
        "DV2 DELETE_MODIFY (1)",
        "DV3 ADD_ADD (2)",
        "XS4 MISSING_VALUE (1)",
        "validation: 72 Records against the merged schema, 0 issues",
        "S-1001 risk_score: 32 -> 75",
    ):
        assert expected in lines
    assert any(line.startswith("merged: Revision ") for line in lines)
    assert "5 Conflicts" in first.stdout
    assert run_demo().stdout == first.stdout  # deterministic
