"""Five-field cron expressions."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lorebase_scribe.cron import CronError, parse_cron


def at(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


@pytest.mark.parametrize(
    ("expression", "moment", "expected"),
    [
        ("* * * * *", at(2026, 10, 21, 9, 30, 45), at(2026, 10, 21, 9, 30)),
        ("0 3 * * *", at(2026, 10, 21, 9, 30), at(2026, 10, 21, 3, 0)),
        ("0 3 * * *", at(2026, 10, 21, 2, 59), at(2026, 10, 20, 3, 0)),
        ("*/15 * * * *", at(2026, 10, 21, 9, 44), at(2026, 10, 21, 9, 30)),
        ("0 2 * * 1-5", at(2026, 10, 25, 12, 0), at(2026, 10, 23, 2, 0)),  # Sunday -> Friday
        ("30 6 1 * *", at(2026, 10, 21), at(2026, 10, 1, 6, 30)),
        ("0 0 1 1 *", at(2026, 10, 21), at(2026, 1, 1)),
        ("0 12 * * 7", at(2026, 10, 21), at(2026, 10, 18, 12, 0)),  # 7 is Sunday
    ],
)
def test_previous_firing(expression: str, moment: datetime, expected: datetime) -> None:
    assert parse_cron(expression).previous(moment) == expected


def test_day_of_month_or_day_of_week_when_both_are_restricted() -> None:
    cron = parse_cron("0 0 13 * 5")  # the 13th, or any Friday
    assert cron.matches(at(2026, 10, 13))  # a Tuesday
    assert cron.matches(at(2026, 10, 16))  # a Friday
    assert not cron.matches(at(2026, 10, 14))


@pytest.mark.parametrize(
    "expression",
    ["* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *"],
)
def test_invalid_expressions(expression: str) -> None:
    with pytest.raises(CronError):
        parse_cron(expression)
