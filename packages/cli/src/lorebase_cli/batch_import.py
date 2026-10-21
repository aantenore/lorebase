"""Importing Records together with their Links through the `import_batch` Operation.

Files of Records (one per Kind) and files of Links (one per Relation) are cut into requests
that keep a Record with its Links: a Link travels with the Record at its `from` end (or, when
that Record is not in the import, at its `to` end), so a Relation that is `required_from` is
satisfied in the Revision that creates the Record. Units are sent in this order: Records that
own no Link (in the order of the files given), then Records with their Links, then Links whose
ends are not part of the import. A unit is never split; a request is full when the next unit
would pass `batch_size` rows.

Every file is read twice: once to coerce and check all rows before anything is written (this
also finds the keys of the Records), once to send. The Links are held in memory, since they can
be owned by Records of any file.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from lorebase_cli import sources
from lorebase_cli.coercion import KEY_COLUMN
from lorebase_cli.failures import CliError, ImportAbortedError
from lorebase_cli.importing import (
    BatchResult,
    ImportReport,
    Prepared,
    Progress,
    coerced_rows,
    prepare,
)
from lorebase_client import LorebaseClient, LorebaseError

KEY_TYPES = frozenset({"string", "text", "uri", "ref", "enum", "integer"})
Identity = tuple[str, str]  # Kind api name and Record key


@dataclass(frozen=True)
class Source:
    """A file of Records of a Kind, or of Links of a Relation."""

    name: str
    path: str


def parse_sources(pairs: Iterable[str], option: str, shape: str) -> list[Source]:
    """`--records Kind=file` or `--links relation=file` options."""
    out: list[Source] = []
    for pair in pairs:
        name, separator, path = pair.partition("=")
        if not separator or not name or not path:
            raise CliError(f"{option} expects {shape}, got {pair!r}")
        if path == sources.STDIN:
            raise CliError(f"{option}: import batch reads files, not standard input")
        out.append(Source(name, path))
    return out


@dataclass(frozen=True)
class Item:
    """A row ready to send and where it came from (1-based, header excluded)."""

    name: str  # Kind or Relation api name
    row: dict[str, Any]
    path: str
    number: int


@dataclass
class Unit:
    """A Record with the Links it owns, or one Link on its own."""

    record: Item | None = None
    links: list[Item] = field(default_factory=list)

    @property
    def size(self) -> int:
        return (self.record is not None) + len(self.links)


@dataclass(frozen=True)
class RequestRows:
    """The rows of one request."""

    records: list[Item]
    links: list[Item]

    @property
    def size(self) -> int:
        return len(self.records) + len(self.links)

    def parameters(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return (
            [{"kind": i.name, **i.row} for i in self.records],
            [{"relation": i.name, **i.row} for i in self.links],
        )

    def locate(self, path: Sequence[Any]) -> str | None:
        """`file row N rest` for an issue path under `parameters.records` or `.links`."""
        if len(path) < 3 or path[0] != "parameters" or not isinstance(path[2], int):
            return None
        items = {"records": self.records, "links": self.links}.get(str(path[1]))
        if items is None or not 0 <= path[2] < len(items):
            return None
        item = items[path[2]]
        rest = ".".join(str(part) for part in path[3:])
        return f"{item.path} row {item.number}" + (f" {rest}" if rest else "")


def pack(units: Iterable[Unit], size: int) -> Iterator[RequestRows]:
    """Units in requests of at most `size` rows (a larger unit is a request of its own)."""
    records: list[Item] = []
    links: list[Item] = []
    for unit in units:
        if (records or links) and len(records) + len(links) + unit.size > size:
            yield RequestRows(records, links)
            records, links = [], []
        if unit.record is not None:
            records.append(unit.record)
        links.extend(unit.links)
    if records or links:
        yield RequestRows(records, links)


class BatchImport:
    """The files of one `import batch`, checked and ready to be read again for sending."""

    def __init__(
        self,
        schema: Mapping[str, Any],
        records: Sequence[Source],
        links: Sequence[Source],
        *,
        fmt: str | None,
        delimiter: str | None,
        renames: Mapping[str, str],
        ignore_unknown: bool,
    ) -> None:
        self.records = list(records)
        self.fmt = fmt
        self.delimiter = delimiter
        self.ignore_unknown = ignore_unknown
        self.prepared = {
            s.name: prepare(schema, "records", s.name, renames=renames) for s in records
        }
        self.key_attributes = {s.name: _key_attribute(schema, s.name) for s in records}
        self.record_count = 0
        self.identities: dict[Identity, None] = {}  # in file order
        self.owned: dict[Identity, list[Item]] = {}
        self.loose: list[Item] = []
        self._load_links(schema, links, renames)
        self._scan_records()
        self._assign_links()

    # Reading --------------------------------------------------------------------------------

    def _rows(self, source: Source, prepared: Prepared) -> Iterator[Item]:
        fmt = sources.detect_format(source.path, self.fmt)
        rows = coerced_rows(
            sources.read_rows(source.path, fmt, delimiter=self.delimiter),
            prepared,
            ignore_unknown=self.ignore_unknown,
            source=source.path,
        )
        for number, row in enumerate(rows, start=1):
            yield Item(source.name, row, source.path, number)

    def _load_links(
        self, schema: Mapping[str, Any], links: Sequence[Source], renames: Mapping[str, str]
    ) -> None:
        self._links: list[tuple[Item, Identity, Identity]] = []
        for source in links:
            prepared = prepare(schema, "links", source.name, renames=renames)
            from_kind, to_kind = _relation_kinds(schema, source.name)
            for item in self._rows(source, prepared):
                ends = (item.row.get("from"), item.row.get("to"))
                if not all(isinstance(end, str) for end in ends):
                    raise CliError(
                        f"{item.path}: row {item.number}: a Link needs `from` and `to` "
                        "(columns from_key and to_key)"
                    )
                a, b = (_normalized(str(end)) for end in ends)
                self._links.append((item, (from_kind, a), (to_kind, b)))

    def _scan_records(self) -> None:
        for source in self.records:
            for item in self._rows(source, self.prepared[source.name]):
                self.record_count += 1
                identity = self._identity(item)
                if identity is not None:
                    self.identities.setdefault(identity)

    def _assign_links(self) -> None:
        for item, start, end in self._links:
            owner = start if start in self.identities else end if end in self.identities else None
            if owner is None:
                self.loose.append(item)
            else:
                self.owned.setdefault(owner, []).append(item)
        del self._links

    def _identity(self, item: Item) -> Identity | None:
        attribute = self.key_attributes[item.name]
        value = item.row.get(attribute or KEY_COLUMN)
        return None if value is None else (item.name, _normalized(str(value)))

    # Units ----------------------------------------------------------------------------------

    def units(self) -> Iterator[Unit]:
        """Records without Links, Records with their Links, then the Links left over."""
        owned = {identity: list(links) for identity, links in self.owned.items()}
        with_links: list[Unit] = []
        for source in self.records:
            for item in self._rows(source, self.prepared[source.name]):
                identity = self._identity(item)
                links = owned.pop(identity, []) if identity is not None else []
                if links:
                    with_links.append(Unit(item, links))
                else:
                    yield Unit(item)
        yield from with_links
        for link in self.loose:
            yield Unit(links=[link])

    def sizes(self) -> list[int]:
        """The sizes of the units in the order `units()` yields them."""
        with_links = [1 + len(self.owned[i]) for i in self.identities if i in self.owned]
        lone = self.record_count - len(with_links)
        return [1] * lone + with_links + [1] * len(self.loose)

    @property
    def link_count(self) -> int:
        return sum(len(v) for v in self.owned.values()) + len(self.loose)

    def expected_requests(self, size: int) -> int:
        """How many requests `pack` will make (exact unless Records repeat)."""
        requests = used = 0
        for unit_size in self.sizes():
            if used and used + unit_size > size:
                requests += 1
                used = 0
            used += unit_size
        return requests + bool(used)


def _normalized(key: str) -> str:
    return unicodedata.normalize("NFC", key)


def _kind_entry(schema: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return next(k for k in schema["kinds"].values() if k["api_name"] == name)


def _key_attribute(schema: Mapping[str, Any], kind_name: str) -> str | None:
    """The api name of the one Attribute that is the key of a Kind (`None`: a `key` column)."""
    kind = _kind_entry(schema, kind_name)
    key = kind.get("primary_key") or []
    if not key:
        return None
    if len(key) > 1:
        raise CliError(
            f"{kind_name} has a composite key; import batch cannot tell which Links go with "
            "which Record. Use `import records` with --link instead"
        )
    attribute = kind["attributes"][key[0]]
    if attribute["type"]["name"] not in KEY_TYPES:
        raise CliError(
            f"the key of {kind_name} is a {attribute['type']['name']}; import batch needs a "
            "text or integer key. Use `import records` with --link instead"
        )
    return str(attribute["api_name"])


def _relation_kinds(schema: Mapping[str, Any], name: str) -> tuple[str, str]:
    """The api names of the Kinds at the `from` and `to` ends of a Relation."""
    relation = next(r for r in schema["relations"].values() if r["api_name"] == name)
    kinds = schema["kinds"]
    return kinds[relation["from_kind"]]["api_name"], kinds[relation["to_kind"]]["api_name"]


def run_batch_import(
    client: LorebaseClient,
    plan: BatchImport,
    *,
    domain: str,
    branch: str | None,
    name: str,
    batch_size: int,
    dry_run: bool = False,
    message: str | None = None,
    progress: Progress | None = None,
) -> ImportReport:
    """Send the units of `plan` in requests of at most `batch_size` rows."""
    report = ImportReport("batch", name, domain, branch, dry_run)
    first_row = 1
    for number, request in enumerate(pack(plan.units(), batch_size), start=1):
        last_row = first_row + request.size - 1
        note = f"{message} (rows {first_row}-{last_row})" if message else None
        records, links = request.parameters()
        key = client.idempotency_key("import")  # a retry of this request cannot apply it twice
        try:
            result = client.operations.import_batch(
                domain,
                records,
                links,
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
                scope=f"request {number} ({len(records)} Records, {len(links)} Links) was rejected",
                locate=request.locate,
            ) from error
        outcome = BatchResult(
            number, first_row, request.size, result.edit_count, result.revisions, result.status
        )
        report.batches.append(outcome)
        if progress is not None:
            progress(outcome)
        first_row = last_row + 1
    return report
