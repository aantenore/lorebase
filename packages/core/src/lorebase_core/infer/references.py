"""Foreign keys by value inclusion, and junction tables.

A column references a table when at least ``min_inclusion_ratio`` of its distinct values are
values of that table's single-column key, the value families match (integer to integer, text to
text) and, for integers, the name looks like a reference. Declared foreign keys need no
evidence.

Tables whose key is itself included in another table's key are *dependent* (extension or link
tables) and are never targets. When two keys hold the same values (``purchase_order.po_number``
and ``delivered_to.from_key``), the table with more columns owns them and the other depends on
it. Among several targets the best inclusion wins, then the stronger name hint, then the wider
table, then the name.

A junction table has exactly two references, both required, and its key is made of those
reference columns. It becomes a Relation between the two referenced Kinds, with the other
columns as link Attributes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

from lorebase_core.infer.columns import ColumnAnalysis, support
from lorebase_core.infer.names import strip_suffix
from lorebase_core.infer.options import InferenceOptions
from lorebase_core.infer.proposal import InferenceIssue, InferenceIssueCode
from lorebase_core.infer.tables import TableAnalysis
from lorebase_core.model.relation import Cardinality

#: Column name prefixes giving the direction of a junction table.
_FROM_PREFIXES = ("from_", "source_", "src_", "parent_")
_TO_PREFIXES = ("to_", "target_", "dst_", "child_")


class NameHint(IntEnum):
    """How strongly a column name points at a referenced table (lower is stronger)."""

    DECLARED = 0
    TARGET = 1
    SUFFIX = 2
    NONE = 3


@dataclass(frozen=True)
class Reference:
    """Column ``column`` of table ``table`` holds keys of table ``target``."""

    table: int
    column: int
    target: int
    inclusion: float
    hint: NameHint
    confidence: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Junction:
    """Table ``table`` links ``source`` (from) to ``destination`` (to) Records."""

    table: int
    source: Reference
    destination: Reference
    cardinality: Cardinality
    reasons: tuple[str, ...]


def inclusion(column: ColumnAnalysis, key: ColumnAnalysis) -> float:
    """Share of ``column``'s distinct values that are values of ``key``."""
    if not column.distinct_keys:
        return 0.0
    return len(column.distinct_keys & key.distinct_keys) / len(column.distinct_keys)


def name_hint(column: ColumnAnalysis, target: TableAnalysis, options: InferenceOptions) -> NameHint:
    """``TARGET`` when the name names the target table or its key, ``SUFFIX`` when it only
    has a reference suffix, ``NONE`` otherwise."""
    stem = strip_suffix(column.snake, options.foreign_key_suffixes)
    base = stem or column.snake
    forms = {target.snake, target.singular}
    key = target.key_column
    if (
        base in forms
        or any(base.endswith("_" + form) for form in forms)
        or (key is not None and column.snake == key.snake)
    ):
        return NameHint.TARGET
    return NameHint.SUFFIX if stem is not None else NameHint.NONE


def _factor(hint: NameHint, options: InferenceOptions) -> float:
    if hint is NameHint.SUFFIX:
        return options.weak_name_factor
    if hint is NameHint.NONE:
        return options.no_name_factor
    return 1.0


def _owner(a: TableAnalysis, b: TableAnalysis) -> TableAnalysis:
    """Of two tables whose keys hold the same values, the one owning them: not keyed by a
    directional link column (``from_key``), then the widest, then by name."""

    def rank(table: TableAnalysis) -> tuple[bool, int, str, str]:
        key = table.key_column
        directional = key is not None and key.snake.startswith(_FROM_PREFIXES + _TO_PREFIXES)
        return (directional, -len(table.columns), table.snake, table.name)

    return min((a, b), key=rank)


def dependent_tables(tables: Sequence[TableAnalysis], options: InferenceOptions) -> set[int]:
    """Indexes of tables whose single key column is included in another table's key."""
    dependent: set[int] = set()
    for i, table in enumerate(tables):
        key = table.key_column
        if key is None or key.family is None:
            continue
        for j, other in enumerate(tables):
            other_key = other.key_column
            if i == j or other_key is None or other_key.family is not key.family:
                continue
            if inclusion(key, other_key) < options.min_inclusion_ratio:
                continue
            mutual = inclusion(other_key, key) >= options.min_inclusion_ratio
            if not mutual or _owner(table, other) is other:
                dependent.add(i)
                break
    return dependent


def _declared(
    tables: Sequence[TableAnalysis], issues: list[InferenceIssue]
) -> dict[tuple[int, int], Reference]:
    by_name = {t.name: i for i, t in enumerate(tables)}
    found: dict[tuple[int, int], Reference] = {}
    for i, table in enumerate(tables):
        for fk in table.foreign_keys:
            target = by_name.get(fk.target_table)
            column = table.column(fk.columns[0]) if len(fk.columns) == 1 else None
            target_key = tables[target].key_column if target is not None else None
            if (
                target is None
                or column is None
                or target_key is None
                or fk.target_columns != (target_key.name,)
            ):
                issues.append(
                    InferenceIssue(
                        code=InferenceIssueCode.UNSUPPORTED_REFERENCE,
                        message=f"foreign key ({', '.join(fk.columns)}) -> {fk.target_table}"
                        f"({', '.join(fk.target_columns)}) is not a single-column reference to "
                        "the key of a profiled table; no Relation proposed",
                        source=table.name,
                    )
                )
                continue
            found[(i, column.index)] = Reference(
                table=i,
                column=column.index,
                target=target,
                inclusion=1.0,
                hint=NameHint.DECLARED,
                confidence=1.0,
                reasons=(f"declared foreign key to {fk.target_table}({target_key.name})",),
            )
    return found


def find_references(
    tables: Sequence[TableAnalysis], options: InferenceOptions, issues: list[InferenceIssue]
) -> list[Reference]:
    """Every reference between the tables, in table and column order."""
    dependent = dependent_tables(tables, options)
    targets = [j for j, t in enumerate(tables) if j not in dependent and t.key_column is not None]
    declared = _declared(tables, issues)
    references: list[Reference] = []
    for i, table in enumerate(tables):
        own_key = table.key_column
        for column in table.columns:
            if (i, column.index) in declared:
                references.append(declared[(i, column.index)])
                continue
            if column.family is None or not column.distinct_keys:
                continue
            is_own_key = own_key is not None and own_key.index == column.index
            if is_own_key and i not in dependent:
                continue
            candidates: list[tuple[float, NameHint, TableAnalysis, int]] = []
            for j in targets:
                target = tables[j]
                key = target.key_column
                assert key is not None
                if (j == i and is_own_key) or key.family is not column.family:
                    continue
                hint = name_hint(column, target, options)
                if (
                    column.family.value == "integer"
                    and options.integer_reference_needs_name
                    and hint is NameHint.NONE
                ):
                    continue
                share = inclusion(column, key)
                if share >= options.min_inclusion_ratio:
                    candidates.append((share, hint, target, j))
            if not candidates:
                continue
            candidates.sort(key=lambda c: (-c[0], c[1], -len(c[2].columns), c[2].snake, c[2].name))
            share, hint, target, j = candidates[0]
            if len(candidates) > 1 and candidates[1][:2] == (share, hint):
                issues.append(
                    InferenceIssue(
                        code=InferenceIssueCode.AMBIGUOUS_REFERENCE,
                        message=f"{table.name}.{column.name} matches the keys of "
                        + ", ".join(c[2].name for c in candidates if c[:2] == (share, hint))
                        + f"; {target.name} was chosen",
                        source=f"{table.name}.{column.name}",
                    )
                )
            sampling = options.sampled_uniqueness_factor if table.sampled or target.sampled else 1.0
            confidence = share * _factor(hint, options) * support(column.non_null, options)
            key = target.key_column
            assert key is not None
            reasons = [
                f"{share:.0%} of {len(column.distinct_keys)} distinct values are keys of "
                f"{target.name}({key.name})",
                {
                    NameHint.TARGET: "the column name points at the target",
                    NameHint.SUFFIX: "the column name has a reference suffix",
                    NameHint.NONE: "no name hint",
                    NameHint.DECLARED: "declared",
                }[hint],
            ]
            references.append(
                Reference(
                    table=i,
                    column=column.index,
                    target=j,
                    inclusion=share,
                    hint=hint,
                    confidence=round(confidence * sampling, 4),
                    reasons=tuple(reasons),
                )
            )
    return references


def _direction(
    table: TableAnalysis, first: Reference, second: Reference
) -> tuple[Reference, Reference, str]:
    key = table.key.columns if table.key is not None else ()
    if len(key) == 1:
        if key[0] == second.column:
            return second, first, "the key column is the from side"
        return first, second, "the key column is the from side"
    for ref, other in ((first, second), (second, first)):
        name = table.columns[ref.column].snake
        other_name = table.columns[other.column].snake
        if name.startswith(_FROM_PREFIXES) or other_name.startswith(_TO_PREFIXES):
            return ref, other, "column names give the direction"
    return first, second, "column order gives the direction"


def find_junctions(
    tables: Sequence[TableAnalysis],
    references: Sequence[Reference],
    options: InferenceOptions,
) -> dict[int, Junction]:
    """Junction tables by table index."""
    if not options.detect_junction_tables:
        return {}
    targeted = {r.target for r in references}
    by_table: dict[int, list[Reference]] = {}
    for reference in references:
        by_table.setdefault(reference.table, []).append(reference)
    junctions: dict[int, Junction] = {}
    for i, refs in by_table.items():
        table = tables[i]
        if len(refs) != 2 or i in targeted or table.key is None or table.key.minted:
            continue
        columns = {r.column for r in refs}
        if not set(table.key.columns) <= columns:
            continue
        if not all(table.columns[c].required for c in columns):
            continue
        if len(table.columns) - 2 > options.junction_max_attributes:
            continue
        source, destination, why = _direction(table, refs[0], refs[1])
        source_column = table.columns[source.column]
        destination_column = table.columns[destination.column]
        from_single = len(table.key.columns) == 1
        to_single = destination_column.declared_unique or (
            not options.one_to_one_needs_declared_unique and destination_column.unique
        )
        cardinality = {
            (True, True): Cardinality.ONE_TO_ONE,
            (True, False): Cardinality.MANY_TO_ONE,
            (False, True): Cardinality.ONE_TO_MANY,
            (False, False): Cardinality.MANY_TO_MANY,
        }[(from_single, to_single)]
        reasons = (
            f"two references ({source_column.name} -> {tables[source.target].name}, "
            f"{destination_column.name} -> {tables[destination.target].name}) form the key",
            why,
            f"{cardinality.value}: "
            + (
                f"{source_column.name!r} is unique"
                if from_single
                else "the pair of references is the key"
            ),
        )
        junctions[i] = Junction(i, source, destination, cardinality, reasons)
    return junctions
