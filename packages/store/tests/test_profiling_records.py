"""The profiler of rows read as records."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from lorebase_core.infer import infer_schema
from lorebase_core.types import AttributeType
from lorebase_store.profiling import (
    RecordProfileError,
    RecordProfiler,
    RowSampler,
    profile_records,
)
from lorebase_store.settings import RecordProfilingSettings


def test_columns_are_every_key_in_first_seen_order() -> None:
    rows = [{"a": 1, "b": 2}, {"c": 3}, {"b": 4, "a": 5, "d": None}]
    profile = profile_records(rows, "items", RecordProfilingSettings())
    assert profile.name == "items"
    assert profile.row_count == 3
    assert [column.name for column in profile.columns] == ["a", "b", "c", "d"]
    assert list(profile.columns[2].values) == [None, 3, None]


def test_a_declared_header_comes_first() -> None:
    profiler = RecordProfiler(RecordProfilingSettings())
    profiler.add({"b": 1, "extra": 2})
    profile = profiler.profile("t", ("a", "b"))
    assert [column.name for column in profile.columns] == ["a", "b", "extra"]


@pytest.mark.parametrize("sampling", ["head", "reservoir"])
def test_samples_keep_read_order_and_count_every_row(sampling: str) -> None:
    sampler: RowSampler[int] = RowSampler(10, sampling, 7)  # type: ignore[arg-type]
    for value in range(1000):
        sampler.add(value)
    kept = sampler.kept()
    assert sampler.total == 1000
    assert len(kept) == 10
    assert kept == sorted(kept)
    if sampling == "head":
        assert kept == list(range(10))
    else:
        assert kept != list(range(10))
        again: RowSampler[int] = RowSampler(10, "reservoir", 7)
        for value in range(1000):
            again.add(value)
        assert again.kept() == kept


def test_wide_tables_are_refused() -> None:
    with pytest.raises(RecordProfileError, match="2 columns"):
        profile_records([{"a": 1, "b": 2, "c": 3}], "t", RecordProfilingSettings(max_columns=2))


def test_native_values_feed_inference() -> None:
    rows = [
        {
            "code": f"P-{n:03d}",
            "price": Decimal(f"{n}.25"),
            "since": date(2026, 1, 1 + n % 28),
            "meta": {"n": n},
        }
        for n in range(40)
    ]
    proposal = infer_schema([profile_records(rows, "parts", RecordProfilingSettings())], "x")
    kind = next(iter(proposal.schema.kinds.values()))
    types = {a.api_name: a.type.name for a in kind.attributes.values()}
    assert types["price"] is AttributeType.DECIMAL
    assert types["since"] is AttributeType.DATE
    assert types["meta"] is AttributeType.JSON
