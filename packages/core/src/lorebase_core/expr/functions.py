"""The Lorebase function library: signatures for the checker, implementations for evaluation.

Global functions
    ``now()``, ``today()``; ``record(ref)``, ``links(ref, "relation")`` (Operations only);
    ``size(x)``, ``count(list)``; conversions ``int``, ``double``, ``decimal``, ``string``,
    ``date``, ``timestamp``, ``duration`` (``"1h30m"`` or ISO 8601 ``"P1DT2H"``); durations
    ``days(n)``, ``hours(n)``, ``minutes(n)``, ``seconds(n)``; ``days_between(a, b)``;
    math ``abs``, ``round(x, places)`` (half up), ``floor``, ``ceil``, ``min``, ``max``
    (two values or a list), ``sum(list)``; ``matches(text, regex)``.
Methods
    strings: ``size``, ``contains``, ``startsWith``, ``endsWith``, ``matches`` (RE2,
    partial match), ``lower``, ``upper``, ``trim``, ``replace``, ``split``, ``substring``,
    ``indexOf``; lists: ``size``, ``join``; dates and timestamps: ``getFullYear``,
    ``getMonth`` (0-based), ``getDate`` (1-based), ``getDayOfMonth`` (0-based),
    ``getDayOfWeek`` (0 is Sunday), ``getDayOfYear`` (0-based); timestamps and durations:
    ``getHours``, ``getMinutes``, ``getSeconds`` (timestamps in UTC; durations in total).

Regular expressions use RE2 (linear time). Literal patterns are validated when compiling.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Context, Decimal
from typing import Protocol

import re2  # type: ignore[import-untyped]

from lorebase_core.expr import ops
from lorebase_core.expr.ast import Call, Literal
from lorebase_core.expr.environment import Environment
from lorebase_core.expr.errors import ExprError, evaluation_error, type_error
from lorebase_core.expr.limits import ExprLimits
from lorebase_core.expr.types import (
    BOOL,
    DATE,
    DECIMAL,
    DOUBLE,
    DURATION,
    DYN,
    INT,
    NUMERIC,
    ORDERED,
    STRING,
    TIMESTAMP,
    ExprType,
    TypeKind,
    join,
    list_of,
    record_of,
)
from lorebase_core.expr.values import RecordValue, Ref, type_name
from lorebase_core.types import AttributeType, EncodingError, TypeSpec, decode_value, encode_value
from lorebase_core.types.encoding import (
    float_text,
    format_date,
    format_duration,
    format_timestamp,
    to_decimal,
)

_TIMESTAMP_SPEC = TypeSpec.of(AttributeType.TIMESTAMP)
_DATE_SPEC = TypeSpec.of(AttributeType.DATE)
_DURATION_SPEC = TypeSpec.of(AttributeType.DURATION)
_INT_TEXT = re.compile(r"[+-]?[0-9]+")
_CEL_DURATION = re.compile(r"(-)?((?:[0-9]+(?:\.[0-9]+)?(?:h|ms|m|s|us|ns))+)")
_CEL_DURATION_PART = re.compile(r"([0-9]+(?:\.[0-9]+)?)(h|ms|m|s|us|ns)")
_MICROSECONDS = {
    "h": Decimal(3_600_000_000),
    "m": Decimal(60_000_000),
    "s": Decimal(1_000_000),
    "ms": Decimal(1000),
    "us": Decimal(1),
    "ns": Decimal("0.001"),
}


class Runtime(Protocol):
    """What implementations need from the evaluator."""

    @property
    def now(self) -> datetime: ...

    @property
    def limits(self) -> ExprLimits: ...

    @property
    def decimals(self) -> Context: ...

    def load_record(self, ref: Ref) -> RecordValue: ...

    def load_links(self, ref: Ref, name: str) -> list[RecordValue]: ...

    def text(self, value: str) -> str:
        """``value`` if within ``max_string_length``; raises otherwise."""
        ...

    def items(self, value: list[object]) -> list[object]:
        """``value`` if within ``max_list_size``; raises otherwise."""
        ...

    def regex(self, pattern: str) -> Callable[[str], bool]: ...


@dataclass(frozen=True)
class CallSite:
    """A call being type checked: argument types (the target first for methods)."""

    node: Call
    args: tuple[ExprType, ...]
    environment: Environment

    def literal(self, index: int) -> object:
        """Value of argument ``index`` (target excluded) when it is a literal, else ``None``."""
        if index < len(self.node.args) and isinstance(self.node.args[index], Literal):
            literal = self.node.args[index]
            assert isinstance(literal, Literal)
            return literal.value
        return None

    def describe(self) -> str:
        return ", ".join(self.environment.describe(t) for t in self.args)


Check = Callable[[CallSite], ExprType]
Run = Callable[[Runtime, Sequence[object]], object]


@dataclass(frozen=True)
class Function:
    name: str
    member: bool
    check: Check
    run: Run
    records: bool = False


def compile_regex(pattern: str) -> Callable[[str], bool]:
    """An RE2 search function; raises ``ValueError`` for an invalid pattern."""
    options = re2.Options()
    options.log_errors = False
    try:
        compiled = re2.compile(pattern, options)
    except re2.error as error:
        detail = error.args[0] if error.args else error
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        raise ValueError(f"invalid regular expression: {detail}") from None

    def search(text: str) -> bool:
        return compiled.search(text) is not None

    return search


# Signatures -----------------------------------------------------------------------------------

Result = ExprType | Callable[[tuple[ExprType, ...]], ExprType]


def signatures(name: str, *overloads: tuple[tuple[TypeKind | None, ...], Result]) -> Check:
    """A checker accepting the listed parameter kinds (``None`` accepts any type)."""

    def check(site: CallSite) -> ExprType:
        results: list[ExprType] = []
        arities = {len(params) for params, _ in overloads}
        for params, result in overloads:
            if len(params) != len(site.args):
                continue
            if all(
                p is None or a.kind is TypeKind.DYN or a.kind is p
                for p, a in zip(params, site.args, strict=True)
            ):
                results.append(result(site.args) if callable(result) else result)
        if not results:
            if len(site.args) not in arities:
                expected = " or ".join(str(n) for n in sorted(arities))
                raise type_error(
                    f"{name}() takes {expected} argument(s), got {len(site.args)}", site.node.pos
                )
            raise type_error(f"no overload of {name}() accepts ({site.describe()})", site.node.pos)
        if all(r == results[0] for r in results):
            return results[0]
        return DYN

    return check


def _first(args: tuple[ExprType, ...]) -> ExprType:
    return args[0].plain()


K = TypeKind


# Implementations ------------------------------------------------------------------------------


def _size(rt: Runtime, args: Sequence[object]) -> object:
    (value,) = args
    if isinstance(value, str | bytes | list | dict):
        return len(value)
    raise evaluation_error(f"size() is not defined for {type_name(value)}")


def _to_int(rt: Runtime, args: Sequence[object]) -> object:
    (value,) = args
    if isinstance(value, bool):
        raise evaluation_error("int() does not convert bools")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise evaluation_error("int() of a non-finite double")
        return ops.check_int(math.trunc(value))
    if isinstance(value, Decimal):
        return ops.check_int(int(value))
    if isinstance(value, str):
        if not _INT_TEXT.fullmatch(value.strip()):
            raise evaluation_error(f"int() cannot parse {value!r}")
        return ops.check_int(int(value))
    if isinstance(value, datetime):
        return math.floor(value.timestamp())
    raise evaluation_error(f"int() is not defined for {type_name(value)}")


def _to_double(rt: Runtime, args: Sequence[object]) -> object:
    (value,) = args
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        raise evaluation_error(f"double() is not defined for {type_name(value)}")
    try:
        result = float(to_decimal(value)) if isinstance(value, str) else float(value)
    except (EncodingError, OverflowError) as error:
        raise evaluation_error(f"double() failed: {error}") from None
    if not math.isfinite(result):
        raise evaluation_error("double() result is not finite")
    return result


def _to_decimal(rt: Runtime, args: Sequence[object]) -> object:
    (value,) = args
    try:
        return to_decimal(value)
    except EncodingError as error:
        raise evaluation_error(f"decimal() failed: {error.message}") from None


def _to_string(rt: Runtime, args: Sequence[object]) -> object:
    (value,) = args
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return float_text(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, date):
        return format_date(value)
    if isinstance(value, timedelta):
        return format_duration(value)
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise evaluation_error("string() of bytes that are not UTF-8") from None
    if isinstance(value, Ref | RecordValue):
        return value.key
    raise evaluation_error(f"string() is not defined for {type_name(value)}")


def _decode(spec: TypeSpec, value: str, what: str) -> object:
    try:
        return decode_value(spec, encode_value(spec, value))
    except EncodingError as error:
        raise evaluation_error(f"{what}() cannot parse {value!r}: {error.message}") from None


def _to_date(rt: Runtime, args: Sequence[object]) -> object:
    (value,) = args
    if isinstance(value, datetime):
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return _decode(_DATE_SPEC, value, "date")
    raise evaluation_error(f"date() is not defined for {type_name(value)}")


def _to_timestamp(rt: Runtime, args: Sequence[object]) -> object:
    (value,) = args
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        return _decode(_TIMESTAMP_SPEC, value, "timestamp")
    raise evaluation_error(f"timestamp() is not defined for {type_name(value)}")


def parse_duration(text: str) -> timedelta:
    """CEL duration text (``"1h30m"``, ``"-1.5s"``) or ISO 8601 (``"P1DT2H"``)."""
    stripped = text.strip()
    if stripped.lstrip("-").startswith("P"):
        value = _decode(_DURATION_SPEC, stripped, "duration")
        assert isinstance(value, timedelta)
        return value
    match = _CEL_DURATION.fullmatch(stripped)
    if match is None:
        raise evaluation_error(f"duration() cannot parse {text!r}")
    micros = sum(
        (
            Decimal(amount) * _MICROSECONDS[unit]
            for amount, unit in _CEL_DURATION_PART.findall(text)
        ),
        Decimal(0),
    )
    if micros != micros.to_integral_value():
        raise evaluation_error("durations are precise to the microsecond")
    total = int(micros)
    try:
        return timedelta(microseconds=-total if match.group(1) else total)
    except OverflowError:
        raise evaluation_error("duration is out of range") from None


def _to_duration(rt: Runtime, args: Sequence[object]) -> object:
    (value,) = args
    if isinstance(value, timedelta):
        return value
    if isinstance(value, str):
        return parse_duration(value)
    raise evaluation_error(f"duration() is not defined for {type_name(value)}")


def _unit(unit: str) -> Run:
    def run(rt: Runtime, args: Sequence[object]) -> object:
        (value,) = args
        if isinstance(value, bool) or not isinstance(value, int):
            raise evaluation_error(f"{unit}() takes an int")
        try:
            return timedelta(**{unit: value})
        except OverflowError:
            raise evaluation_error(f"{unit}({value}) is out of range") from None

    return run


def _days_between(rt: Runtime, args: Sequence[object]) -> object:
    start, end = args
    if isinstance(start, datetime) and isinstance(end, datetime):
        return (end - start).days
    if (
        isinstance(start, date)
        and isinstance(end, date)
        and not isinstance(start, datetime)
        and not isinstance(end, datetime)
    ):
        return (end - start).days
    raise evaluation_error(
        f"days_between() takes two dates or two timestamps, got {type_name(start)} "
        f"and {type_name(end)}"
    )


def _abs(rt: Runtime, args: Sequence[object]) -> object:
    (value,) = args
    if isinstance(value, bool) or not ops.is_number(value):
        raise evaluation_error(f"abs() is not defined for {type_name(value)}")
    if isinstance(value, int):
        return ops.check_int(abs(value))
    if isinstance(value, Decimal):
        return rt.decimals.abs(value)
    assert isinstance(value, float)
    return abs(value)


def _rounding(rounding: str, name: str) -> Run:
    def run(rt: Runtime, args: Sequence[object]) -> object:
        value, places = (args[0], args[1]) if len(args) == 2 else (args[0], 0)
        if isinstance(places, bool) or not isinstance(places, int) or places < 0:
            raise evaluation_error(f"{name}() places must be a non-negative int")
        if isinstance(value, bool) or not ops.is_number(value):
            raise evaluation_error(f"{name}() is not defined for {type_name(value)}")
        if isinstance(value, int):
            return value
        exact = value if isinstance(value, Decimal) else to_decimal(value)
        try:
            rounded = exact.quantize(
                Decimal(1).scaleb(-places), rounding=rounding, context=rt.decimals
            )
        except ArithmeticError:
            raise evaluation_error(f"{name}() needs more digits than decimal precision") from None
        return rounded if isinstance(value, Decimal) else float(rounded)

    return run


def _extreme(name: str, pick: Callable[[int], bool]) -> Run:
    def run(rt: Runtime, args: Sequence[object]) -> object:
        values: Sequence[object] = args
        if len(args) == 1:
            (only,) = args
            if not isinstance(only, list):
                raise evaluation_error(f"{name}() of one argument takes a list")
            values = only
        if not values:
            raise evaluation_error(f"{name}() of an empty list")
        best = values[0]
        for value in values[1:]:
            if pick(ops.compare(value, best)):
                best = value
        return best

    return run


def _sum(rt: Runtime, args: Sequence[object]) -> object:
    (values,) = args
    if not isinstance(values, list):
        raise evaluation_error(f"sum() takes a list, got {type_name(values)}")
    total: object = 0
    for value in values:
        total = ops.arithmetic("+", total, value, rt.decimals)
    return total


def _text(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise evaluation_error(f"{what} needs a string, got {type_name(value)}")
    return value


def _string_method(name: str, body: Callable[[Runtime, str, list[str]], object]) -> Run:
    def run(rt: Runtime, args: Sequence[object]) -> object:
        target = _text(args[0], f"{name}()")
        rest = [_text(arg, f"{name}() argument") for arg in args[1:]]
        return body(rt, target, rest)

    return run


def _matches(rt: Runtime, args: Sequence[object]) -> object:
    text = _text(args[0], "matches()")
    pattern = _text(args[1], "matches() pattern")
    return rt.regex(pattern)(text)


def _substring(rt: Runtime, args: Sequence[object]) -> object:
    text = _text(args[0], "substring()")
    bounds = list(args[1:])
    if any(isinstance(b, bool) or not isinstance(b, int) for b in bounds):
        raise evaluation_error("substring() takes int positions")
    start = bounds[0]
    end = bounds[1] if len(bounds) > 1 else len(text)
    assert isinstance(start, int)
    assert isinstance(end, int)
    if not 0 <= start <= end <= len(text):
        raise evaluation_error(f"substring({start}, {end}) is out of range for size {len(text)}")
    return text[start:end]


def _join(rt: Runtime, args: Sequence[object]) -> object:
    items = args[0]
    separator = _text(args[1], "join() separator") if len(args) > 1 else ""
    if not isinstance(items, list):
        raise evaluation_error(f"join() needs a list, got {type_name(items)}")
    return rt.text(separator.join(_text(item, "join() element") for item in items))


def _split(rt: Runtime, target: str, rest: list[str]) -> object:
    if not rest[0]:
        raise evaluation_error("split() needs a non-empty separator")
    return rt.items(list(target.split(rest[0])))


def _date_part(name: str, part: Callable[[date], int]) -> Run:
    def run(rt: Runtime, args: Sequence[object]) -> object:
        (value,) = args
        if isinstance(value, datetime):
            return part(value.astimezone(UTC))
        if isinstance(value, date):
            return part(value)
        raise evaluation_error(f"{name}() is not defined for {type_name(value)}")

    return run


def _time_part(name: str, unit_seconds: int, modulo: int) -> Run:
    def run(rt: Runtime, args: Sequence[object]) -> object:
        (value,) = args
        if isinstance(value, datetime):
            utc = value.astimezone(UTC)
            seconds = utc.hour * 3600 + utc.minute * 60 + utc.second
            return (seconds // unit_seconds) % modulo
        if isinstance(value, timedelta):
            micros = value // timedelta(microseconds=1)
            whole = abs(micros) // (unit_seconds * 1_000_000)
            return -whole if micros < 0 else whole
        raise evaluation_error(f"{name}() is not defined for {type_name(value)}")

    return run


def _record(rt: Runtime, args: Sequence[object]) -> object:
    return rt.load_record(ops.ref_key(args[0], "record() argument"))


def _links(rt: Runtime, args: Sequence[object]) -> object:
    name = _text(args[1], "links() relation")
    return rt.load_links(ops.ref_key(args[0], "links() argument"), name)


# Special checks -------------------------------------------------------------------------------


def _check_record(site: CallSite) -> ExprType:
    if len(site.args) != 1:
        raise type_error("record() takes one argument", site.node.pos)
    (arg,) = site.args
    if arg.kind is K.DYN:
        return DYN
    if arg.kind is not K.REF or arg.name is None:
        raise type_error(
            "record() takes a Record reference (a ref parameter, an attribute of type ref "
            f"or $step[n]), got {site.describe()}",
            site.node.pos,
        )
    return record_of(arg.name)


def _check_links(site: CallSite) -> ExprType:
    if len(site.args) != 2:
        raise type_error("links() takes a Record reference and a Relation name", site.node.pos)
    ref, _ = site.args
    name = site.literal(1)
    if not isinstance(name, str):
        raise type_error("links() needs a Relation name as a string literal", site.node.pos)
    found = site.environment.schema.relation_by_api_name(name)
    if found is None:
        raise type_error(f"unknown Relation {name!r}", site.node.pos)
    relation, direction = found
    if ref.kind is K.REF and ref.name is not None and relation.endpoint(direction) != ref.name:
        raise type_error(
            f"Relation {name!r} does not start at Kind {site.environment.kind_name(ref.name)}",
            site.node.pos,
        )
    if ref.kind not in (K.REF, K.DYN):
        raise type_error(f"links() takes a Record reference, got {site.describe()}", site.node.pos)
    return list_of(record_of(relation.far_endpoint(direction)))


def _check_matches(site: CallSite) -> ExprType:
    check = signatures("matches", ((K.STRING, K.STRING), BOOL))
    result = check(site)
    pattern = site.literal(len(site.node.args) - 1)
    if isinstance(pattern, str):
        try:
            compile_regex(pattern)
        except ValueError as error:
            raise type_error(str(error), site.node.pos) from None
    return result


def _check_extreme(name: str) -> Check:
    def check(site: CallSite) -> ExprType:
        if len(site.args) == 1:
            (items,) = site.args
            if items.kind is K.DYN:
                return DYN
            if items.kind is not K.LIST:
                raise type_error(f"{name}() of one argument takes a list", site.node.pos)
            element = items.element
            if element.kind not in NUMERIC | ORDERED | {K.DYN}:
                raise type_error(f"{name}() needs orderable elements", site.node.pos)
            return element.plain()
        if len(site.args) != 2:
            raise type_error(f"{name}() takes a list or two values", site.node.pos)
        left, right = site.args
        if K.DYN in (left.kind, right.kind):
            return DYN
        if left.is_numeric and right.is_numeric:
            return join(left, right)
        if left.kind is right.kind and left.kind in ORDERED:
            return left.plain()
        raise type_error(f"{name}() cannot compare {site.describe()}", site.node.pos)

    return check


def _check_sum(site: CallSite) -> ExprType:
    if len(site.args) != 1:
        raise type_error("sum() takes one list", site.node.pos)
    (items,) = site.args
    if items.kind is K.DYN:
        return DYN
    if items.kind is not K.LIST or items.element.kind not in NUMERIC | {K.DYN}:
        raise type_error(f"sum() takes a list of numbers, got {site.describe()}", site.node.pos)
    return items.element


def _check_join(site: CallSite) -> ExprType:
    if len(site.args) not in (1, 2):
        raise type_error("join() takes an optional separator", site.node.pos)
    items = site.args[0]
    separator_ok = len(site.args) == 1 or site.args[1].kind in (K.STRING, K.DYN)
    if items.kind is K.LIST and items.element.kind in (K.STRING, K.DYN) and separator_ok:
        return STRING
    if items.kind is K.DYN:
        return STRING
    raise type_error(f"join() needs a list of strings, got {site.describe()}", site.node.pos)


def _check_count(site: CallSite) -> ExprType:
    if len(site.args) != 1:
        raise type_error("count() takes one list", site.node.pos)
    if site.args[0].kind not in (K.LIST, K.MAP, K.DYN):
        raise type_error(f"count() takes a list, got {site.describe()}", site.node.pos)
    return INT


_SIZED = ((K.STRING,), INT), ((K.BYTES,), INT), ((K.LIST,), INT), ((K.MAP,), INT)
_NUMBER_IN_OUT = ((K.INT,), INT), ((K.DOUBLE,), DOUBLE), ((K.DECIMAL,), DECIMAL)
_DATE_LIKE = ((K.DATE,), INT), ((K.TIMESTAMP,), INT)
_TIME_LIKE = ((K.TIMESTAMP,), INT), ((K.DURATION,), INT)


def _library() -> dict[tuple[str, bool], Function]:
    entries = [
        Function("now", False, signatures("now", ((), TIMESTAMP)), lambda rt, _: rt.now),
        Function(
            "today",
            False,
            signatures("today", ((), DATE)),
            lambda rt, _: rt.now.astimezone(UTC).date(),
        ),
        Function("record", False, _check_record, _record, records=True),
        Function("links", False, _check_links, _links, records=True),
        Function("size", False, signatures("size", *_SIZED), _size),
        Function("size", True, signatures("size", *_SIZED), _size),
        Function("count", False, _check_count, _size),
        Function(
            "int",
            False,
            signatures(
                "int",
                ((K.INT,), INT),
                ((K.DOUBLE,), INT),
                ((K.DECIMAL,), INT),
                ((K.STRING,), INT),
                ((K.TIMESTAMP,), INT),
            ),
            _to_int,
        ),
        Function(
            "double",
            False,
            signatures(
                "double",
                ((K.INT,), DOUBLE),
                ((K.DOUBLE,), DOUBLE),
                ((K.DECIMAL,), DOUBLE),
                ((K.STRING,), DOUBLE),
            ),
            _to_double,
        ),
        Function(
            "decimal",
            False,
            signatures(
                "decimal",
                ((K.INT,), DECIMAL),
                ((K.DOUBLE,), DECIMAL),
                ((K.DECIMAL,), DECIMAL),
                ((K.STRING,), DECIMAL),
            ),
            _to_decimal,
        ),
        Function(
            "string",
            False,
            signatures(
                "string",
                *[
                    ((kind,), STRING)
                    for kind in (
                        K.STRING,
                        K.BOOL,
                        K.INT,
                        K.DOUBLE,
                        K.DECIMAL,
                        K.TIMESTAMP,
                        K.DATE,
                        K.DURATION,
                        K.BYTES,
                        K.REF,
                        K.RECORD,
                    )
                ],
            ),
            _to_string,
        ),
        Function(
            "date",
            False,
            signatures("date", ((K.STRING,), DATE), ((K.TIMESTAMP,), DATE), ((K.DATE,), DATE)),
            _to_date,
        ),
        Function(
            "timestamp",
            False,
            signatures(
                "timestamp",
                ((K.STRING,), TIMESTAMP),
                ((K.DATE,), TIMESTAMP),
                ((K.TIMESTAMP,), TIMESTAMP),
            ),
            _to_timestamp,
        ),
        Function(
            "duration",
            False,
            signatures("duration", ((K.STRING,), DURATION), ((K.DURATION,), DURATION)),
            _to_duration,
        ),
        *[
            Function(unit, False, signatures(unit, ((K.INT,), DURATION)), _unit(unit))
            for unit in ("days", "hours", "minutes", "seconds")
        ],
        Function(
            "days_between",
            False,
            signatures("days_between", ((K.DATE, K.DATE), INT), ((K.TIMESTAMP, K.TIMESTAMP), INT)),
            _days_between,
        ),
        Function("abs", False, signatures("abs", *_NUMBER_IN_OUT), _abs),
        Function(
            "round",
            False,
            signatures(
                "round",
                *_NUMBER_IN_OUT,
                ((K.INT, K.INT), INT),
                ((K.DOUBLE, K.INT), DOUBLE),
                ((K.DECIMAL, K.INT), DECIMAL),
            ),
            _rounding(ROUND_HALF_UP, "round"),
        ),
        Function(
            "floor", False, signatures("floor", *_NUMBER_IN_OUT), _rounding(ROUND_FLOOR, "floor")
        ),
        Function(
            "ceil", False, signatures("ceil", *_NUMBER_IN_OUT), _rounding(ROUND_CEILING, "ceil")
        ),
        Function("min", False, _check_extreme("min"), _extreme("min", lambda c: c < 0)),
        Function("max", False, _check_extreme("max"), _extreme("max", lambda c: c > 0)),
        Function("sum", False, _check_sum, _sum),
        Function("matches", False, _check_matches, _matches),
        Function("matches", True, _check_matches, _matches),
        Function(
            "contains",
            True,
            signatures("contains", ((K.STRING, K.STRING), BOOL)),
            _string_method("contains", lambda rt, s, a: a[0] in s),
        ),
        Function(
            "startsWith",
            True,
            signatures("startsWith", ((K.STRING, K.STRING), BOOL)),
            _string_method("startsWith", lambda rt, s, a: s.startswith(a[0])),
        ),
        Function(
            "endsWith",
            True,
            signatures("endsWith", ((K.STRING, K.STRING), BOOL)),
            _string_method("endsWith", lambda rt, s, a: s.endswith(a[0])),
        ),
        Function(
            "lower",
            True,
            signatures("lower", ((K.STRING,), STRING)),
            _string_method("lower", lambda rt, s, a: s.lower()),
        ),
        Function(
            "upper",
            True,
            signatures("upper", ((K.STRING,), STRING)),
            _string_method("upper", lambda rt, s, a: s.upper()),
        ),
        Function(
            "trim",
            True,
            signatures("trim", ((K.STRING,), STRING)),
            _string_method("trim", lambda rt, s, a: s.strip()),
        ),
        Function(
            "replace",
            True,
            signatures("replace", ((K.STRING, K.STRING, K.STRING), STRING)),
            _string_method("replace", lambda rt, s, a: rt.text(s.replace(a[0], a[1]))),
        ),
        Function(
            "split",
            True,
            signatures("split", ((K.STRING, K.STRING), list_of(STRING))),
            _string_method("split", _split),
        ),
        Function(
            "substring",
            True,
            signatures(
                "substring", ((K.STRING, K.INT), STRING), ((K.STRING, K.INT, K.INT), STRING)
            ),
            _substring,
        ),
        Function(
            "indexOf",
            True,
            signatures("indexOf", ((K.STRING, K.STRING), INT)),
            _string_method("indexOf", lambda rt, s, a: s.find(a[0])),
        ),
        Function("join", True, _check_join, _join),
        Function(
            "getFullYear",
            True,
            signatures("getFullYear", *_DATE_LIKE),
            _date_part("getFullYear", lambda d: d.year),
        ),
        Function(
            "getMonth",
            True,
            signatures("getMonth", *_DATE_LIKE),
            _date_part("getMonth", lambda d: d.month - 1),
        ),
        Function(
            "getDate",
            True,
            signatures("getDate", *_DATE_LIKE),
            _date_part("getDate", lambda d: d.day),
        ),
        Function(
            "getDayOfMonth",
            True,
            signatures("getDayOfMonth", *_DATE_LIKE),
            _date_part("getDayOfMonth", lambda d: d.day - 1),
        ),
        Function(
            "getDayOfWeek",
            True,
            signatures("getDayOfWeek", *_DATE_LIKE),
            _date_part("getDayOfWeek", lambda d: (d.weekday() + 1) % 7),
        ),
        Function(
            "getDayOfYear",
            True,
            signatures("getDayOfYear", *_DATE_LIKE),
            _date_part("getDayOfYear", lambda d: d.timetuple().tm_yday - 1),
        ),
        Function(
            "getHours", True, signatures("getHours", *_TIME_LIKE), _time_part("getHours", 3600, 24)
        ),
        Function(
            "getMinutes",
            True,
            signatures("getMinutes", *_TIME_LIKE),
            _time_part("getMinutes", 60, 60),
        ),
        Function(
            "getSeconds",
            True,
            signatures("getSeconds", *_TIME_LIKE),
            _time_part("getSeconds", 1, 60),
        ),
    ]
    return {(f.name, f.member): f for f in entries}


#: Functions by ``(name, member)``.
LIBRARY: Mapping[tuple[str, bool], Function] = _library()


def lookup(name: str, member: bool) -> Function | None:
    return LIBRARY.get((name, member))


def raise_with(error: ExprError, node: Call) -> ExprError:
    """``error`` positioned at ``node`` unless it carries a position already."""
    if error.position is None:
        error.position = node.pos
    return error
