"""Per-table analysis: normalized names, column analyses and the key choice."""

from __future__ import annotations

from dataclasses import dataclass

from lorebase_core.infer.columns import ColumnAnalysis, analyze_column
from lorebase_core.infer.keys import KeyChoice, choose_key, is_minted
from lorebase_core.infer.names import normalize, singular, strip_prefixes, strip_suffix
from lorebase_core.infer.options import InferenceOptions
from lorebase_core.infer.profile import DeclaredForeignKey, TableProfile


@dataclass(frozen=True)
class TableAnalysis:
    """What inference knows about one table."""

    name: str
    snake: str
    singular: str
    columns: tuple[ColumnAnalysis, ...]
    rows: int
    values_read: int
    key: KeyChoice | None
    foreign_keys: tuple[DeclaredForeignKey, ...]
    comment: str | None

    @property
    def sampled(self) -> bool:
        return self.rows > self.values_read

    @property
    def key_column(self) -> ColumnAnalysis | None:
        """The single key column (also for minted keys), or ``None``."""
        if self.key is None or self.key.single is None:
            return None
        return self.columns[self.key.single]

    def column(self, name: str) -> ColumnAnalysis | None:
        return next((c for c in self.columns if c.name == name), None)


def analyze_table(profile: TableProfile, options: InferenceOptions) -> TableAnalysis:
    """Analyze every column of ``profile`` and choose its key."""
    snake = strip_prefixes(normalize(profile.name), options.strip_table_prefixes)
    single = singular(snake) if options.singular_kind_names else snake
    columns = tuple(
        analyze_column(column, index, options) for index, column in enumerate(profile.columns)
    )
    values_read = max((c.count for c in columns), default=0)
    rows = max(profile.row_count or 0, values_read)
    sampled = rows > values_read
    reference_like = frozenset(
        c.index for c in columns if strip_suffix(c.snake, options.foreign_key_suffixes) is not None
    )
    names = list(dict.fromkeys((snake, single)))
    key = choose_key(
        names, columns, profile.primary_key, options, sampled=sampled, reference_like=reference_like
    )
    if key is not None and not key.declared and is_minted(key, columns, options):
        key = KeyChoice(
            columns=key.columns,
            confidence=key.confidence,
            reasons=(
                *key.reasons,
                f"{columns[key.columns[0]].name!r} holds generated ids: Records use minted keys",
            ),
            candidates=key.candidates,
            minted=True,
        )
    return TableAnalysis(
        name=profile.name,
        snake=snake,
        singular=single,
        columns=columns,
        rows=rows,
        values_read=values_read,
        key=key,
        foreign_keys=profile.foreign_keys,
        comment=profile.comment,
    )
