"""Performance harness at a small scale (`pytest -m perf`; skipped otherwise).

Checks that the harness runs end to end and that every read stays index-driven for the owner
role; latency targets are asserted only by the full baseline run (`bench.py`), not here, except
the overhead of full-text search on an overlay branch over a copy.
"""

from __future__ import annotations

import pytest

from . import bench

pytestmark = pytest.mark.perf

MAX_OVERLAY_SEARCH_OVERHEAD = 0.25
"""Overlay p50 over copy p50, minus one, for full-text search (docs/performance.md)."""


def test_small_scale_baseline() -> None:
    report = bench.main(
        [
            "--scale",
            "3",
            "--extra-links",
            "2",
            "--iterations",
            "60",
            "--warmup",
            "10",
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
    assert report is not None
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
    for role in ("owner", "rls"):
        copy = report.copy_reads[role]["full-text search"].p50
        overlay = report.overlay_reads[role]["full-text search"].p50
        assert overlay / copy - 1 < MAX_OVERLAY_SEARCH_OVERHEAD, (role, copy, overlay)
