"""Performance harness at a small scale (`pytest -m perf`; skipped otherwise).

Checks that the harness runs end to end and that every read stays index-driven for the owner
role; latency targets are asserted only by the full baseline run (`bench.py`), not here.
"""

from __future__ import annotations

import pytest

from . import bench

pytestmark = pytest.mark.perf


def test_small_scale_baseline() -> None:
    report = bench.main(
        [
            "--scale",
            "3",
            "--extra-links",
            "2",
            "--iterations",
            "20",
            "--warmup",
            "5",
            "--branch-iterations",
            "1",
            "--commit-iterations",
            "1",
            "--commit-changes",
            "50",
        ]
    )
    assert report.seeded.records > 500
    assert report.seeded.links > report.seeded.records
    assert set(report.owner) == set(bench.harness.TARGETS_MS)
    assert all(m.samples_ms for m in report.reader.values())
    for name in ("get by key", "list page of 50"):
        for role in ("owner", "rls"):
            assert all("Seq Scan" not in node for node in report.plans[name][role]), name
