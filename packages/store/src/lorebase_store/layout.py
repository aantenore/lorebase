"""Where the materialized rows of a branch live: its own rows, or an overlay on a base branch.

A `full` branch has every row of its head in `mat_records`, `mat_links` and `mat_history`.
An `overlay` branch (design 6.11, AC-16) stores only the rows it changed, tombstones
(`deleted`) included, and reads the rows of its base branch as they were at the fork point:

    overlay rows (not deleted)
    UNION ALL
    base rows with revision_generation <= base_generation that the overlay does not shadow

A base commit that changes a row an overlay still reads copies the previous row into the
overlay first (copy on write, `MaterializedRepository`), so the base rows an overlay reads
never changed after its fork; the generation filter hides the rows the base wrote since.

This module renders that row set once, as SQL text with the Domain, branch, base branch and
generation as literals (per-Attribute partial indexes of the base match, see
`lorebase_store.indexes`), and offers it both as text (`records_from`) and as a SQLAlchemy
`FromClause` with the table's columns (`records`). Every row of the set reports the overlay
as its `branch`, so predicates written for a table (`branch = :branch`) keep working. For a
full branch both return the plain table, so its statements are unchanged.

The base half also checks that the ref is still an overlay forked at that generation: a
reader that resolved the layout before a promotion or a re-fork (READ COMMITTED) then reads
only the overlay rows, which a promotion has completed. Revision `0007` renders the same text
inside the security definer read functions (`lb_overlay_rows`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import FromClause, Table, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from lorebase_store.models import metadata
from lorebase_store.models.enums import Materialization

MAT_RECORDS: Table = metadata.tables["mat_records"]
MAT_LINKS: Table = metadata.tables["mat_links"]
MAT_HISTORY: Table = metadata.tables["mat_history"]

RECORD_COLUMNS = tuple(c.name for c in MAT_RECORDS.columns)
LINK_COLUMNS = tuple(c.name for c in MAT_LINKS.columns)
HISTORY_COLUMNS = tuple(c.name for c in MAT_HISTORY.columns)

_LAYOUT = text(
    "SELECT CAST(materialization AS text), base_branch, base_generation FROM refs "
    "WHERE domain_id = :domain AND name = :branch"
)


@dataclass(frozen=True, slots=True)
class BranchLayout:
    """The rows a branch reads: its own (`base` is None) or an overlay on `base`."""

    domain_id: str
    branch: str
    base: str | None = None
    base_generation: int | None = None

    def __post_init__(self) -> None:
        if (self.base is None) != (self.base_generation is None):
            raise ValueError("an overlay needs both its base branch and its base generation")

    @property
    def overlay(self) -> bool:
        return self.base is not None

    # SQL text -----------------------------------------------------------------------

    def records_sql(self) -> str:
        """The resolved Record rows as one statement (all `mat_records` columns)."""
        return self._resolved("mat_records", RECORD_COLUMNS, ("kind_id", "key"))

    def links_sql(self) -> str:
        """The resolved Link rows as one statement (all `mat_links` columns)."""
        return self._resolved("mat_links", LINK_COLUMNS, ("relation_id", "from_key", "to_key"))

    def history_sql(self) -> str:
        """The branch history: the overlay's own entries and the base's up to the fork."""
        own = ", ".join(f"h.{_quoted(c)}" for c in HISTORY_COLUMNS)
        base = ", ".join(
            f"CAST({_literal(self.branch)} AS text)" if c == "branch" else f"b.{_quoted(c)}"
            for c in HISTORY_COLUMNS
        )
        domain = _literal(self.domain_id)
        return (
            f"SELECT {own} FROM mat_history h WHERE h.domain_id = {domain} "
            f"AND h.branch = {_literal(self.branch)} UNION ALL SELECT {base} FROM mat_history b "
            f"WHERE b.domain_id = {domain} AND b.branch = {_literal(self._base())} "
            f"AND EXISTS (SELECT 1 FROM revisions r WHERE r.domain_id = {domain} "
            f"AND r.id = b.revision_id AND r.generation <= {self._generation()} OFFSET 0) "
            f"AND {self._guard()}"
        )

    def records_from(self, alias: str | None = None) -> str:
        """A FROM item for text statements: `mat_records [alias]` or the resolved rows."""
        return self._from_item("mat_records", alias, self.records_sql)

    def links_from(self, alias: str | None = None) -> str:
        return self._from_item("mat_links", alias, self.links_sql)

    def history_from(self, alias: str | None = None) -> str:
        return self._from_item("mat_history", alias, self.history_sql)

    # SQLAlchemy ---------------------------------------------------------------------

    def records(self, name: str | None = None) -> FromClause:
        """`mat_records` (aliased when `name` is given) or the resolved rows of an overlay."""
        return self._from_clause(MAT_RECORDS, name)

    def links(self, name: str | None = None) -> FromClause:
        return self._from_clause(MAT_LINKS, name)

    def history(self, name: str | None = None) -> FromClause:
        return self._from_clause(MAT_HISTORY, name)

    def records_alias(self) -> FromClause:
        """An anonymous alias of the rows (for self-joins)."""
        return self._resolved_clause(MAT_RECORDS) if self.overlay else MAT_RECORDS.alias()

    def links_alias(self) -> FromClause:
        return self._resolved_clause(MAT_LINKS) if self.overlay else MAT_LINKS.alias()

    # Rendering ----------------------------------------------------------------------

    def _resolved(self, table: str, columns: tuple[str, ...], key: tuple[str, ...]) -> str:
        own, base = self._halves(table, columns, key, own="o", base="s")
        return f"{own} UNION ALL {base}"

    def _halves(
        self, table: str, columns: tuple[str, ...], key: tuple[str, ...], *, own: str, base: str
    ) -> tuple[str, str]:
        """The overlay half and the base half of the resolved rows."""
        domain, branch = _literal(self.domain_id), _literal(self.branch)
        own_columns = ", ".join(f"{own}.{_quoted(c)}" for c in columns)
        base_columns = ", ".join(
            f"CAST({branch} AS text)" if c == "branch" else f"{base}.{_quoted(c)}" for c in columns
        )
        shadow = " AND ".join(f"x.{c} = {base}.{c}" for c in key)
        # Postgres does not flatten a UNION ALL whose members have WHERE clauses: the halves
        # stay subqueries; restriction quals and lateral references are pushed into each,
        # ordering and LIMIT are not (`records_arms` repeats them per half). OFFSET 0 keeps
        # the anti-join a primary-key probe per base row read.
        return (
            f"SELECT {own_columns} FROM {table} {own} WHERE {own}.domain_id = {domain} "
            f"AND {own}.branch = {branch} AND NOT {own}.deleted",
            f"SELECT {base_columns} FROM {table} {base} WHERE {base}.domain_id = {domain} "
            f"AND {base}.branch = {_literal(self._base())} "
            f"AND {base}.revision_generation <= {self._generation()} AND {self._guard()} "
            f"AND NOT EXISTS (SELECT 1 FROM {table} x WHERE x.domain_id = {domain} "
            f"AND x.branch = {branch} AND {shadow} OFFSET 0)",
        )

    def records_arms(self, tail: str, alias: str = "m") -> str:
        """A FROM item with the resolved Record rows of an overlay where `tail` is applied
        inside each half: conditions (starting with ` AND`) on alias `m`, then `ORDER BY` and
        `LIMIT`, so that a keyset page reads at most `LIMIT` rows of each half. `tail` may hold
        bind parameters and must not test `branch` (the halves read different branches)."""
        if not self.overlay:
            raise ValueError("records_arms applies to overlay branches")
        own, base = (
            escape_colons(half)
            for half in self._halves(
                "mat_records", RECORD_COLUMNS, ("kind_id", "key"), own="m", base="m"
            )
        )
        return f"(({own}{tail}) UNION ALL ({base}{tail})) AS {alias}"

    def _guard(self) -> str:
        return (
            f"EXISTS (SELECT 1 FROM refs f WHERE f.domain_id = {_literal(self.domain_id)} "
            f"AND f.name = {_literal(self.branch)} AND f.base_branch = {_literal(self._base())} "
            f"AND f.base_generation = {self._generation()} OFFSET 0)"
        )

    def _base(self) -> str:
        assert self.base is not None
        return self.base

    def _generation(self) -> int:
        assert self.base_generation is not None
        return int(self.base_generation)

    def _from_item(self, table: str, alias: str | None, resolved: Any) -> str:
        if not self.overlay:
            return table if alias is None else f"{table} {alias}"
        return f"({escape_colons(resolved())}) AS {alias or table}"

    def _from_clause(self, table: Table, name: str | None) -> FromClause:
        if not self.overlay:
            return table.alias(name) if name else table
        return self._resolved_clause(table, name or table.name)

    def _resolved_clause(self, table: Table, name: str | None = None) -> FromClause:
        sql = {
            MAT_RECORDS.name: self.records_sql,
            MAT_LINKS.name: self.links_sql,
            MAT_HISTORY.name: self.history_sql,
        }[table.name]()
        return text(escape_colons(sql)).columns(*table.columns).subquery(name)


def escape_colons(fragment: str) -> str:
    """A literal-holding SQL fragment made safe for `text()`, where `:name` binds."""
    return fragment.replace(":", r"\:")


def full_layout(domain_id: str, branch: str) -> BranchLayout:
    return BranchLayout(domain_id, branch)


def layout_from(
    domain_id: str,
    branch: str,
    materialization: Materialization | str | None,
    base: str | None,
    generation: int | None,
) -> BranchLayout:
    """The layout of a ref row's materialization columns."""
    if materialization == Materialization.OVERLAY and base is not None:
        return BranchLayout(domain_id, branch, base, generation)
    return BranchLayout(domain_id, branch)


async def load_layout(session: AsyncSession, domain_id: str, branch: str) -> BranchLayout:
    """The layout of a branch (one primary-key read); a missing ref reads as full."""
    found = (await session.execute(_LAYOUT, {"domain": domain_id, "branch": branch})).one_or_none()
    if found is None:
        return BranchLayout(domain_id, branch)
    return layout_from(domain_id, branch, found[0], found[1], found[2])


def load_layout_sync(session: Session, domain_id: str, branch: str) -> BranchLayout:
    found = session.execute(_LAYOUT, {"domain": domain_id, "branch": branch}).one_or_none()
    if found is None:
        return BranchLayout(domain_id, branch)
    return layout_from(domain_id, branch, found[0], found[1], found[2])


async def resolve_layout(
    session: AsyncSession, domain_id: str, branch: str | BranchLayout
) -> BranchLayout:
    """`branch` itself when it is a layout, else the layout read from its ref."""
    if isinstance(branch, BranchLayout):
        return branch
    return await load_layout(session, domain_id, branch)


def _literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("SQL literals cannot contain NUL")
    return "'" + value.replace("'", "''") + "'"


def _quoted(column: str) -> str:
    return f'"{column}"'
