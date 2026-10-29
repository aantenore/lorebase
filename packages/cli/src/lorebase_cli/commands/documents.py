"""`documents upload|list|versions|get|text|retire|purge`: documents stored for extraction by
Scribe. Uploads with `--key` are versions of one source; retiring stops further extraction and
purging irreversibly erases the content."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from lorebase_cli.context import AppContext, app_of
from lorebase_cli.failures import CliError
from lorebase_cli.options import AllOpt, DomainOpt, JsonOpt, LimitOpt
from lorebase_cli.output import Column
from lorebase_client import models as m

documents_app = typer.Typer(
    help="Upload documents and follow their extraction.", no_args_is_help=True
)

DOCUMENT_COLUMNS = [
    Column("id", "id"),
    Column("filename", "filename"),
    Column("type", "media_type"),
    Column("size", "size"),
    Column("key", "source_key"),
    Column("v", "version"),
    Column("status", "status"),
    Column("created", "created_at"),
]


KeyOpt = Annotated[
    str | None,
    typer.Option("--key", help="Source key (path, URL or external id): the document's identity."),
]
ReasonOpt = Annotated[str, typer.Option("--reason", help="Why, recorded in the audit.")]


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
        ("source key", found.source_key),
        ("version", found.version),
        ("replaces", found.replaces),
        ("status", found.status),
        ("text parsed", found.text_sha256 is not None),
        ("created", found.created_at),
        ("created by", found.created_by),
        *(
            [("retired", f"{found.retired_at} by {found.retired_by}")]
            if found.retired_at is not None
            else []
        ),
        *(
            [("purged", f"{found.purged_at} by {found.purged_by}")]
            if found.purged_at is not None
            else []
        ),
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
    key: KeyOpt = None,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Upload a document (PATH, or --url) and queue its extraction.

    With --key, new bytes become a new version of the key's document.
    """
    app = app_of(ctx, json_out)
    name = app.domain(domain)
    extract = False if no_extract else None
    wanted = _tiers(tiers)
    documents = app.client().documents
    if path is not None and url is None:
        try:
            uploaded = documents.upload(name, path, extract=extract, tiers=wanted, source_key=key)
        except OSError as error:
            raise CliError(f"cannot read {path}: {error.strerror or error}") from error
    elif url is not None and path is None:
        uploaded = documents.upload_url(name, url, extract=extract, tiers=wanted, source_key=key)
    else:
        raise CliError("give PATH or --url")
    app.out.emit(
        uploaded,
        lambda: app.out.pairs(
            [
                *_document_pairs(uploaded.document),
                ("duplicate", uploaded.duplicate),
                ("job", uploaded.job_id),
                ("check job", uploaded.check_job_id),
            ]
        ),
    )


@documents_app.command("list")
def documents_list(
    ctx: typer.Context,
    key: Annotated[
        str | None, typer.Option("--key", help="Only the versions of this source key.")
    ] = None,
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the documents of a Domain, newest first."""
    app = app_of(ctx, json_out)
    stream = app.client().documents.iterate(app.domain(domain), source_key=key)
    items, more = app.take(stream, limit, everything)
    app.out.emit(items, lambda: app.out.table([d.model_dump() for d in items], DOCUMENT_COLUMNS))
    if more:
        app.note_truncated(len(items))


@documents_app.command("versions")
def documents_versions(
    ctx: typer.Context,
    document_id: Annotated[str, typer.Argument(help="Document id.", metavar="ID")],
    domain: DomainOpt = None,
    limit: LimitOpt = None,
    everything: AllOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """List the versions of a document's source key, newest first."""
    app = app_of(ctx, json_out)
    stream = app.client().documents.iterate_versions(app.domain(domain), document_id)
    items, more = app.take(stream, limit, everything)
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


@documents_app.command("text")
def documents_text(
    ctx: typer.Context,
    document_id: Annotated[str, typer.Argument(help="Document id.", metavar="ID")],
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Print the parsed text of a document (the text extraction evidence offsets index)."""
    app = app_of(ctx, json_out)
    found = app.client().documents.text(app.domain(domain), document_id)

    def human() -> None:
        app.out.text(found.text)
        if found.truncated:
            app.out.info(f"text cut at {len(found.text)} of {found.length} characters")

    app.out.emit(found, human)


@documents_app.command("retire")
def documents_retire(
    ctx: typer.Context,
    document_id: Annotated[str, typer.Argument(help="Document id.", metavar="ID")],
    reason: ReasonOpt,
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Retire a document: it is never extracted again and its evidence is re-checked."""
    app = app_of(ctx, json_out)
    done = app.client().documents.retire(app.domain(domain), document_id, reason=reason)
    app.out.emit(
        done,
        lambda: app.out.pairs([*_document_pairs(done.document), ("check job", done.job_id)]),
    )


@documents_app.command("purge")
def documents_purge(
    ctx: typer.Context,
    document_id: Annotated[str, typer.Argument(help="Document id.", metavar="ID")],
    reason: ReasonOpt,
    confirm: Annotated[
        str, typer.Option("--confirm", help="The document id again: the purge is irreversible.")
    ],
    domain: DomainOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Irreversibly erase a document's bytes, text, chunks and evidence quotes."""
    if confirm != document_id:
        raise CliError(f"--confirm must repeat the document id {document_id!r}")
    app = app_of(ctx, json_out)
    done = app.client().documents.purge(
        app.domain(domain), document_id, reason=reason, confirm=confirm
    )
    app.out.emit(
        done,
        lambda: app.out.pairs(
            [
                *_document_pairs(done.document),
                ("check job", done.job_id),
                ("blob deleted", done.blob_deleted),
                ("text deleted", done.text_deleted),
                ("chunks", done.chunks),
                ("cache entries", done.cache_entries),
                ("items erased", done.items),
                ("candidates erased", done.candidates),
                ("suggestions erased", done.suggestions),
            ]
        ),
    )
