"""Type system, cast rules and canonical value encoding (design 5.4 and 6.1)."""

from lorebase_core.types.casts import CAST_MATRIX, ELEMENT_RULE, CastRule, cast, cast_rule
from lorebase_core.types.constraints import (
    ConstraintName,
    Constraints,
    ConstraintViolation,
    check_constraints,
    compile_pattern,
    constraint_problems,
)
from lorebase_core.types.encoding import (
    decode_value,
    encode_value,
    format_date,
    format_duration,
    format_timestamp,
    is_canonical,
    normalize_key,
    normalize_text,
    to_text,
)
from lorebase_core.types.errors import CastError, EncodingError, EncodingReason, TypeSystemError
from lorebase_core.types.types import (
    INT64_MAX,
    INT64_MIN,
    ORDERED_TYPES,
    TEXTUAL_TYPES,
    AttributeType,
    CanonicalValue,
    GeoPoint,
    TypeSpec,
)

__all__ = [
    "CAST_MATRIX",
    "ELEMENT_RULE",
    "INT64_MAX",
    "INT64_MIN",
    "ORDERED_TYPES",
    "TEXTUAL_TYPES",
    "AttributeType",
    "CanonicalValue",
    "CastError",
    "CastRule",
    "ConstraintName",
    "ConstraintViolation",
    "Constraints",
    "EncodingError",
    "EncodingReason",
    "GeoPoint",
    "TypeSpec",
    "TypeSystemError",
    "cast",
    "cast_rule",
    "check_constraints",
    "compile_pattern",
    "constraint_problems",
    "decode_value",
    "encode_value",
    "format_date",
    "format_duration",
    "format_timestamp",
    "is_canonical",
    "normalize_key",
    "normalize_text",
    "to_text",
]
