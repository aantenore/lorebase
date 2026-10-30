"""Resolution misses (design 8.10): which phrases a resolution records, and the in-memory
aggregation of the recorder before its batch write."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from lorebase_keeper.reading import misses as module
from lorebase_keeper.reading.misses import MissRecorder, missed_phrases, unknown_runs
from lorebase_keeper.reading.resolve import weak_phrases
from lorebase_keeper.reading.settings import ResolveMissRecording
from lorebase_keeper.reading.views import ResolveCandidate
from lorebase_store.resolve_misses import MissSighting, caller_digest
from lorebase_store.settings import ResolveMissSettings


def test_unknown_runs_are_consecutive_uncovered_words() -> None:
    words = ["average", "price", "of", "fire", "engine", "red", "shoes"]
    assert unknown_runs(words, {1, 6}, 3) == ["average", "of fire engine", "red"]
    assert unknown_runs(words, set(range(7)), 3) == []
    assert unknown_runs(["a", "b", "a", "b"], (), 2) == ["a b"]


def test_recorded_phrases_respect_bounds_and_ignored_words() -> None:
    settings = ResolveMissSettings(
        enabled=True, min_phrase_chars=3, max_phrase_chars=12, ignore_words=("How", "Many")
    )
    assert missed_phrases(
        ["how", "of", "many", "colour", "a very long phrase indeed"], ["teal"], settings
    ) == ["colour", "teal"]


def test_weak_phrases_need_a_threshold() -> None:
    candidates = [
        ResolveCandidate(target="kind", kind="Shoe", score=0.4, level="trigram", matched="sho"),
        ResolveCandidate(target="kind", kind="Shoe", score=0.9, level="exact", matched="shoe"),
    ]
    assert weak_phrases(candidates, None) == []
    assert weak_phrases(candidates, 0.5) == ["sho"]


def test_caller_digests_name_nobody_and_differ_per_domain() -> None:
    first = caller_digest("shop", "usr_alice")
    assert len(first) == 16
    assert "alice" not in first
    assert first == caller_digest("shop", "usr_alice")
    assert first != caller_digest("supply", "usr_alice")


async def test_recorder_aggregates_then_writes_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[list[MissSighting]] = []

    async def record(_: Any, batch: Sequence[MissSighting], *, settings: Any) -> int:
        written.append(list(batch))
        return len(batch)

    class Sessions:
        def __call__(self) -> Sessions:
            return self

        async def __aenter__(self) -> Sessions:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def begin(self) -> Sessions:
            return self

    monkeypatch.setattr(module, "record_misses", record)
    now = datetime(2026, 11, 2, tzinfo=UTC)
    recorder = MissRecorder(
        Sessions(),  # type: ignore[arg-type]
        ResolveMissSettings(enabled=True, max_callers=2),
        ResolveMissRecording(queue_size=2, flush_interval_s=60),
        clock=lambda: now,
    )
    assert recorder.offer("shop", "main", ["colour", "teal"], "c1") == 2
    assert recorder.offer("shop", "main", ["colour", "maroon"], "c2") == 1  # queue full
    assert recorder.offer("shop", "main", ["colour"], "c3") == 1
    await recorder.aclose()
    assert len(written) == 1
    by_phrase = {s.phrase: s for s in written[0]}
    assert set(by_phrase) == {"colour", "teal"}
    assert by_phrase["colour"].misses == 3
    assert by_phrase["colour"].callers == ("c1", "c2")  # bounded by max_callers
    assert by_phrase["teal"].first_seen == now


async def test_recorder_bounds_new_phrases_per_caller_and_finishes_a_write_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written: list[MissSighting] = []
    started = asyncio.Event()

    async def record(_: Any, batch: Sequence[MissSighting], *, settings: Any) -> int:
        started.set()
        await asyncio.sleep(0.05)  # a slow write, in flight when the recorder closes
        written.extend(batch)
        return len(batch)

    class Sessions:
        def __call__(self) -> Sessions:
            return self

        async def __aenter__(self) -> Sessions:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def begin(self) -> Sessions:
            return self

    monkeypatch.setattr(module, "record_misses", record)
    recorder = MissRecorder(
        Sessions(),  # type: ignore[arg-type]
        ResolveMissSettings(enabled=True),
        ResolveMissRecording(max_new_per_caller=2, flush_interval_s=0.01),
    )
    assert recorder.offer("shop", "main", ["aa1", "aa2", "aa3"], "spammer") == 2
    assert recorder.offer("shop", "main", ["aa1"], "spammer") == 1  # known phrases count
    assert recorder.offer("other", "main", ["bb1"], "spammer") == 1  # per Domain
    await asyncio.wait_for(started.wait(), 1)
    await recorder.aclose()
    assert sorted(s.phrase for s in written) == ["aa1", "aa2", "bb1"]
    assert {s.phrase: s.misses for s in written}["aa1"] == 2
