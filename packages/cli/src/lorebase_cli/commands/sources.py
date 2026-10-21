"""`sources sync|runs`: Source synchronizations run by Scribe."""

from __future__ import annotations

from typing import Annotated

import typer

from lorebase_cli.context import app_of
from lorebase_cli.options import AllOpt, DomainOpt, JsonOpt, LimitOpt
from lorebase_cli.output import Column

sources_app = typer.Typer(help="Synchronize the Sources of a Domain.", no_args_is_help=True)

SourceArg = Annotated[
    str, typer.Argument(help="Source id or api name.", metavar="SOURCE", show_default=False)
]

RUN_COLUMNS = [
    Column("id", "id"),
    Column("status", "status"),
    Column("started", "started_at"),
    Column("finished", "finished_at"),
    Column("revision", "revision"),
    Column("proposal", "proposal_id"),
]


@sources_app.command("sync")
def sources_sync(
    ctx: typer.Context,
    source: SourceArg,
    domain: DomainOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Record the delta without writing to any branch.")
    ] = False,
    json_out: JsonOpt = False,
) -> None:
    """Queue a synchronization of a Source; `sources runs` shows its outcome."""
    app = app_of(ctx, json_out)
    queued = app.client().sources.sync(app.domain(domain), source, dry_run=dry_run)
    app.out.emit(
        queued,
        lambda: app.out.pairs(
            [
                ("job", queued.job_id),
                ("source", f"{queued.api_name} ({queued.source})"),
                ("dry run", queued.dry_run),
            ]
        ),
    )


@sources_app.command("runs")
def sources_runs(
    ctx: typer.Context,
    source: SourceArg,
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the runs of a Source, newest first."""
    app = app_of(ctx, json_out)
    runs = app.client().sources.iterate_runs(app.domain(domain), source)
    items, more = app.take(runs, limit, everything)
    app.out.emit(items, lambda: app.out.table([r.model_dump() for r in items], RUN_COLUMNS))
    if more:
        app.note_truncated(len(items))
