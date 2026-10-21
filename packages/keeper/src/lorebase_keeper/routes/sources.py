"""Structured Sources of a Domain (design 9.1): trigger a synchronization, list its runs.

`POST /domains/{d}/sources/{s}/sync` queues a `source_sync` job that Scribe runs (permission
`source.sync`: `editor`, `maintainer`, `admin`) and answers 202 with the job id; while an
identical job is queued or running, that job is returned instead of a new one. The Source is
named by id or api name and looked up in the schema of the Domain's default branch. Every
trigger is audited (`source.sync`).

`GET /domains/{d}/sources/{s}/runs` lists the runs recorded by Scribe, newest first, with their
counts, Revision, Proposal and error (Domain-wide `data.read`: run errors quote row values).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Source
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.context import StoreDep, require_domain
from lorebase_keeper.errors import NOT_FOUND, VALIDATION_FAILED, Problem, ProblemError
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_store.commit.schema import load_compiled
from lorebase_store.jobs import SOURCE_SYNC_JOB, enqueue_job, source_sync_payload
from lorebase_store.repositories import (
    MaterializedRepository,
    SourceRunRepository,
    SourceRunRow,
)
from lorebase_store.runtime import StoreRuntime

router = APIRouter(tags=["sources"])

BASE = "/domains/{domain_id}/sources/{source}"
_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission."},
    404: {"model": Problem, "description": "Domain or Source not found."},
}

Sync = Annotated[Access, Depends(require(Permission.SOURCE_SYNC))]
ReadRuns = Annotated[Access, Depends(require(Permission.DATA_READ))]


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dry_run: bool = Field(
        default=False, description="Compute and record the delta without writing to any branch."
    )


class SyncQueued(BaseModel):
    job_id: int = Field(description="Id of the queued (or already pending) job.")
    source: str = Field(description="Source id.")
    api_name: str
    dry_run: bool


class SourceRunOut(BaseModel):
    id: str
    source: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stats: dict[str, Any]
    revision: str | None = None
    proposal_id: str | None = None
    error: dict[str, Any] | None = None


async def _source(
    session: AsyncSession, domain_id: str, reference: str, runtime: StoreRuntime
) -> Source:
    domain = await require_domain(session, domain_id)
    compiled = await MaterializedRepository(
        session, domain_id, domain.default_branch, writes=runtime.settings.writes
    ).schema()
    schema = None if compiled is None else load_compiled(compiled.compiled)
    found = None
    if schema is not None:
        found = schema.sources.get(reference) or schema.source_by_api_name(reference)
    if found is None:
        raise ProblemError(NOT_FOUND, f"Source {reference!r} not found.")
    return found


def _run(row: SourceRunRow) -> SourceRunOut:
    return SourceRunOut(
        id=row.id,
        source=row.source_id,
        status=row.status.value,
        started_at=row.started_at,
        finished_at=row.finished_at,
        stats=row.stats,
        revision=None if row.revision_id is None else row.revision_id.hex(),
        proposal_id=row.proposal_id,
        error=row.error,
    )


@router.post(
    f"{BASE}/sync",
    status_code=202,
    summary="Synchronize a Source",
    response_model=SyncQueued,
    responses=_ERRORS,
)
async def sync_source(
    domain_id: str,
    source: str,
    access: Sync,
    store: StoreDep,
    request: Request,
    response: Response,
    body: SyncRequest | None = None,
) -> SyncQueued:
    """Queue a synchronization of the Source for Scribe."""
    dry_run = body is not None and body.dry_run
    async with store.sessions() as session, session.begin():
        found = await _source(session, domain_id, source, store.runtime)
        job_id = await enqueue_job(
            session,
            SOURCE_SYNC_JOB,
            domain_id=domain_id,
            payload=source_sync_payload(found.id, dry_run=dry_run),
            unique=True,
        )
    annotate(request, action="source.sync", target=found.id, job=job_id, dry_run=dry_run)
    response.headers["Location"] = f"{request.url.path.rsplit('/', 1)[0]}/runs"
    return SyncQueued(job_id=job_id, source=found.id, api_name=found.api_name, dry_run=dry_run)


@router.get(f"{BASE}/runs", summary="List Source runs", responses=_ERRORS)
async def list_source_runs(
    domain_id: str, source: str, access: ReadRuns, store: StoreDep, params: PageParamsDep
) -> Page[SourceRunOut]:
    """Runs of the Source, newest first."""
    after = None
    if params.position is not None:
        try:
            after = (
                datetime.fromisoformat(str(params.position["at"])),
                str(params.position["id"]),
            )
        except (KeyError, ValueError):
            raise ProblemError(VALIDATION_FAILED, "The cursor is not a run cursor.") from None
    async with store.sessions() as session, session.begin():
        found = await _source(session, domain_id, source, store.runtime)
        rows = await SourceRunRepository(session, domain_id).page(
            found.id, limit=params.limit + 1, after=after
        )
    more = len(rows) > params.limit
    rows = rows[: params.limit]
    last = rows[-1] if rows else None
    next_cursor = (
        encode_cursor({"at": last.started_at.isoformat(), "id": last.id})
        if more and last is not None and last.started_at is not None
        else None
    )
    return Page[SourceRunOut](items=[_run(r) for r in rows], next_cursor=next_cursor)
