"""Documents of a Domain (design 9.2 step 1 and "Document versions and lifecycle"): intake
for extraction, versions, retirement, purge, listing, details.

| Method and path | Permission | Notes |
|-----------------|------------|-------|
| `POST /domains/{d}/documents` | `document.write` | 201 new or new version, 200 same bytes |
| `GET /domains/{d}/documents` | `document.read` | newest first; `source_key` filters |
| `GET /domains/{d}/documents/{id}` | `document.read` | with its extraction jobs |
| `GET /domains/{d}/documents/{id}/versions` | `document.read` | versions of its key |
| `GET /domains/{d}/documents/{id}/text` | `document.read` | parsed text, cached by its hash |
| `POST /domains/{d}/documents/{id}/retirement` | `document.retire` | queues the evidence check |
| `POST /domains/{d}/documents/{id}/purge` | `document.purge` | `confirm` names the document |

Every permission needs a Domain-wide grant: documents are raw input, not Records of a Kind.

The upload is `multipart/form-data` with the bytes in `file` (optional fields `extract`,
`true` or `false`, `tiers`, a JSON list such as `[0, 1]`, and `source_key`), or JSON naming a
URL: `{"url": "https://docs.example.com/c.pdf", "filename": "c.pdf", "extract": true, "tiers":
[0, 1, 2], "source_key": "contracts/c.pdf"}`. URLs must use an allowed scheme and an
allowlisted host (`keeper.documents.url_hosts`) that resolves to global unicast addresses only;
Keeper connects to those checked addresses without following redirects
(`services/document_urls.py`). The media
type comes from the file name suffix (`keeper.documents.media_types`) and is checked against
the content (PDF and DOCX signatures, UTF-8 text). Documents are stored content addressed: the
same bytes uploaded again return the stored document. Unless `extract` is false (default
`keeper.documents.extract_on_upload`), an `extraction` job is queued for Scribe; its id is in
the answer. Uploads are audited (`document.upload`).

**Versions.** An upload may name a `source_key` (a path, URL or external id; at most
`keeper.documents.max_source_key_chars` once trimmed): bytes equal to the key's active version
return it (200), other bytes become a new version (`version` + 1, `replaces` the previous one,
which becomes `superseded`), audited `document.version`. The evidence check of a new version
(`document_version_check`) is queued by its extraction job once it finished, or here when the
upload does not extract. Keyless uploads keep their content addressing.

**Retirement and purge.** A retired document stays readable and is never extracted again: an
upload asking to extract it explicitly answers 409 `document-retired` (a default extraction is
just not queued). Retiring queues the evidence check with empty text, so values whose evidence
was the document raise `stale` candidates; audited `document.retire`. Purging (admin) retires
the document if needed, then deletes its bytes and parsed text (unless another document holds
the same blob), its chunks and the cached answers of its chunks, and replaces the evidence
quotes of Proposal items, reconciliation candidates and schema suggestions by
`keeper.reads.sensitivity.erased_value`; provenance rows and offsets stay. It is irreversible
and audited `document.purge` in its own transaction, without content. Retiring or purging again
changes nothing. Both lock the document row, as extraction jobs do before writing its text,
chunks or items, and a purge cancels the queued and running extraction jobs of the document: a
job running across a purge stops instead of writing content back.

The text route serves the text Scribe parsed, which the evidence offsets of extraction items
index (Unicode code points). It answers 404 until Scribe parsed the document, is cut at
`keeper.documents.max_text_chars` (`truncated`), and is cached under the text's hash: the
`ETag` changes only when the text does.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, get_args
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile

from lorebase_keeper.auth.audit import annotate, suppress
from lorebase_keeper.auth.deps import Access, AuthDep, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.auth.rbac import NO_CONTEXT
from lorebase_keeper.context import StoreDep
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import (
    BAD_GATEWAY,
    BAD_REQUEST,
    PAYLOAD_TOO_LARGE,
    VALIDATION_FAILED,
    Problem,
    ProblemError,
)
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_keeper.reading.caching import respond
from lorebase_keeper.reading.runtime import ReadRuntimeDep
from lorebase_keeper.services import document_urls
from lorebase_keeper.services import documents as service
from lorebase_keeper.services.config import DocumentSettings
from lorebase_store.repositories import DocumentRow, ExtractionJobRow
from lorebase_store.repositories.documents import DOCUMENT_STATUSES

router = APIRouter(tags=["documents"])

DOCUMENTS = "/domains/{domain_id}/documents"
MULTIPART = "multipart/form-data"
_TIERS = frozenset({0, 1, 2})
_SIGNATURES = {"application/pdf": b"%PDF-", "application/zip": b"PK\x03\x04"}

UPLOAD_ACTION = "document.upload"
RETIRE_ACTION = "document.retire"

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission Domain-wide."},
    404: {"model": Problem, "description": "Domain or document not found."},
    409: {"model": Problem, "description": "The document is retired (`document-retired`)."},
    413: {"model": Problem, "description": "The document is too large."},
    422: {"model": Problem, "description": "Unsupported or invalid document, URL or fields."},
    502: {"model": Problem, "description": "The URL could not be fetched."},
}
_LIFECYCLE_ERRORS: dict[int | str, dict[str, Any]] = {
    403: _ERRORS[403],
    404: _ERRORS[404],
    422: {"model": Problem, "description": "Invalid reason, or `confirm` is not the document id."},
}
Write = Annotated[Access, Depends(require(Permission.DOCUMENT_WRITE))]
Read = Annotated[Access, Depends(require(Permission.DOCUMENT_READ))]
Retire = Annotated[Access, Depends(require(Permission.DOCUMENT_RETIRE))]
Purge = Annotated[Access, Depends(require(Permission.DOCUMENT_PURGE))]


def document_upload_path(api_prefix: str) -> re.Pattern[str]:
    """Path of the upload route, for the request body limit."""
    return re.compile(re.escape(api_prefix) + r"/domains/[^/]+/documents")


class DocumentFromUrl(BaseModel):
    """JSON body: a document fetched from an allowlisted URL."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    filename: str | None = Field(
        default=None, max_length=255, description="Name to store; default: the URL's last segment."
    )
    extract: bool | None = Field(default=None, description="Queue an extraction job.")
    tiers: list[int] | None = Field(default=None, description="Extraction tiers (0, 1, 2).")
    source_key: str | None = Field(
        default=None,
        description="Stable identity (path, URL or external id): new bytes under the same key "
        "are a new version of its document.",
    )


_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            MULTIPART: {
                "schema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "extract": {"type": "boolean"},
                        "tiers": {"type": "string", "description": "JSON list, e.g. [0, 1]"},
                        "source_key": {
                            "type": "string",
                            "description": "Stable identity: new bytes are a new version.",
                        },
                    },
                    "required": ["file"],
                }
            },
            "application/json": {"schema": DocumentFromUrl.model_json_schema()},
        },
    }
}


class ExtractionJobOut(BaseModel):
    id: str
    status: str
    model: str
    prompt_version: str
    branch: str | None = None
    proposal_id: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    usage: dict[str, Any]
    stats: dict[str, Any]
    error: dict[str, Any] | None = None


DocumentStatus = Literal["active", "superseded", "retired", "purged"]
assert set(get_args(DocumentStatus)) == set(DOCUMENT_STATUSES)


class DocumentOut(BaseModel):
    id: str
    filename: str
    media_type: str
    sha256: str
    size: int
    text_sha256: str | None = Field(default=None, description="Set once Scribe parsed the text.")
    metadata: dict[str, Any]
    created_at: datetime | None = None
    created_by: str
    source_key: str | None = Field(default=None, description="Stable identity; null: keyless.")
    version: int = Field(description="1 for keyless documents and the first version of a key.")
    replaces: str | None = Field(default=None, description="The version this one replaced.")
    status: DocumentStatus
    retired_at: datetime | None = None
    retired_by: str | None = None
    purged_at: datetime | None = None
    purged_by: str | None = None


class DocumentDetailOut(DocumentOut):
    extraction_jobs: list[ExtractionJobOut] = Field(description="Newest first.")
    open_stale_candidates: int = Field(
        description="Open `stale` reconciliation candidates whose claims come from this "
        "document (its evidence gone from a newer version, or the document retired)."
    )
    can_retire: bool = Field(description="The caller holds `document.retire` Domain-wide.")
    can_purge: bool = Field(description="The caller holds `document.purge` Domain-wide.")


class RetirementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000, description="Why, recorded in the audit.")


class PurgeIn(RetirementIn):
    confirm: str = Field(min_length=1, description="The document id, to confirm the purge.")


class DocumentRetired(BaseModel):
    document: DocumentOut
    job_id: int | None = Field(
        default=None, description="Queued evidence check; null when it was retired before."
    )


class DocumentPurged(BaseModel):
    document: DocumentOut
    job_id: int | None = Field(
        default=None,
        description="Queued evidence check of the retirement, if this purge retired the document.",
    )
    blob_deleted: bool = Field(description="The bytes were deleted (no other document holds them).")
    text_deleted: bool = Field(description="The parsed text was deleted.")
    chunks: int = Field(description="Chunks deleted.")
    cache_entries: int = Field(description="Cached model answers of its chunks deleted.")
    items: int = Field(description="Proposal items whose evidence quotes were erased.")
    candidates: int = Field(description="Reconciliation candidates whose quotes were erased.")
    suggestions: int = Field(description="Schema suggestions whose example quotes were erased.")


class DocumentTextOut(BaseModel):
    document_id: str
    text_sha256: str = Field(description="SHA-256 of the whole parsed text (UTF-8).")
    text: str = Field(description="The parsed text, cut at `keeper.documents.max_text_chars`.")
    length: int = Field(description="Characters (code points) of the whole parsed text.")
    truncated: bool = Field(description="`text` holds only the first characters.")


class DocumentUploaded(BaseModel):
    document: DocumentOut
    duplicate: bool = Field(description="The same bytes were stored before.")
    job_id: int | None = Field(default=None, description="Queued extraction job, if any.")
    check_job_id: int | None = Field(
        default=None,
        description="Evidence check of a new version queued now (without extraction; with it, "
        "the extraction job queues the check once it finished).",
    )


def _document(row: DocumentRow) -> DocumentOut:
    return DocumentOut(
        id=row.id,
        filename=row.filename,
        media_type=row.media_type,
        sha256=row.sha256.hex(),
        size=row.size,
        text_sha256=None if row.text_hash is None else row.text_hash.hex(),
        metadata=row.metadata,
        created_at=row.created_at,
        created_by=row.created_by,
        source_key=row.source_key,
        version=row.version,
        replaces=row.replaces,
        status=row.status,
        retired_at=row.retired_at,
        retired_by=row.retired_by,
        purged_at=row.purged_at,
        purged_by=row.purged_by,
    )


def _job(row: ExtractionJobRow) -> ExtractionJobOut:
    return ExtractionJobOut(
        id=row.id,
        status=row.status.value,
        model=row.model,
        prompt_version=row.prompt_version,
        branch=row.branch,
        proposal_id=row.proposal_id,
        created_at=row.created_at,
        finished_at=row.finished_at,
        usage=row.usage,
        stats=row.stats,
        error=row.error,
    )


def _invalid(pointer: str, message: str) -> ProblemError:
    """422 naming one invalid request member."""
    return ProblemError(
        VALIDATION_FAILED,
        "One or more request members are invalid.",
        extensions={"errors": [{"pointer": pointer, "message": message, "code": "invalid"}]},
    )


def _tiers(raw: Any, pointer: str) -> list[int] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise _invalid(pointer, "must be a JSON list of tiers") from None
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(t, int) and t in _TIERS for t in raw)
    ):
        raise _invalid(pointer, "must be a non-empty list of tiers among 0, 1 and 2")
    return sorted(set(raw))


def _flag(raw: Any, pointer: str) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    raise _invalid(pointer, "must be true or false")


def _media_type(filename: str, data: bytes, settings: DocumentSettings) -> str:
    """The media type of an accepted document; 422 when unsupported or not what it claims."""
    suffix = PurePosixPath(filename).suffix.lower()
    media_type = settings.media_types.get(suffix)
    if media_type is None:
        accepted = ", ".join(sorted(settings.media_types))
        raise _invalid("/body/file", f"unsupported file type {suffix or '(none)'}; use {accepted}")
    if not data:
        raise _invalid("/body/file", "the document is empty")
    if media_type == "application/pdf":
        valid = data.startswith(_SIGNATURES["application/pdf"])
    elif media_type.endswith("wordprocessingml.document"):
        valid = data.startswith(_SIGNATURES["application/zip"])
    elif media_type.startswith("text/"):
        try:
            data.decode("utf-8-sig")
            valid = b"\x00" not in data
        except UnicodeDecodeError:
            valid = False
    else:
        valid = True
    if not valid:
        raise _invalid("/body/file", f"the content is not a valid {media_type} document")
    return media_type


def _safe_name(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name.strip()
    if not name or name in (".", ".."):
        raise _invalid("/body/filename", "a file name is required")
    return name[:255]


def _source_key(raw: Any, settings: DocumentSettings) -> str | None:
    """The trimmed key of an upload; 422 when empty or longer than allowed."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _invalid("/body/source_key", "must be a string")
    key = raw.strip()
    if not key:
        raise _invalid("/body/source_key", "must not be empty")
    if len(key) > settings.max_source_key_chars:
        raise _invalid(
            "/body/source_key", f"must be at most {settings.max_source_key_chars} characters"
        )
    return key


async def _upload(
    request: Request, settings: DocumentSettings
) -> tuple[str, bytes, bool | None, list[int] | None, str | None, dict[str, Any]]:
    try:
        form = await request.form(max_files=1, max_fields=3, max_part_size=settings.max_bytes + 1)
    except Exception as error:  # the multipart parser reports limits and malformed bodies
        raise ProblemError(BAD_REQUEST, f"Unreadable multipart body: {error}") from None
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise _invalid("/body/file", "a file is required")
        data = await upload.read(settings.max_bytes + 1)
        if len(data) > settings.max_bytes:
            raise ProblemError(
                PAYLOAD_TOO_LARGE, f"The document exceeds {settings.max_bytes} bytes."
            )
        extract = _flag(form.get("extract"), "/body/extract")
        tiers = _tiers(form.get("tiers"), "/body/tiers")
        key = _source_key(form.get("source_key"), settings)
        return _safe_name(upload.filename or ""), data, extract, tiers, key, {"source": "upload"}
    finally:
        await form.close()


async def _fetch(body: DocumentFromUrl, settings: DocumentSettings) -> tuple[str, bytes]:
    try:
        data = await document_urls.fetch(body.url, settings)
    except document_urls.UrlRefusedError as error:
        raise _invalid("/body/url", str(error)) from None
    except document_urls.DocumentTooLargeError:
        raise ProblemError(
            PAYLOAD_TOO_LARGE, f"The document exceeds {settings.max_bytes} bytes."
        ) from None
    except document_urls.UrlUnavailableError as error:
        raise ProblemError(BAD_GATEWAY, f"{str(error).capitalize()}; nothing was stored.") from None
    name = body.filename or unquote(PurePosixPath(urlsplit(body.url).path).name)
    return _safe_name(name), data


@router.post(
    DOCUMENTS,
    status_code=201,
    summary="Upload a document for extraction",
    responses={**_ERRORS, 200: {"model": DocumentUploaded}},
    response_model=DocumentUploaded,
    openapi_extra=_BODY,
)
async def upload_document(
    domain_id: str,
    request: Request,
    response: Response,
    access: Write,
    store: StoreDep,
    keeper: KeeperSettingsDep,
) -> DocumentUploaded:
    """Store a document (multipart upload or allowlisted URL) and queue its extraction; with
    a `source_key`, new bytes are a new version of the key's document."""
    settings = keeper.documents
    if request.headers.get("content-type", "").startswith(MULTIPART):
        filename, data, extract, tiers, key, metadata = await _upload(request, settings)
    else:
        try:
            body = DocumentFromUrl.model_validate(await request.json())
        except ValidationError as error:
            first = error.errors()[0]
            raise _invalid(
                "/body/" + "/".join(str(p) for p in first["loc"]), str(first["msg"])
            ) from None
        except ValueError:
            raise _invalid("/body", "send multipart/form-data or a JSON object") from None
        key = _source_key(body.source_key, settings)
        if not settings.url_hosts:
            raise _invalid("/body/url", "URL intake is disabled (keeper.documents.url_hosts)")
        filename, data = await _fetch(body, settings)
        extract = body.extract
        tiers = _tiers(body.tiers, "/body/tiers")
        metadata = {"source": "url", "url": body.url}
    media_type = _media_type(filename, data, settings)
    uploaded = await service.upload(
        store,
        domain_id,
        service.NewDocument(
            filename=filename,
            data=data,
            media_type=media_type,
            metadata=metadata,
            source_key=key,
            extract=extract,
            tiers=tiers,
        ),
        access.principal.id,
        extract_on_upload=settings.extract_on_upload,
    )
    row, created = uploaded.row, uploaded.created
    annotate(
        request,
        action=UPLOAD_ACTION,
        target=row.id,
        sha256=uploaded.sha256.hex(),
        size=len(data),
        media_type=media_type,
        duplicate=not created,
        job=uploaded.job_id,
        source_key=key,
        version=row.version,
    )
    if not created:
        response.status_code = 200
    response.headers["Location"] = f"{request.url.path.rstrip('/')}/{row.id}"
    return DocumentUploaded(
        document=_document(row),
        duplicate=not created,
        job_id=uploaded.job_id,
        check_job_id=uploaded.check_job_id,
    )


@router.get(DOCUMENTS, summary="List documents", responses=_ERRORS)
async def list_documents(
    domain_id: str,
    access: Read,
    store: StoreDep,
    params: PageParamsDep,
    source_key: Annotated[
        str | None, Query(description="Only the versions of this key (trimmed, exact).")
    ] = None,
) -> Page[DocumentOut]:
    """Documents of the Domain, newest first; with `source_key`, the versions of that key."""
    key = None if source_key is None else source_key.strip() or None
    after = None
    if params.position is not None:
        try:
            after = (
                datetime.fromisoformat(str(params.position["at"])),
                str(params.position["id"]),
            )
        except (KeyError, ValueError):
            raise ProblemError(VALIDATION_FAILED, "The cursor is not a document cursor.") from None
    rows = await service.page(store, domain_id, limit=params.limit + 1, after=after, source_key=key)
    more = len(rows) > params.limit
    rows = rows[: params.limit]
    last = rows[-1] if rows else None
    next_cursor = (
        encode_cursor({"at": last.created_at.isoformat(), "id": last.id})
        if more and last is not None and last.created_at is not None
        else None
    )
    return Page[DocumentOut](items=[_document(r) for r in rows], next_cursor=next_cursor)


@router.get(DOCUMENTS + "/{document_id}", summary="Get a document", responses=_ERRORS)
async def get_document(
    domain_id: str,
    document_id: str,
    access: Read,
    store: StoreDep,
    keeper: KeeperSettingsDep,
    auth: AuthDep,
) -> DocumentDetailOut:
    """The document's metadata and its extraction jobs, newest first."""
    found = await service.detail(
        store, domain_id, document_id, jobs_listed=keeper.documents.jobs_listed
    )
    return DocumentDetailOut(
        **_document(found.row).model_dump(),
        extraction_jobs=[_job(j) for j in found.jobs],
        open_stale_candidates=found.open_stale_candidates,
        can_retire=await auth.authorizer.allows(
            access.principal, Permission.DOCUMENT_RETIRE, domain_id, NO_CONTEXT
        ),
        can_purge=await auth.authorizer.allows(
            access.principal, Permission.DOCUMENT_PURGE, domain_id, NO_CONTEXT
        ),
    )


@router.get(
    DOCUMENTS + "/{document_id}/versions",
    summary="List the versions of a document",
    responses=_ERRORS,
)
async def list_document_versions(
    domain_id: str, document_id: str, access: Read, store: StoreDep, params: PageParamsDep
) -> Page[DocumentOut]:
    """The versions of the document's `source_key`, newest first (only itself when keyless)."""
    before: int | None = None
    if params.position is not None:
        try:
            before = int(params.position["v"])
        except (KeyError, TypeError, ValueError):
            raise ProblemError(VALIDATION_FAILED, "The cursor is not a version cursor.") from None
    rows = await service.versions(
        store, domain_id, document_id, limit=params.limit + 1, before=before
    )
    more = len(rows) > params.limit
    rows = rows[: params.limit]
    next_cursor = encode_cursor({"v": rows[-1].version}) if more and rows else None
    return Page[DocumentOut](items=[_document(r) for r in rows], next_cursor=next_cursor)


@router.post(
    DOCUMENTS + "/{document_id}/retirement",
    summary="Retire a document",
    responses=_LIFECYCLE_ERRORS,
)
async def retire_document(
    domain_id: str,
    document_id: str,
    body: RetirementIn,
    request: Request,
    access: Retire,
    store: StoreDep,
) -> DocumentRetired:
    """Retire a document: it stays readable, is never extracted again, and values whose
    evidence was this document raise `stale` reconciliation candidates."""
    annotate(request, action=RETIRE_ACTION, target=document_id, reason=body.reason)
    row, job = await service.retire(store, domain_id, document_id, access.principal.id)
    annotate(request, job=job, status=row.status)
    return DocumentRetired(document=_document(row), job_id=job)


@router.post(
    DOCUMENTS + "/{document_id}/purge",
    summary="Purge a document (irreversible)",
    responses=_LIFECYCLE_ERRORS,
)
async def purge_document(
    domain_id: str,
    document_id: str,
    body: PurgeIn,
    request: Request,
    access: Purge,
    store: StoreDep,
    keeper: KeeperSettingsDep,
) -> DocumentPurged:
    """Delete a document's bytes, text, chunks and quotes, keeping its provenance (module
    doc); retires it first if needed."""
    if body.confirm != document_id:
        raise _invalid("/body/confirm", "must be the id of the document to purge")
    purged = await service.purge(
        store,
        domain_id,
        document_id,
        access.principal.id,
        reason=body.reason,
        erased_value=keeper.reads.sensitivity.erased_value,
    )
    # The service wrote the audit record of the purge; the request's own entry would repeat it.
    suppress(request)
    return DocumentPurged(document=_document(purged.row), **purged.counts)


@router.get(
    DOCUMENTS + "/{document_id}/text",
    summary="Get the parsed text of a document",
    responses={
        200: {"model": DocumentTextOut},
        304: {"description": "Not modified (`If-None-Match`)."},
        403: _ERRORS[403],
        404: {"model": Problem, "description": "Domain or document not found, or not parsed."},
    },
    response_model=DocumentTextOut,
)
async def get_document_text(
    request: Request,
    domain_id: str,
    document_id: str,
    access: Read,
    store: StoreDep,
    runtime: ReadRuntimeDep,
    keeper: KeeperSettingsDep,
) -> Response:
    """The text Scribe parsed from the document; extraction evidence offsets index it."""
    text_hash, ref = await service.parsed_text_ref(store, domain_id, document_id)
    limit = keeper.documents.max_text_chars

    async def produce() -> DocumentTextOut:
        text = await service.parsed_text(store, domain_id, document_id, ref)
        return DocumentTextOut(
            document_id=document_id,
            text_sha256=text_hash,
            text=text[:limit],
            length=len(text),
            truncated=len(text) > limit,
        )

    return await respond(
        request,
        runtime.cache,
        domain_id=domain_id,
        revision=text_hash,
        produce=produce,
        variant=f"chars={limit}",
    )
