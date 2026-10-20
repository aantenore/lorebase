"""Primary key choice.

Candidates are required, unique columns of a keyable type (string, integer, date, uri). They are
ranked by ``key_name_patterns`` (``id``, ``key``, ``code``, ``{table}_id``... then ``*_id``...),
then by column order. Without a named candidate the first column may be the key
(``allow_unnamed_primary_key``); without any single candidate, pairs (and triples) of key-named
or reference-named columns are tried. A declared primary key always wins.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from lorebase_core.infer.columns import ColumnAnalysis, support
from lorebase_core.infer.names import rank
from lorebase_core.infer.options import InferenceOptions
from lorebase_core.types import AttributeType

#: Types a primary key candidate may have.
KEY_CANDIDATE_TYPES = frozenset(
    {AttributeType.STRING, AttributeType.INTEGER, AttributeType.DATE, AttributeType.URI}
)
#: Confidence of a key chosen without a name hint, before support and sampling factors.
UNNAMED_KEY_CONFIDENCE = 0.6
#: Confidence of a composite key, before support and sampling factors.
COMPOSITE_KEY_CONFIDENCE = 0.85


@dataclass(frozen=True)
class KeyChoice:
    """The chosen key: column indexes in key order."""

    columns: tuple[int, ...]
    confidence: float
    reasons: tuple[str, ...]
    #: Named single-column candidates, best first (more than one is an ambiguity).
    candidates: tuple[int, ...] = ()
    declared: bool = False
    #: The key column carries generated Record keys: the Kind uses minted keys.
    minted: bool = False

    @property
    def single(self) -> int | None:
        return self.columns[0] if len(self.columns) == 1 else None


def key_patterns(table_names: Sequence[str], options: InferenceOptions) -> list[str]:
    """``key_name_patterns`` with ``{table}`` expanded to each table name form, in rank order."""
    expanded: list[str] = []
    for pattern in options.key_name_patterns:
        forms = [pattern.replace("{table}", name) for name in table_names]
        for form in forms if "{table}" in pattern else [pattern]:
            if form not in expanded:
                expanded.append(form)
    return expanded


def _factor(columns: Sequence[ColumnAnalysis], sampled: bool, options: InferenceOptions) -> float:
    non_null = min((c.non_null for c in columns), default=0)
    sampling = options.sampled_uniqueness_factor if sampled else 1.0
    return support(non_null, options) * sampling


def _unique_rows(columns: Sequence[ColumnAnalysis]) -> bool:
    rows = list(zip(*(c.row_keys for c in columns), strict=False))
    if not rows or any(None in row for row in rows):
        return False
    return len(set(rows)) == len(rows)


def choose_key(
    table_names: Sequence[str],
    columns: Sequence[ColumnAnalysis],
    declared: tuple[str, ...] | None,
    options: InferenceOptions,
    *,
    sampled: bool,
    reference_like: frozenset[int] = frozenset(),
) -> KeyChoice | None:
    """The primary key of a table, or ``None`` when it has none (Records get minted keys).

    ``reference_like`` lists columns named like references (``*_id``, ``from_key``): they may
    form composite keys even when ``key_name_patterns`` does not list them.
    """
    by_name = {c.name: c for c in columns}
    if declared:
        found = [by_name[name] for name in declared if name in by_name]
        if len(found) == len(declared):
            return KeyChoice(
                columns=tuple(c.index for c in found),
                confidence=1.0,
                reasons=(f"declared primary key ({', '.join(declared)})",),
                declared=True,
            )

    patterns = key_patterns(table_names, options)
    candidates = [
        c for c in columns if c.required and c.unique and c.type.name in KEY_CANDIDATE_TYPES
    ]
    ranked: list[tuple[int, int, ColumnAnalysis]] = []
    for column in candidates:
        position = rank(column.snake, patterns)
        if position is not None:
            ranked.append((position, column.index, column))
    ranked.sort(key=lambda item: (item[0], item[1]))
    named = tuple(c.index for _, _, c in ranked)
    if ranked:
        best_rank, _, best = ranked[0]
        reasons = [
            f"{best.name!r} is unique and never null",
            f"named like a key ({patterns[best_rank]})",
        ]
        if len(ranked) > 1:
            others = ", ".join(repr(c.name) for _, _, c in ranked[1:])
            reasons.append(f"other key candidates: {others}")
        return KeyChoice(
            columns=(best.index,),
            confidence=round(_factor([best], sampled, options), 4),
            reasons=tuple(reasons),
            candidates=named,
        )
    if options.allow_unnamed_primary_key and candidates and candidates[0].index == 0:
        first = candidates[0]
        return KeyChoice(
            columns=(first.index,),
            confidence=round(UNNAMED_KEY_CONFIDENCE * _factor([first], sampled, options), 4),
            reasons=(
                f"first column {first.name!r} is unique and never null",
                "no key-like column name",
            ),
        )

    keyish = [
        c
        for c in columns
        if c.required
        and c.type.name in KEY_CANDIDATE_TYPES
        and (rank(c.snake, patterns) is not None or c.index in reference_like)
    ]
    for size in range(2, options.composite_key_max_columns + 1):
        for combo in combinations(keyish, size):
            if _unique_rows(combo):
                names = ", ".join(c.name for c in combo)
                return KeyChoice(
                    columns=tuple(c.index for c in combo),
                    confidence=round(
                        COMPOSITE_KEY_CONFIDENCE * _factor(combo, sampled, options), 4
                    ),
                    reasons=(f"({names}) is unique and never null: composite key",),
                )
    return None


def is_minted(
    choice: KeyChoice, columns: Sequence[ColumnAnalysis], options: InferenceOptions
) -> bool:
    """Whether the single key column carries generated Record keys (see ``minted_key_columns``)."""
    single = choice.single
    if single is None:
        return False
    column = columns[single]
    return column.snake in options.minted_key_columns and column.generated_ids
