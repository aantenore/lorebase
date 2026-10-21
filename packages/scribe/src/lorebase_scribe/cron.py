"""Five-field cron expressions (minute hour day-of-month month day-of-week), evaluated in UTC.

Fields accept `*`, numbers, ranges `a-b`, lists `a,b` and steps `*/n` or `a-b/n`. Day of week
runs from 0 (Sunday) to 6, with 7 also meaning Sunday. As in classic cron, when both day of
month and day of week are restricted a day matching either one matches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = ("minute", "hour", "day of month", "month", "day of week")
_SEARCH_LIMIT = timedelta(days=366 * 5)


class CronError(ValueError):
    """The expression is not a valid five-field cron expression."""


def _field(text: str, index: int) -> frozenset[int]:
    low, high = _BOUNDS[index]
    values: set[int] = set()
    for part in text.split(","):
        body, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"bad step {step_text!r} in the {_NAMES[index]} field")
            step = int(step_text)
        if body == "*":
            start, end = low, high
        elif "-" in body:
            first, _, last = body.partition("-")
            if not (first.isdigit() and last.isdigit()):
                raise CronError(f"bad range {body!r} in the {_NAMES[index]} field")
            start, end = int(first), int(last)
        elif body.isdigit():
            start = end = int(body)
            if step_text:
                end = high
        else:
            raise CronError(f"bad value {body!r} in the {_NAMES[index]} field")
        if not (low <= start <= end <= high):
            raise CronError(f"{body!r} is outside {low}-{high} in the {_NAMES[index]} field")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    """A parsed expression; `matches` and `previous` work on UTC datetimes."""

    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        parts = expression.split()
        if len(parts) != len(_BOUNDS):
            raise CronError(f"expected 5 fields, got {len(parts)}: {expression!r}")
        fields = [_field(text, index) for index, text in enumerate(parts)]
        weekdays = frozenset(0 if day == 7 else day for day in fields[4])
        return cls(
            expression=expression,
            minutes=fields[0],
            hours=fields[1],
            days=fields[2],
            months=fields[3],
            weekdays=weekdays,
            any_day=parts[2] == "*",
            any_weekday=parts[4] == "*",
        )

    def _day_matches(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        by_day = moment.day in self.days
        by_weekday = (moment.isoweekday() % 7) in self.weekdays
        if self.any_day or self.any_weekday:
            return by_day and by_weekday
        return by_day or by_weekday

    def matches(self, moment: datetime) -> bool:
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and self._day_matches(moment)
        )

    def previous(self, moment: datetime) -> datetime:
        """The latest firing time at or before `moment` (whole minutes)."""
        current = moment.replace(second=0, microsecond=0)
        limit = current - _SEARCH_LIMIT
        while current >= limit:
            if not self._day_matches(current):
                current = current.replace(hour=23, minute=59) - timedelta(days=1)
                continue
            if current.hour not in self.hours:
                current = current.replace(minute=59) - timedelta(hours=1)
                continue
            if current.minute in self.minutes:
                return current
            current -= timedelta(minutes=1)
        raise CronError(f"{self.expression!r} never fires")


def parse_cron(expression: str) -> Cron:
    """Parse a cron expression; raises `CronError` with the offending field."""
    return Cron.parse(expression)
