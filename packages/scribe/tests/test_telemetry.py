"""The queue depth gauge reports the scheduler leader's latest count."""

from __future__ import annotations

from lorebase_scribe.telemetry import QueueDepth


def observed(depth: QueueDepth) -> dict[tuple[str, str], float]:
    return {
        (str(point.attributes["type"]), str(point.attributes["status"])): point.value
        for point in depth.observe()
        if point.attributes is not None
    }


def test_a_pair_that_empties_reports_zero() -> None:
    depth = QueueDepth()
    depth.update({("gc", "queued"): 2, ("extraction", "running"): 1})
    depth.update({("gc", "queued"): 1})
    assert observed(depth) == {("gc", "queued"): 1, ("extraction", "running"): 0}


def test_nothing_is_reported_after_leadership_is_lost() -> None:
    depth = QueueDepth()
    depth.update({("gc", "queued"): 2})
    depth.clear()
    assert observed(depth) == {}
