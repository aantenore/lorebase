"""Importing rows from a file through the `import_records` and `import_links` Operations.

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
)
from lorebase_cli.failures import CliError, ImportAbortedError
from lorebase_client import LorebaseClient, LorebaseError

Target = Literal["records", "links"]
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


def prepare(
    schema: Mapping[str, Any],
    target: Target,
    name: str,
    *,
    renames: Mapping[str, str] | None = None,
) -> Prepared:
    """Look the Kind or Relation up in the schema document and fix its column rules."""
    if target == "records":
        types = attribute_types(schema, "kinds", name)
        kind = next(k for k in schema["kinds"].values() if k["api_name"] == name)
        extra = () if kind.get("primary_key") else (KEY_COLUMN,)
        return Prepared(types, extra, dict(renames or {}))
    types = attribute_types(schema, "relations", name)
    return Prepared(types, LINK_COLUMNS, {**LINK_ENDS, **(renames or {})})


def parse_renames(pairs: Iterable[str]) -> dict[str, str]:
    """`--map file_column=attribute` options as a dict."""
    out: dict[str, str] = {}
    for pair in pairs:
        source, separator, target = pair.partition("=")
        if not separator or not source or not target:
            raise CliError(f"--map expects column=attribute, got {pair!r}")
        out[source] = target
    return out


def coerced_rows(
    rows: Iterable[Mapping[str, Any]], prepared: Prepared, *, ignore_unknown: bool
) -> Iterator[dict[str, Any]]:
    """Rows coerced one by one; a bad value names its row (1-based, header excluded)."""
    for number, row in enumerate(rows, start=1):
        try:
            yield coerce_row(
                prepared.types,
                row,
                renames=prepared.renames,
                ignore_unknown=ignore_unknown,
                extra_columns=prepared.extra_columns,
            )
        except CoercionError as error:
            raise CliError(f"row {number}: {error}") from None


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
