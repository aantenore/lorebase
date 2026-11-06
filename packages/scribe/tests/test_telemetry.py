"""The queue depth gauge reports the scheduler leader's latest count; the worker slots gauge
the job loop's."""

from __future__ import annotations

from lorebase_scribe.telemetry import QueueDepth, WorkerSlots


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


def slots_observed(slots: WorkerSlots) -> dict[str, float]:
    return {
        str(point.attributes["state"]): point.value
        for point in slots.observe()
        if point.attributes is not None
    }


def test_worker_slots_report_live_and_lost_while_the_worker_runs() -> None:
    slots = WorkerSlots()
    assert slots_observed(slots) == {}
    slots.update(live=1, configured=2)
    assert slots_observed(slots) == {"live": 1, "lost": 1}
    slots.update(live=2, configured=2)
    assert slots_observed(slots) == {"live": 2, "lost": 0}
    slots.clear()
    assert slots_observed(slots) == {}
