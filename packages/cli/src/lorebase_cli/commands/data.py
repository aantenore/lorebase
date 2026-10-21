"""`import`, `record`, `query` and `search`: loading and reading Records and Links."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from lorebase_cli import batch_import, importing, sources
from lorebase_cli.context import AppContext, app_of
from lorebase_cli.failures import CliError
from lorebase_cli.options import AllOpt, AtOpt, BranchOpt, DomainOpt, JsonOpt, LimitOpt
from lorebase_cli.output import Column, short_id
from lorebase_client import models as m

import_app = typer.Typer(
    help="Load Records and Links from CSV, JSON Lines or Parquet files.", no_args_is_help=True
)
record_app = typer.Typer(help="Read Records.", no_args_is_help=True)

FileArg = Annotated[
    str, typer.Argument(help="Input file; `-` reads standard input.", metavar="FILE")
]
FormatOpt = Annotated[
    str | None,
    typer.Option(
        "--format", help="csv, tsv, jsonl, json or parquet (default: from the file suffix)."
    ),
]


# Import -----------------------------------------------------------------------------------


def _import(
    ctx: typer.Context,
    target: importing.Target,
    name: str,
    file: str,
    *,
    domain: str | None,
    branch: str | None,
    fmt: str | None,
    delimiter: str | None,
    mapping: list[str] | None,
    ignore_unknown: bool,
    batch_size: int | None,
    dry_run: bool,
    message: str | None,
    json_out: bool,
    link_columns: list[str] | None = None,
) -> None:
    app = app_of(ctx, json_out)
    client = app.client()
    domain_id = app.domain(domain)
    chosen_branch = app.branch(branch)
    size = batch_size or app.settings.batch_size
    kind = sources.detect_format(file, fmt)
    schema = client.schema.get(domain_id, branch=chosen_branch).schema_
    prepared = importing.prepare(
        schema,
        target,
        name,
        renames=importing.parse_renames(mapping or []),
        link_columns=importing.parse_link_columns(link_columns or []),
    )

    def rows() -> Any:
        return importing.coerced_rows(
            sources.read_rows(file, kind, delimiter=delimiter),
            prepared,
            ignore_unknown=ignore_unknown,
        )

    total = None
    if file != sources.STDIN:  # check every row first, so a bad value stops before any write
        total = sum(1 for _ in rows())
    expected_batches = None if total is None else -(-total // size)

    progress = _progress(app, expected_batches)
    note = message or f"Import {name} from {Path(file).name if file != sources.STDIN else 'stdin'}"
    report = importing.run_import(
        client,
        domain=domain_id,
        branch=chosen_branch,
        target=target,
        name=name,
        rows=rows(),
        batch_size=size,
        dry_run=dry_run,
        message=note,
        progress=progress,
    )
    app.out.emit(report.summary(), lambda: _report_import(app, report))


def _progress(app: AppContext, expected_batches: int | None) -> importing.Progress:
    def progress(batch: importing.BatchResult) -> None:
        if app.json_mode or app.quiet:
            return
        of = f"/{expected_batches}" if expected_batches else ""
        end = batch.first_row + batch.rows - 1
        app.out.info(
            f"batch {batch.number}{of}: rows {batch.first_row}-{end} {batch.status}, "
            f"{batch.edits} edits"
        )

    return progress


def _report_import(app: AppContext, report: importing.ImportReport) -> None:
    app.out.pairs(
        [
            (report.target.removesuffix("s"), report.name),
            ("branch", report.branch or "(default)"),
            ("mode", "dry run" if report.dry_run else "committed"),
            ("rows", report.rows),
            ("edits", report.edits),
            ("batches", len(report.batches)),
            ("revisions", [short_id(r) for r in report.revisions]),
        ]
    )


MapOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--map",
        help="Rename a file column to an Attribute: --map file_column=attribute. Repeatable.",
    ),
]
IgnoreOpt = Annotated[
    bool, typer.Option("--ignore-unknown", help="Skip columns that are not Attributes.")
]
BatchOpt = Annotated[
    int | None,
    typer.Option("--batch-size", min=1, help="Rows per request (default: batch_size setting)."),
]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Validate and compute; write nothing.")]
MessageOpt = Annotated[
    str | None, typer.Option("--message", "-m", help="Revision message (default names the file).")
]
DelimiterOpt = Annotated[
    str | None, typer.Option("--delimiter", help="CSV field separator (default `,`).")
]


LinkOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--link",
        help="Send a file column as Links of the row: --link relation=column, the cell holding "
        "the key at the other end. Repeatable.",
    ),
]


@import_app.command("records")
def import_records(
    ctx: typer.Context,
    kind: Annotated[
        str, typer.Argument(help="Kind api name, for example Supplier.", metavar="KIND")
    ],
    file: FileArg,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    fmt: FormatOpt = None,
    delimiter: DelimiterOpt = None,
    mapping: MapOpt = None,
    link: LinkOpt = None,
    ignore_unknown: IgnoreOpt = False,
    batch_size: BatchOpt = None,
    dry_run: DryRunOpt = False,
    message: MessageOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Upsert Records of a Kind from a file.

    Columns are Attribute api names; values are converted to each Attribute's type (true and
    false become booleans, numbers and decimals stay exact, empty cells leave an Attribute
    unset). Kinds without a natural key take a `key` column.

    A Kind whose Records need a Link when they are created takes it from a column:
    `--link with_supplier=supplier_code` creates each Record together with its Link to the
    Record whose key is in that column (JSON Lines and Parquet cells may hold a list of keys).
    """
    _import(
        ctx,
        "records",
        kind,
        file,
        domain=domain,
        branch=branch,
        fmt=fmt,
        delimiter=delimiter,
        mapping=mapping,
        ignore_unknown=ignore_unknown,
        batch_size=batch_size,
        dry_run=dry_run,
        message=message,
        json_out=json_out,
        link_columns=link,
    )


@import_app.command("links")
def import_links(
    ctx: typer.Context,
    relation: Annotated[
        str, typer.Argument(help="Forward Relation api name, e.g. supplied_by.", metavar="RELATION")
    ],
    file: FileArg,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    fmt: FormatOpt = None,
    delimiter: DelimiterOpt = None,
    mapping: MapOpt = None,
    ignore_unknown: IgnoreOpt = False,
    batch_size: BatchOpt = None,
    dry_run: DryRunOpt = False,
    message: MessageOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Upsert Links of a Relation from a file.

    Columns `from` and `to` (or `from_key` and `to_key`) hold the keys of the two ends; other
    columns are Link Attributes.
    """
    _import(
        ctx,
        "links",
        relation,
        file,
        domain=domain,
        branch=branch,
        fmt=fmt,
        delimiter=delimiter,
        mapping=mapping,
        ignore_unknown=ignore_unknown,
        batch_size=batch_size,
        dry_run=dry_run,
        message=message,
        json_out=json_out,
    )


@import_app.command("batch")
def import_batch(
    ctx: typer.Context,
    records: Annotated[
        list[str] | None,
        typer.Option(
            "--records",
            help="Records of a Kind from a file: --records Kind=file. Repeatable; list the Kinds "
            "others point to first.",
        ),
    ] = None,
    links: Annotated[
        list[str] | None,
        typer.Option(
            "--links", help="Links of a Relation from a file: --links relation=file. Repeatable."
        ),
    ] = None,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    fmt: FormatOpt = None,
    delimiter: DelimiterOpt = None,
    mapping: MapOpt = None,
    ignore_unknown: IgnoreOpt = False,
    batch_size: BatchOpt = None,
    dry_run: DryRunOpt = False,
    message: MessageOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Upsert Records of several Kinds together with their Links.

    Files are read as `import records` and `import links` read them (`--map` applies to every
    file). Each request carries Records with the Links at their ends, so Kinds that need a Link
    when they are created (a Relation marked required_from) load without a Link column. Links
    go with the Record at their `from` end, or at their `to` end when that one is not in the
    import. Records without Links go first, then Records with Links, then the other Links;
    --batch-size counts Records and Links together.

    Example: lorebase import batch --records Supplier=supplier.csv --records
    Contract=contract.csv --links with_supplier=with_supplier.csv
    """
    app = app_of(ctx, json_out)
    record_sources = batch_import.parse_sources(records or [], "--records", "Kind=file")
    link_sources = batch_import.parse_sources(links or [], "--links", "relation=file")
    if not record_sources and not link_sources:
        raise CliError("give at least one --records Kind=file or --links relation=file")
    client = app.client()
    domain_id = app.domain(domain)
    chosen_branch = app.branch(branch)
    size = batch_size or app.settings.batch_size
    schema = client.schema.get(domain_id, branch=chosen_branch).schema_
    plan = (
        batch_import.BatchImport(  # reads and checks every row, so a bad one stops before any write
            schema,
            record_sources,
            link_sources,
            fmt=fmt,
            delimiter=delimiter,
            renames=importing.parse_renames(mapping or []),
            ignore_unknown=ignore_unknown,
        )
    )
    names = [s.name for s in (*record_sources, *link_sources)]
    note = message or f"Import {len(record_sources)} Kinds and {len(link_sources)} Relations"
    report = batch_import.run_batch_import(
        client,
        plan,
        domain=domain_id,
        branch=chosen_branch,
        name=", ".join(names),
        batch_size=size,
        dry_run=dry_run,
        message=note,
        progress=_progress(app, plan.expected_requests(size)),
    )
    app.out.emit(report.summary(), lambda: _report_import(app, report))


# Records ----------------------------------------------------------------------------------

MAX_VALUE_COLUMNS = 6


def record_rows(
    records: list[m.RecordItem], select: list[str]
) -> tuple[list[dict[str, Any]], list[Column]]:
    """Table rows and columns for Records: key, title and the selected (or first) values."""
    names: list[str] = list(select)
    if not names:
        for record in records:
            for name in record.values:
                if name not in names and len(names) < MAX_VALUE_COLUMNS:
                    names.append(name)
    columns = [Column("key", "key"), Column("title", "title"), *(Column(n, n) for n in names)]
    rows = [{"key": r.key, "title": r.title, **r.values} for r in records]
    return rows, columns


@record_app.command("get")
def record_get(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Kind api name.", metavar="KIND")],
    key: Annotated[str, typer.Argument(help="Record key.", metavar="KEY")],
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    at: AtOpt = None,
    links: Annotated[bool, typer.Option("--links", help="Add Link counts per Relation.")] = False,
    lineage: Annotated[
        bool, typer.Option("--lineage", help="Add where each value came from.")
    ] = False,
    json_out: JsonOpt = False,
) -> None:
    """Show one Record with its values."""
    app = app_of(ctx, json_out)
    found = app.client().records.get(
        app.domain(domain),
        kind,
        key,
        branch=app.branch(branch),
        at=at,
        links=links,
        lineage=lineage,
    )
    record = found.record

    def human() -> None:
        pairs: list[tuple[str, Any]] = [
            ("kind", record.kind),
            ("key", record.key),
            ("title", record.title),
            *record.values.items(),
            ("revision", short_id(found.revision)),
        ]
        if record.links:
            pairs.extend((f"links: {c.relation}", c.count) for c in record.links)
        if record.lineage:
            pairs.extend(
                (f"lineage: {name}", f"{item.origin_type} by {item.actor} at {item.at}")
                for name, item in record.lineage.items()
            )
        app.out.pairs(pairs)

    app.out.emit(found, human)


@record_app.command("list")
def record_list(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Kind api name.", metavar="KIND")],
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    at: AtOpt = None,
    where: Annotated[
        str | None,
        typer.Option("--where", help="Filter: a DSL predicate as JSON, or @file.json."),
    ] = None,
    order_by: Annotated[
        list[str] | None,
        typer.Option("--order-by", help="Attribute to sort by, `name` or `name:desc`. Repeatable."),
    ] = None,
    select: Annotated[
        list[str] | None, typer.Option("--select", help="Attribute to show. Repeatable.")
    ] = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the Records of a Kind."""
    app = app_of(ctx, json_out)
    predicate = json_argument(where) if where else None
    items, more = app.take(
        app.client().records.iterate(
            app.domain(domain),
            kind,
            branch=app.branch(branch),
            at=at,
            where=predicate,
            order_by=order_by,
            select=select,
        ),
        limit,
        everything,
    )
    _print_records(app, items, select or [])
    if more:
        app.note_truncated(len(items))


def _print_records(app: AppContext, items: list[m.RecordItem], select: list[str]) -> None:
    def human() -> None:
        rows, columns = record_rows(items, select)
        app.out.table(rows, columns)

    app.out.emit(items, human)


def json_argument(text: str) -> Any:
    """JSON given inline, or `@path` to read it from a file, or `-` for standard input."""
    source = text
    if text == "-":
        source = sys.stdin.read()
    elif text.startswith("@"):
        try:
            source = Path(text[1:]).read_text(encoding="utf-8")
        except OSError as error:
            raise CliError(f"cannot read {text[1:]}: {error.strerror or error}") from error
    try:
        return json.loads(source)
    except ValueError as error:
        raise CliError(f"not valid JSON: {error}") from error


# Query and search -------------------------------------------------------------------------


def query_command(
    ctx: typer.Context,
    query: Annotated[
        str,
        typer.Argument(
            help="A query of the JSON DSL: inline JSON, a file path, or `-` for standard input.",
            metavar="QUERY",
        ),
    ],
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    at: AtOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Run a query of the JSON DSL (filters, Relations, includes, aggregates).

    Example: lorebase query '{"kind": "Part", "where": {"attr": "category", "op": "eq",
    "value": "bearing"}}'
    """
    app = app_of(ctx, json_out)
    body = _query_body(query)
    if branch or app.settings.branch:
        body.setdefault("branch", app.branch(branch))
    if at:
        body["at"] = at
    client = app.client()
    domain_id = app.domain(domain)
    if body.get("aggregate"):
        result = client.records.query(domain_id, body)
        app.out.emit(result, lambda: _print_aggregate(app, result))
        return
    items, more = app.take(client.records.iterate_query(domain_id, body), limit, everything)
    selected = [str(s) for s in body.get("select") or [] if s not in ("key", "title")]
    _print_records(app, items, selected)
    if more:
        app.note_truncated(len(items))


def _query_body(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("{") or candidate == "-":
        parsed = json_argument(candidate)
    else:
        parsed = json_argument("@" + candidate)
    if not isinstance(parsed, dict):
        raise CliError("a query is a JSON object")
    return parsed


def _print_aggregate(app: AppContext, result: m.QueryResult) -> None:
    assert result.aggregate is not None
    rows = [c.model_dump() for c in result.aggregate.counts]
    app.out.table(
        rows, [Column("kind", "kind"), Column("value", "value"), Column("count", "count")]
    )
    if result.aggregate.truncated:
        app.out.info("the groups were cut at the server limit")


def search_command(
    ctx: typer.Context,
    text: Annotated[str, typer.Argument(help="Words to look for.", metavar="TEXT")],
    kind: Annotated[
        list[str] | None, typer.Option("--kind", help="Only this Kind. Repeatable.")
    ] = None,
    mode: Annotated[
        str | None, typer.Option("--mode", help="lexical, semantic or hybrid (default: lexical).")
    ] = None,
    domain: DomainOpt = None,
    branch: BranchOpt = None,
    limit: LimitOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Search Records by text."""
    app = app_of(ctx, json_out)
    request = m.SearchRequest.model_validate(
        {
            "q": text,
            "kinds": kind,
            "mode": mode,
            "branch": app.branch(branch),
            "limit": limit or app.settings.list_limit,
        }
    )
    result = app.client().records.search(app.domain(domain), request)
    app.out.emit(
        result,
        lambda: app.out.table(
            [h.model_dump() for h in result.items],
            [
                Column("kind", "kind"),
                Column("key", "key"),
                Column("title", "title"),
                Column("score", "score", lambda v: f"{v:.3f}"),
                Column("snippet", "snippet"),
            ],
        ),
    )
