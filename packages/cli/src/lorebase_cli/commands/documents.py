"""`documents upload|list|get`: documents stored for extraction by Scribe."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from lorebase_cli.context import AppContext, app_of
from lorebase_cli.failures import CliError
from lorebase_cli.options import AllOpt, DomainOpt, JsonOpt, LimitOpt
from lorebase_cli.output import Column, short_id
from lorebase_client import models as m

documents_app = typer.Typer(
    help="Upload documents and follow their extraction.", no_args_is_help=True
)

DOCUMENT_COLUMNS = [
    Column("id", "id"),
    Column("filename", "filename"),
    Column("type", "media_type"),
    Column("size", "size"),
    Column("sha256", "sha256", short_id),
    Column("created", "created_at"),
]


def _tiers(text: str | None) -> list[int] | None:
    """Extraction tiers from a comma separated list such as `0,1`."""
    if text is None:
        return None
    try:
        return [int(part) for part in text.split(",")]
    except ValueError:
        raise CliError(f"--tiers is a comma separated list of integers, got {text!r}") from None


def _document_pairs(found: m.Document) -> list[tuple[str, Any]]:
    return [
        ("document", found.id),
        ("filename", found.filename),
        ("media type", found.media_type),
        ("size", found.size),
        ("sha256", found.sha256),
        ("text parsed", found.text_sha256 is not None),
        ("created", found.created_at),
        ("created by", found.created_by),
    ]


@documents_app.command("upload")
def documents_upload(
    ctx: typer.Context,
    path: Annotated[
        Path | None,
        typer.Argument(help="File to upload.", metavar="[PATH]", show_default=False),
    ] = None,
    url: Annotated[
        str | None,
        typer.Option("--url", help="Have the server fetch the document from this URL instead."),
    ] = None,
    no_extract: Annotated[
        bool, typer.Option("--no-extract", help="Store the document without queueing extraction.")
    ] = False,
    tiers: Annotated[
        str | None, typer.Option("--tiers", help="Extraction tiers, comma separated (e.g. 0,1).")
    ] = None,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Upload a document (PATH, or --url) and queue its extraction."""
    app = app_of(ctx, json_out)
    name = app.domain(domain)
    extract = False if no_extract else None
    wanted = _tiers(tiers)
    documents = app.client().documents
    if path is not None and url is None:
        try:
            uploaded = documents.upload(name, path, extract=extract, tiers=wanted)
        except OSError as error:
            raise CliError(f"cannot read {path}: {error.strerror or error}") from error
    elif url is not None and path is None:
        uploaded = documents.upload_url(name, url, extract=extract, tiers=wanted)
    else:
        raise CliError("give PATH or --url")
    app.out.emit(
        uploaded,
        lambda: app.out.pairs(
            [
                *_document_pairs(uploaded.document),
                ("duplicate", uploaded.duplicate),
                ("job", uploaded.job_id),
            ]
        ),
    )


@documents_app.command("list")
def documents_list(
    ctx: typer.Context,
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the documents of a Domain, newest first."""
    app = app_of(ctx, json_out)
    items, more = app.take(app.client().documents.iterate(app.domain(domain)), limit, everything)
    app.out.emit(items, lambda: app.out.table([d.model_dump() for d in items], DOCUMENT_COLUMNS))
    if more:
        app.note_truncated(len(items))


def _show_document(app: AppContext, found: m.DocumentDetail) -> None:
    app.out.pairs(
        [
            *_document_pairs(found),
            *(
                (
                    f"job {job.id}",
                    f"{job.status} ({job.model}, prompt {job.prompt_version})"
                    + (f", proposal {job.proposal_id}" if job.proposal_id else ""),
                )
                for job in found.extraction_jobs
            ),
        ]
    )


@documents_app.command("get")
def documents_get(
    ctx: typer.Context,
    document_id: Annotated[str, typer.Argument(help="Document id.", metavar="ID")],
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Show a document with its extraction jobs."""
    app = app_of(ctx, json_out)
    found = app.client().documents.get(app.domain(domain), document_id)
    app.out.emit(found, lambda: _show_document(app, found))
