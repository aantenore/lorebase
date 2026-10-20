"""RFC 8785 JSON Canonicalization Scheme (JCS) serializer.

This module implements the plain RFC 8785 rules. The Lorebase value rules
(NFC strings, omitted nulls) are applied on top of it by
:func:`lorebase_core.canonical.canonical_bytes`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal

__all__ = ["CanonicalError", "format_number", "jcs_bytes"]

# ECMAScript Number::toString switches to exponential notation outside
# 1e-7 < |x| < 1e21 (RFC 8785, section 3.2.2.3).
_MAX_PLAIN_EXPONENT = 21
_MIN_PLAIN_EXPONENT = -6

_ESCAPES = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}
_FIRST_PRINTABLE = 0x20


class CanonicalError(ValueError):
    """Raised when a value cannot be represented in canonical JSON."""


def jcs_bytes(value: object) -> bytes:
    """Serialize a JSON-compatible value to RFC 8785 canonical UTF-8 bytes.

    Accepted types: ``None``, ``bool``, ``int`` (only if its decimal text is
    the canonical text of an IEEE 754 double), ``float`` (finite), ``str``, mappings with ``str``
    keys, lists and tuples.
    """
    parts: list[str] = []
    _write(value, parts)
    try:
        return "".join(parts).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalError("string contains a lone surrogate") from exc


def format_number(value: float) -> str:
    """Format a number with the ECMAScript Number::toString rules (RFC 8785 3.2.2.3)."""
    if isinstance(value, bool):
        raise CanonicalError("booleans are not numbers")
    if isinstance(value, int):
        # RFC 8785 numbers are IEEE 754 doubles. An integer is accepted only when
        # its decimal text is already the canonical text of its double, so no
        # digit is silently changed; other integers must be passed as strings.
        try:
            text = format_number(float(value))
        except OverflowError:
            text = ""
        if text != str(value):
            raise CanonicalError(
                f"integer {value} is not exactly representable; pass it as a string"
            )
        return text
    if not math.isfinite(value):
        raise CanonicalError("NaN and Infinity are not valid JSON numbers")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr() yields the shortest digit string that round-trips, which is the
    # digit selection ECMAScript requires.
    _, digit_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    assert isinstance(exponent, int)
    digits = "".join(str(d) for d in digit_tuple).rstrip("0")
    exponent += len(digit_tuple) - len(digits)
    k = len(digits)
    n = exponent + k  # value = 0.digits * 10**n
    if k <= n <= _MAX_PLAIN_EXPONENT:
        body = digits + "0" * (n - k)
    elif 0 < n <= _MAX_PLAIN_EXPONENT:
        body = digits[:n] + "." + digits[n:]
    elif _MIN_PLAIN_EXPONENT < n <= 0:
        body = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        e_text = f"+{e}" if e >= 0 else str(e)
        mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
        body = f"{mantissa}e{e_text}"
    return sign + body


def _write(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int | float):
        out.append(format_number(value))
    elif isinstance(value, str):
        _write_string(value, out)
    elif isinstance(value, Mapping):
        _write_object(value, out)
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    else:
        raise CanonicalError(f"unsupported type for canonical JSON: {type(value).__name__}")


def _write_object(value: Mapping[object, object], out: list[str]) -> None:
    keys: list[str] = []
    for key in value:
        if not isinstance(key, str):
            raise CanonicalError(f"object keys must be strings, got {type(key).__name__}")
        keys.append(key)
    keys.sort(key=_utf16_sort_key)
    out.append("{")
    for index, key in enumerate(keys):
        if index:
            out.append(",")
        _write_string(key, out)
        out.append(":")
        _write(value[key], out)
    out.append("}")


def _utf16_sort_key(key: str) -> bytes:
    # Big-endian UTF-16 bytes compare exactly like sequences of UTF-16 code units.
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise CanonicalError("object key contains a lone surrogate") from exc


def _write_string(value: str, out: list[str]) -> None:
    out.append('"')
    for char in value:
        code = ord(char)
        escaped = _ESCAPES.get(code)
        if escaped is not None:
            out.append(escaped)
        elif code < _FIRST_PRINTABLE:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
