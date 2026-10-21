"""Importing rows from a file through the `import_records` and `import_links` Operations
(`import_batch` is in `batch_import`).

Rows are coerced against the schema of the target branch, grouped in batches (one request and
one Revision each; the server commits very large requests in batches of its own) and sent in
order. Progress goes to standard error. A rejected batch stops the import: earlier batches stay
committed, the error says which rows were rejected, and re-running the same import is safe
because imports upsert.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from lorebase_cli.coercion import (
    KEY_COLUMN,
    LINK_COLUMNS,
    LINK_ENDS,
    CoercionError,
    attribute_types,
    coerce_row,
    is_missing,
)
from lorebase_cli.failures import CliError, ImportAbortedError
from lorebase_client import LorebaseClient, LorebaseError

Target = Literal["records", "links", "batch"]
LINKS_MEMBER = "links"  # the member of a Record row that holds its Links
Progress = Callable[["BatchResult"], None]


@dataclass(frozen=True)
class BatchResult:
    """The outcome of one request."""

    number: int
    first_row: int
    rows: int
    edits: int
    revisions: list[str]
    status: str


@dataclass
class ImportReport:
    """What an import did."""

    target: Target
    name: str
    domain: str
    branch: str | None
    dry_run: bool
    batches: list[BatchResult] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return sum(b.rows for b in self.batches)

    @property
    def edits(self) -> int:
        return sum(b.edits for b in self.batches)

    @property
    def revisions(self) -> list[str]:
        return [r for b in self.batches for r in b.revisions]

    def summary(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "name": self.name,
            "domain": self.domain,
            "branch": self.branch,
            "dry_run": self.dry_run,
            "batches": len(self.batches),
            "rows": self.rows,
            "edits": self.edits,
            "revisions": self.revisions,
        }


@dataclass(frozen=True)
class Prepared:
    """How rows of one target are coerced."""

    types: Mapping[str, str]
    extra_columns: tuple[str, ...]
    renames: Mapping[str, str]
    link_columns: Mapping[str, str] = field(default_factory=dict)  # Relation -> file column


def prepare(
    schema: Mapping[str, Any],
    target: Target,
    name: str,
    *,
    renames: Mapping[str, str] | None = None,
    link_columns: Mapping[str, str] | None = None,
) -> Prepared:
    """Look the Kind or Relation up in the schema document and fix its column rules.

    `link_columns` (Records only) names the file column that holds the key at the other end of
    each Relation; those cells are sent as the `links` of the row.
    """
    if target == "records":
        types = attribute_types(schema, "kinds", name)
        kind_id, kind = next((i, k) for i, k in schema["kinds"].items() if k["api_name"] == name)
        extra = () if kind.get("primary_key") else (KEY_COLUMN,)
        if link_columns:
            _check_link_relations(schema, kind_id, name, types, link_columns)
        return Prepared(types, extra, dict(renames or {}), dict(link_columns or {}))
    types = attribute_types(schema, "relations", name)
    return Prepared(types, LINK_COLUMNS, {**LINK_ENDS, **(renames or {})})


def _check_link_relations(
    schema: Mapping[str, Any],
    kind_id: str,
    kind_name: str,
    types: Mapping[str, str],
    link_columns: Mapping[str, str],
) -> None:
    """Every `--link` Relation must be a stored Relation that starts at the Kind."""
    if LINKS_MEMBER in types:
        raise CliError(
            f"{kind_name} has an Attribute named {LINKS_MEMBER!r}; --link cannot be used"
        )
    usable: set[str] = set()
    for relation in schema.get("relations", {}).values():
        if relation.get("backing", "explicit") != "explicit":
            continue
        if relation.get("from_kind") == kind_id:
            usable.add(relation["api_name"])
        if relation.get("to_kind") == kind_id and relation.get("inverse_api_name"):
            usable.add(relation["inverse_api_name"])
    for relation_name in link_columns:
        if relation_name not in usable:
            known = ", ".join(sorted(usable)) or "none"
            raise CliError(
                f"{relation_name!r} is not a stored Relation of {kind_name} (--link); "
                f"it has: {known}"
            )


def _parse_pairs(pairs: Iterable[str], option: str, shape: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        left, separator, right = pair.partition("=")
        if not separator or not left or not right:
            raise CliError(f"{option} expects {shape}, got {pair!r}")
        out[left] = right
    return out


def parse_renames(pairs: Iterable[str]) -> dict[str, str]:
    """`--map file_column=attribute` options as a dict."""
    return _parse_pairs(pairs, "--map", "column=attribute")


def parse_link_columns(pairs: Iterable[str]) -> dict[str, str]:
    """`--link relation=file_column` options as a dict."""
    return _parse_pairs(pairs, "--link", "relation=column")


def _link_targets(value: Any) -> Any:
    """What a link cell holds: a key, a list of keys or an object with `key` and Attributes."""
    if isinstance(value, list):
        return [
            item if isinstance(item, dict) else str(item) for item in value if not is_missing(item)
        ]
    return value if isinstance(value, dict) else str(value)


def _split_links(
    row: Mapping[str, Any], link_columns: Mapping[str, str]
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    """The row without its link columns, and the `links` they make."""
    if not link_columns:
        return row, {}
    rest = dict(row)
    links: dict[str, Any] = {}
    for relation, column in link_columns.items():
        value = rest.pop(column, None)
        if is_missing(value) or value == []:
            continue
        targets = _link_targets(value)
        if targets != []:
            links[relation] = targets
    return rest, links


def coerced_rows(
    rows: Iterable[Mapping[str, Any]],
    prepared: Prepared,
    *,
    ignore_unknown: bool,
    source: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Rows coerced one by one; a bad value names its row (1-based, header excluded) and, when
    several files are read, `source`."""
    where = f"{source}: " if source else ""
    for number, row in enumerate(rows, start=1):
        row, links = _split_links(row, prepared.link_columns)
        try:
            coerced = coerce_row(
                prepared.types,
                row,
                renames=prepared.renames,
                ignore_unknown=ignore_unknown,
                extra_columns=prepared.extra_columns,
            )
        except CoercionError as error:
            raise CliError(f"{where}row {number}: {error}") from None
        if links:
            coerced[LINKS_MEMBER] = links
        yield coerced


def run_import(
    client: LorebaseClient,
    *,
    domain: str,
    branch: str | None,
    target: Target,
    name: str,
    rows: Iterable[dict[str, Any]],
    batch_size: int,
    dry_run: bool = False,
    message: str | None = None,
    progress: Progress | None = None,
) -> ImportReport:
    """Send `rows` in batches of `batch_size`; returns what was done."""
    report = ImportReport(target, name, domain, branch, dry_run)
    first_row = 1
    for number, batch in enumerate(itertools.batched(rows, batch_size), start=1):
        last_row = first_row + len(batch) - 1
        note = f"{message} (rows {first_row}-{last_row})" if message else None
        key = client.idempotency_key("import")  # a retry of this request cannot apply it twice
        try:
            if target == "records":
                result = client.operations.import_records(
                    domain,
                    name,
                    batch,
                    branch=branch,
                    dry_run=dry_run,
                    message=note,
                    idempotency_key=key,
                )
            else:
                result = client.operations.import_links(
                    domain,
                    name,
                    batch,
                    branch=branch,
                    dry_run=dry_run,
                    message=note,
                    idempotency_key=key,
                )
        except LorebaseError as error:
            raise ImportAbortedError(
                error,
                first_row=first_row,
                last_row=last_row,
                committed_rows=report.rows,
                committed_revisions=report.revisions,
            ) from error
        outcome = BatchResult(
            number, first_row, len(batch), result.edit_count, result.revisions, result.status
        )
        report.batches.append(outcome)
        if progress is not None:
            progress(outcome)
        first_row = last_row + 1
    return report
