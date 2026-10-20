"""Operator semantics on runtime values: equality, ordering, arithmetic.

CEL rules with the Lorebase additions: ``decimal`` arithmetic is exact (with ``int``
operands promoted), numbers of different kinds compare by value, ``date`` takes whole-day
durations, and integer results must stay in the int64 range. ``int`` and ``double`` do not
mix in arithmetic (convert with ``double()`` or ``decimal()``). Division by zero, overflow
and arithmetic on an absent value (null) are evaluation errors.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Context, Decimal, DecimalException
from typing import TypeGuard

from lorebase_core.expr.errors import ExprError, evaluation_error
from lorebase_core.expr.values import ObjectValue, RecordValue, Ref, type_name
from lorebase_core.types import INT64_MAX, INT64_MIN, GeoPoint

_DAY = timedelta(days=1)


Number = int | float | Decimal


def is_number(value: object) -> TypeGuard[Number]:
    return isinstance(value, int | float | Decimal) and not isinstance(value, bool)


def check_int(value: int) -> int:
    if not INT64_MIN <= value <= INT64_MAX:
        raise evaluation_error("integer overflow: the result is outside the int64 range")
    return value


def require_bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise evaluation_error(f"{what} must be a bool, got {type_name(value)}")
    return value


def _absent(operation: str) -> ExprError:
    return evaluation_error(
        f"{operation} on an absent value (null); test presence with has() first"
    )


def equals(left: object, right: object) -> bool:
    """CEL equality: values of different types are not equal, numbers compare by value."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if is_number(left) and is_number(right):
        return bool(left == right)
    if isinstance(left, datetime) or isinstance(right, datetime):
        return isinstance(left, datetime) and isinstance(right, datetime) and left == right
    if isinstance(left, RecordValue) and isinstance(right, RecordValue):
        return left.kind.id == right.kind.id and left.key == right.key
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            equals(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        if len(left) != len(right):
            return False
        for key, value in left.items():
            match = [k for k in right if equals(k, key)]
            if not match or not equals(value, right[match[0]]):
                return False
        return True
    if isinstance(left, ObjectValue) and isinstance(right, ObjectValue):
        return left.name == right.name and equals(dict(left.fields), dict(right.fields))
    if type(left) is not type(right) and not (
        isinstance(left, GeoPoint) and isinstance(right, GeoPoint)
    ):
        return False
    return bool(left == right)


def contains(container: object, item: object) -> bool:
    """``item in container`` for lists (elements) and maps (keys)."""
    if isinstance(container, list):
        return any(equals(item, element) for element in container)
    if isinstance(container, dict):
        return any(equals(item, key) for key in container)
    if container is None:
        raise _absent("'in'")
    raise evaluation_error(f"'in' needs a list or a map, got {type_name(container)}")


def compare(left: object, right: object) -> int:
    """-1, 0 or 1; numbers of any kind compare by value, other values within their type."""
    if left is None or right is None:
        raise _absent("comparison")
    if is_number(left) and is_number(right):
        return int(left > right) - int(left < right)
    same = type(left) is type(right) or (
        isinstance(left, date)
        and isinstance(right, date)
        and isinstance(left, datetime) == isinstance(right, datetime)
    )
    if same and isinstance(left, str | bytes | bool | date | timedelta):
        return (left > right) - (left < right)  # type: ignore[operator]
    raise evaluation_error(f"cannot order {type_name(left)} and {type_name(right)}")


def negate(value: object, decimals: Context) -> object:
    if value is None:
        raise _absent("negation")
    if isinstance(value, bool):
        raise evaluation_error("cannot negate a bool; use !")
    if isinstance(value, int):
        return check_int(-value)
    if isinstance(value, float):
        return -value
    if isinstance(value, Decimal):
        return decimals.minus(value)
    if isinstance(value, timedelta):
        return -value
    raise evaluation_error(f"cannot negate {type_name(value)}")


def arithmetic(op: str, left: object, right: object, decimals: Context) -> object:
    """``+ - * / %`` on runtime values (see the module docstring)."""
    if left is None or right is None:
        raise _absent(f"operator {op}")
    try:
        result = _arithmetic(op, left, right, decimals)
    except (OverflowError, DecimalException) as error:
        raise evaluation_error(f"operator {op} failed: {_reason(error)}") from None
    if result is NotImplemented:
        raise evaluation_error(
            f"operator {op} is not defined for {type_name(left)} and {type_name(right)}"
        )
    return result


def _reason(error: Exception) -> str:
    if isinstance(error, DecimalException):
        return error.__class__.__name__.lower()
    return str(error) or error.__class__.__name__


def _arithmetic(op: str, left: object, right: object, decimals: Context) -> object:
    if isinstance(left, bool) or isinstance(right, bool):
        return NotImplemented
    if is_number(left) and is_number(right):
        return _numeric(op, left, right, decimals)
    if op == "+":
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        if isinstance(left, bytes) and isinstance(right, bytes):
            return left + right
        if isinstance(left, list) and isinstance(right, list):
            return left + right
    return _temporal(op, left, right)


def _numeric(op: str, left: object, right: object, decimals: Context) -> object:
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        if isinstance(left, float) or isinstance(right, float):
            raise evaluation_error("decimal and double do not mix; convert with decimal()")
        assert is_number(left)
        assert is_number(right)
        a = left if isinstance(left, Decimal) else Decimal(left)
        b = right if isinstance(right, Decimal) else Decimal(right)
        if op in ("/", "%") and b.is_zero():
            raise evaluation_error("division by zero")
        operations = {
            "+": decimals.add,
            "-": decimals.subtract,
            "*": decimals.multiply,
            "/": decimals.divide,
            "%": decimals.remainder,
        }
        return operations[op](a, b)
    if isinstance(left, float) or isinstance(right, float):
        if not (isinstance(left, float) and isinstance(right, float)):
            raise evaluation_error("int and double do not mix; convert with double() or int()")
        if op == "%":
            return NotImplemented
        if op == "/" and right == 0.0:
            raise evaluation_error("division by zero")
        value = {"+": left + right, "-": left - right, "*": left * right}.get(op)
        result = left / right if value is None else value
        if not math.isfinite(result):
            raise evaluation_error(f"operator {op} overflowed the double range")
        return result
    assert isinstance(left, int)
    assert isinstance(right, int)
    if op in ("/", "%"):
        if right == 0:
            raise evaluation_error("division by zero")
        quotient = abs(left) // abs(right)
        if (left < 0) != (right < 0):
            quotient = -quotient
        return check_int(quotient if op == "/" else left - right * quotient)
    return check_int({"+": left + right, "-": left - right, "*": left * right}[op])


def _temporal(op: str, left: object, right: object) -> object:
    if op == "+":
        if isinstance(left, timedelta) and isinstance(right, date | timedelta):
            left, right = right, left
        if isinstance(left, datetime) and isinstance(right, timedelta):
            return left + right
        if isinstance(left, date) and isinstance(right, timedelta):
            return left + _whole_days(right)
        if isinstance(left, timedelta) and isinstance(right, timedelta):
            return left + right
        return NotImplemented
    if op == "-":
        if isinstance(left, datetime) and isinstance(right, datetime):
            return left - right
        if isinstance(left, datetime) and isinstance(right, timedelta):
            return left - right
        if isinstance(left, datetime) or isinstance(right, datetime):
            return NotImplemented
        if isinstance(left, date) and isinstance(right, date):
            return left - right
        if isinstance(left, date) and isinstance(right, timedelta):
            return left - _whole_days(right)
        if isinstance(left, timedelta) and isinstance(right, timedelta):
            return left - right
    return NotImplemented


def _whole_days(duration: timedelta) -> timedelta:
    if duration % _DAY:
        raise evaluation_error("only whole days can be added to or subtracted from a date")
    return duration


def ref_key(value: object, what: str) -> Ref:
    if isinstance(value, Ref):
        return value
    if isinstance(value, RecordValue):
        return value.ref
    if value is None:
        raise evaluation_error(f"{what} is null")
    raise evaluation_error(f"{what} must be a Record reference, got {type_name(value)}")
