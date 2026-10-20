"""Thresholds and naming rules of schema inference, with documented defaults.

Every heuristic of ``lorebase_core.infer`` reads its threshold from ``InferenceOptions``; nothing
is hard-coded in the rules. Services expose these options as settings (see
``config/lorebase.example.yaml``, ``keeper.schema_inference.options``).

Name patterns are ``fnmatch`` globs matched against normalized ``snake_case`` names. In key
patterns ``{table}`` stands for the normalized table name and for its singular form.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from lorebase_core.model.base import ElementStatus
from lorebase_core.validate.schema_rules import DEFAULT_RESERVED_ATTRIBUTE_NAMES


class InferenceOptions(BaseModel):
    """Options of ``infer_schema``. Defaults suit small to medium operational tables."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Sampling and value reading ----------------------------------------------------------
    sample_size: int = Field(
        default=10000,
        ge=1,
        description="Values read per column; longer value iterables are cut (head sample).",
    )
    null_tokens: tuple[str, ...] = Field(
        default=("", "NULL", "null"),
        description="Text values read as null (after trimming). Exact, case-sensitive match.",
    )
    trim_text: bool = Field(default=True, description="Trim whitespace around text values.")
    max_evidence_samples: int = Field(
        default=5, ge=0, description="Sample values kept in each Evidence entry."
    )
    min_support_values: int = Field(
        default=20,
        ge=1,
        description="Non-null values needed for full confidence; fewer scale confidence down "
        "linearly (to half at zero values).",
    )
    sampled_uniqueness_factor: float = Field(
        default=0.8,
        ge=0,
        le=1,
        description="Confidence factor of uniqueness and inclusion seen on a sample only "
        "(row_count above the values read).",
    )

    # Types ---------------------------------------------------------------------------------
    true_tokens: tuple[str, ...] = Field(
        default=("true", "t", "yes", "y"), description="Boolean true, compared case-insensitively."
    )
    false_tokens: tuple[str, ...] = Field(
        default=("false", "f", "no", "n"),
        description="Boolean false, compared case-insensitively.",
    )
    type_tolerance: float = Field(
        default=0.0,
        ge=0,
        lt=1,
        description="Share of non-null values allowed not to parse as the detected type. "
        "Outliers are reported as a mixed_types issue.",
    )
    mixed_type_report_ratio: float = Field(
        default=0.5,
        gt=0,
        le=1,
        description="A text column where at least this share parses as one non-text type is "
        "reported as mixed_types.",
    )
    leading_zero_integers_as_text: bool = Field(
        default=True, description="Integers written with leading zeros (007) stay text codes."
    )
    decimal_max_scale: int = Field(
        default=4,
        ge=0,
        description="Numbers with at most this many fractional digits are decimal; more: float.",
    )
    decimal_precision_headroom: int = Field(
        default=2,
        ge=0,
        description="Integer digits added to the largest seen when proposing decimal precision.",
    )
    decimal_max_precision: int = Field(default=38, ge=1, description="Cap of decimal precision.")
    date_formats: tuple[str, ...] = Field(
        default=("%Y-%m-%d",),
        description="strptime formats of date values. Day-month ambiguity is left to the user.",
    )
    timestamp_formats: tuple[str, ...] = Field(
        default=("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"),
        description="strptime formats of timestamps without offset, read as UTC. RFC 3339 "
        "values with an offset are always recognized.",
    )
    enum_max_cardinality: int = Field(
        default=12, ge=1, description="Text columns with at most this many distinct values ..."
    )
    enum_max_distinct_ratio: float = Field(
        default=0.5, gt=0, le=1, description="... and at most this distinct/non-null ratio ..."
    )
    enum_min_values: int = Field(
        default=10, ge=1, description="... and at least this many non-null values are enum."
    )
    enum_max_value_length: int = Field(
        default=40, ge=1, description="Longer values never form an enum."
    )
    text_min_length: int = Field(
        default=256, ge=1, description="Text values longer than this make the column `text`."
    )
    text_name_hints: tuple[str, ...] = Field(
        default=(
            "description",
            "*_description",
            "summary",
            "reason",
            "notes",
            "note",
            "comment",
            "comments",
            "body",
            "content",
            "details",
        ),
        description="Column names whose prose values (see text_hint_min_words) are `text`.",
    )
    text_hint_min_words: float = Field(
        default=3.0, ge=0, description="Average words per value needed by text_name_hints."
    )
    string_length_buckets: tuple[int, ...] = Field(
        default=(16, 32, 64, 128, 256),
        description="max_length of strings is the longest value rounded up to a bucket.",
    )
    propose_max_length: bool = Field(
        default=True, description="Propose a max_length constraint on strings."
    )
    propose_patterns: bool = Field(
        default=True,
        description="Propose a pattern for code-like strings sharing one shape (S-1001, PL).",
    )
    pattern_min_values: int = Field(
        default=5, ge=1, description="Distinct values needed before a pattern is proposed."
    )
    propose_non_negative_min: bool = Field(
        default=True, description="Propose min 0 on numbers that are never negative."
    )

    # Nullability and uniqueness -----------------------------------------------------------
    required_max_null_ratio: float = Field(
        default=0.0,
        ge=0,
        lt=1,
        description="Columns with at most this share of nulls are required.",
    )
    unique_min_values: int = Field(
        default=2, ge=1, description="Non-null values needed before uniqueness counts."
    )
    unique_name_patterns: tuple[str, ...] = Field(
        default=("*_number", "*_code", "*_id", "*_key", "email", "*_email", "vat*", "iban"),
        description="Unique non-key columns flagged `unique` only when named like this.",
    )

    # Keys ----------------------------------------------------------------------------------
    key_name_patterns: tuple[str, ...] = Field(
        default=(
            "id",
            "key",
            "code",
            "{table}_id",
            "{table}_key",
            "{table}_code",
            "{table}_number",
            "{table}_no",
            "*_id",
            "*_key",
            "*_code",
            "*_number",
            "*_no",
        ),
        description="Ranked names of primary key candidates (unique, non-null columns).",
    )
    allow_unnamed_primary_key: bool = Field(
        default=True,
        description="When no candidate matches key_name_patterns, the first column may still "
        "be the key if it is unique and non-null.",
    )
    composite_key_max_columns: int = Field(
        default=2,
        ge=1,
        le=3,
        description="Largest composite key tried (key-named and foreign key-like columns only).",
    )
    minted_key_columns: tuple[str, ...] = Field(
        default=("key",),
        description="A key column with one of these names holding generated ids (ULID, UUID) "
        "carries Record keys: the Kind gets minted keys and no key Attribute.",
    )

    # Foreign keys and Relations ------------------------------------------------------------
    min_inclusion_ratio: float = Field(
        default=0.95,
        gt=0,
        le=1,
        description="Share of a column's distinct values found in another table's key for a "
        "foreign key (and a Relation) to be proposed.",
    )
    foreign_key_suffixes: tuple[str, ...] = Field(
        default=("_id", "_key", "_code", "_number", "_no", "_ref", "_fk"),
        description="Suffixes marking reference columns; stripped to name the Relation.",
    )
    integer_reference_needs_name: bool = Field(
        default=True,
        description="Integer columns reference another table only when their name points at "
        "it (small integers are included in most integer keys).",
    )
    weak_name_factor: float = Field(
        default=0.9,
        ge=0,
        le=1,
        description="Confidence factor of a reference whose name has a reference suffix but "
        "does not name the target.",
    )
    no_name_factor: float = Field(
        default=0.75,
        ge=0,
        le=1,
        description="Confidence factor of a reference whose name gives no hint at all.",
    )
    detect_junction_tables: bool = Field(
        default=True,
        description="Tables made of two foreign keys (plus link attributes) become Relations.",
    )
    junction_max_attributes: int = Field(
        default=8, ge=0, description="Link attributes a junction table may carry."
    )
    one_to_one_needs_declared_unique: bool = Field(
        default=True,
        description="Uniqueness seen in data never makes a Relation one_to_one; a declared "
        "unique constraint (or a key reference) does.",
    )

    # Names ---------------------------------------------------------------------------------
    singular_kind_names: bool = Field(
        default=True, description="Kind names are the singular of table names (suppliers)."
    )
    strip_table_prefixes: tuple[str, ...] = Field(
        default=(), description="Prefixes removed from table names, e.g. tbl_."
    )
    title_names: tuple[str, ...] = Field(
        default=("name", "title", "label", "display_name", "*_name", "*_title", "*_label"),
        description="Ranked names of the title Attribute.",
    )
    propose_search: bool = Field(
        default=True,
        description="Make the title Attribute searchable (weight A) and text Attributes "
        "searchable (weight B).",
    )
    reserved_attribute_names: tuple[str, ...] = Field(
        default=tuple(sorted(DEFAULT_RESERVED_ATTRIBUTE_NAMES)),
        description="Attribute names reserved by Lorebase; colliding columns are renamed "
        "<kind>_<column>.",
    )

    # Ids and status ------------------------------------------------------------------------
    element_status: ElementStatus = Field(
        default=ElementStatus.DRAFT,
        description="Status of proposed Kinds and Relations; draft elements need no "
        "descriptions until a reviewer activates them.",
    )
    id_seed: str = Field(
        default="lorebase.infer",
        description="Seed of the deterministic ids: the same input and seed give the same ids.",
    )
    id_timestamp_ms: int = Field(
        default=0, ge=0, lt=2**48, description="Timestamp part of the deterministic ULIDs."
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.string_length_buckets or list(self.string_length_buckets) != sorted(
            set(self.string_length_buckets)
        ):
            raise ValueError("string_length_buckets must be strictly increasing and non-empty")
        overlap = {t.lower() for t in self.true_tokens} & {t.lower() for t in self.false_tokens}
        if overlap:
            raise ValueError(f"tokens both true and false: {sorted(overlap)}")
        return self
