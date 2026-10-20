"""Coercion of file values to what the import Operations take, driven by the schema.

CSV gives text; JSON Lines and Parquet give typed values. Keeper takes canonical forms:
booleans are JSON booleans, integers and decimals are strings (never floats, so no digit is
lost), floats are numbers, timestamps and dates are text, `json` values are JSON. Empty cells,
nulls and NaN are left out of the row (the Attribute is unset).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from lorebase_cli.failures import CliError

TRUE_WORDS = frozenset({"true", "t", "yes", "y", "1"})
FALSE_WORDS = frozenset({"false", "f", "no", "n", "0"})
INTEGER = re.compile(r"[+-]?[0-9]+")
KEY_COLUMN = "key"
LINK_ENDS = {"from_key": "from", "to_key": "to"}
LINK_COLUMNS = ("from", "to")


class CoercionError(ValueError):
    """A value that cannot take the type of its Attribute."""

    def __init__(self, column: str, message: str) -> None:
        super().__init__(f"column {column!r}: {message}")
        self.column = column


def is_missing(value: Any) -> bool:
    """Empty text, null and NaN all mean `unset`."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return isinstance(value, float) and math.isnan(value)


def _boolean(column: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    word = str(value).strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise CoercionError(column, f"{value!r} is not a boolean (true or false)")


def _integer(column: str, value: Any) -> str:
    if isinstance(value, bool):
        raise CoercionError(column, "a boolean is not an integer")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float | Decimal) and value == int(value):
        return str(int(value))
    text = str(value).strip()
    if not INTEGER.fullmatch(text):
        raise CoercionError(column, f"{value!r} is not an integer")
    return text


def _decimal(column: str, value: Any) -> str:
    if isinstance(value, bool):
        raise CoercionError(column, "a boolean is not a decimal")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except InvalidOperation:
        raise CoercionError(column, f"{value!r} is not a decimal number") from None
    if not number.is_finite():
        raise CoercionError(column, f"{value!r} is not a finite number")
    return format(number, "f")


def _float(column: str, value: Any) -> float:
    if isinstance(value, bool):
        raise CoercionError(column, "a boolean is not a float")
    try:
        number = float(value if not isinstance(value, str) else value.strip())
    except ValueError:
        raise CoercionError(column, f"{value!r} is not a number") from None
    if not math.isfinite(number):
        raise CoercionError(column, f"{value!r} is not a finite number")
    return number


def _json(column: str, value: Any, *, expect: type | None = None) -> Any:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            raise CoercionError(column, "is not valid JSON") from None
    if expect is not None and not isinstance(parsed, expect):
        raise CoercionError(column, f"must be a JSON {expect.__name__}")
    return parsed


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip()


def coerce_value(type_name: str, column: str, value: Any) -> Any:
    """`value` in the form Keeper takes for an Attribute of `type_name`."""
    if type_name == "boolean":
        return _boolean(column, value)
    if type_name == "integer":
        return _integer(column, value)
    if type_name == "decimal":
        return _decimal(column, value)
    if type_name == "float":
        return _float(column, value)
    if type_name == "json":
        return _json(column, value)
    if type_name == "geo_point":
        return _json(column, value, expect=dict)
    if type_name == "array":
        return _json(column, value, expect=list)
    if type_name in ("timestamp", "date"):
        if isinstance(value, date) and not isinstance(value, datetime):
            return value.isoformat()
        return _timestamp(value)
    return value if isinstance(value, str) else str(value)


def attribute_types(schema: Mapping[str, Any], section: str, name: str) -> dict[str, str]:
    """Attribute api name to type name for a Kind (`kinds`) or Relation (`relations`)."""
    elements: Mapping[str, Any] = schema.get(section, {})
    for element in elements.values():
        if element.get("api_name") == name:
            return {
                a["api_name"]: str(a["type"]["name"])
                for a in element.get("attributes", {}).values()
            }
    known = ", ".join(sorted(e.get("api_name", "?") for e in elements.values())) or "none"
    what = "Kind" if section == "kinds" else "Relation"
    raise CliError(f"unknown {what} {name!r}; the schema has: {known}")


def coerce_row(
    types: Mapping[str, str],
    row: Mapping[str, Any],
    *,
    renames: Mapping[str, str] | None = None,
    ignore_unknown: bool = False,
    extra_columns: Iterable[str] = (),
) -> dict[str, Any]:
    """One import row: columns renamed, unknown ones refused (or dropped), values coerced.

    `extra_columns` are accepted besides Attributes (`key` for Kinds without a natural key,
    `from` and `to` for Links); their values pass as text.
    """
    allowed = set(extra_columns)
    out: dict[str, Any] = {}
    for original, value in row.items():
        column = (renames or {}).get(original, original)
        if is_missing(value):
            continue
        if column in types:
            out[column] = coerce_value(types[column], column, value)
        elif column in allowed:
            out[column] = value if isinstance(value, str) else str(value)
        elif not ignore_unknown:
            raise CoercionError(column, "is not an Attribute (use --ignore-unknown to skip it)")
    return out
