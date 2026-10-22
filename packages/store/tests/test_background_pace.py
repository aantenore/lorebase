"""Batch sizing of background branch jobs (`materialize.background._Pace`): batches start
small, grow while they hold the branch lock briefly, and shrink toward a shorter hold once
a commit waited for the lock."""

from __future__ import annotations

from lorebase_store.materialize.background import _Pace
from lorebase_store.settings import MaterializeSettings

SETTINGS = MaterializeSettings(
    job_batch_rows=5000, job_batch_min_rows=100, job_batch_target_ms=200, job_batch_contended_ms=50
)


def test_batches_start_small_and_grow_while_the_lock_is_held_briefly() -> None:
    pace = _Pace.of(SETTINGS)
    assert pace.size == 100
    for _ in range(10):
        pace.next(0.01, contended=False)
    assert pace.size == 5000, "capped at job_batch_rows"


def test_a_long_batch_shrinks_the_next_toward_the_target() -> None:
    pace = _Pace.of(SETTINGS)
    pace.size = 4000
    pace.next(0.4, contended=False)
    assert pace.size == 2000
    pace.next(0.15, contended=False)
    assert pace.size == 2000, "within the target: unchanged"


def test_a_waiting_commit_aims_at_the_contended_hold() -> None:
    pace = _Pace.of(SETTINGS)
    pace.size = 2000
    pace.next(0.2, contended=True)
    assert pace.size == 500
    pace.next(float("inf"), contended=True)
    assert pace.size == 100, "never below job_batch_min_rows"


def test_contention_is_a_lock_wait_or_a_waiting_commit() -> None:
    pace = _Pace.of(SETTINGS)
    assert not pace.contended(0.001, 0)
    assert pace.contended(0.01, 0)
    assert pace.contended(0.0, 1)


def test_the_smallest_batch_never_exceeds_the_largest() -> None:
    pace = _Pace.of(MaterializeSettings(job_batch_rows=3, job_batch_min_rows=100))
    assert (pace.low, pace.size, pace.high) == (3, 3, 3)
