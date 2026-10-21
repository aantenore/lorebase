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
            "--overlay-iterations",
            "3",
            "--overlay-changes",
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
    assert report.pair.overlay.overlay
    assert 0 < report.pair.overlay_rows < report.pair.copy_rows
    for role in ("owner", "rls"):
        assert set(report.overlay_reads[role]) == set(bench.harness.TARGETS_MS)
        assert all(m.samples_ms for m in report.copy_reads[role].values())
    assert report.overlay_branch.samples_ms
