"""Documents of a Domain (design 9.2 step 1): intake for extraction, listing, details.

| Method and path | Permission | Notes |
|-----------------|------------|-------|
| `POST /domains/{d}/documents` | `document.write` | 201 new, 200 same bytes stored before |
| `GET /domains/{d}/documents` | `document.read` | newest first |
| `GET /domains/{d}/documents/{id}` | `document.read` | with its extraction jobs |
| `GET /domains/{d}/documents/{id}/text` | `document.read` | parsed text, cached by its hash |

Both permissions need a Domain-wide grant: documents are raw input, not Records of a Kind.

The upload is `multipart/form-data` with the bytes in `file` (optional fields `extract`,
`true` or `false`, and `tiers`, a JSON list such as `[0, 1]`), or JSON naming a URL:
`{"url": "https://docs.example.com/c.pdf", "filename": "c.pdf", "extract": true, "tiers":
[0, 1, 2]}`. URLs must use an allowed scheme and an allowlisted host
(`keeper.documents.url_hosts`) that resolves to global unicast addresses only; Keeper connects
to those checked addresses without following redirects (`services/document_urls.py`). The media
type comes from the file name suffix (`keeper.documents.media_types`) and is checked against
the content (PDF and DOCX signatures, UTF-8 text). Documents are stored content addressed: the
same bytes uploaded again return the stored document. Unless `extract` is false (default
`keeper.documents.extract_on_upload`), an `extraction` job is queued for Scribe; its id is in
the answer. Uploads are audited (`document.upload`).

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
from typing import Annotated, Any
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.datastructures import UploadFile

from lorebase_core.model import new_ulid
from lorebase_keeper.auth.audit import annotate
from lorebase_keeper.auth.deps import Access, require
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.context import StoreDep, require_domain
from lorebase_keeper.deps import KeeperSettingsDep
from lorebase_keeper.errors import (
    BAD_GATEWAY,
    BAD_REQUEST,
    NOT_FOUND,
    PAYLOAD_TOO_LARGE,
    VALIDATION_FAILED,
    Problem,
    ProblemError,
)
from lorebase_keeper.pagination import Page, PageParamsDep, encode_cursor
from lorebase_keeper.reading.caching import respond
from lorebase_keeper.reading.runtime import ReadRuntimeDep
from lorebase_keeper.services import document_urls
from lorebase_keeper.services.config import DocumentSettings
from lorebase_store.blobs import TEXT_REF_KEY, BlobNotFoundError, PostgresBlobStore, sha256
from lorebase_store.jobs import EXTRACTION_JOB, enqueue_job, extraction_payload
from lorebase_store.repositories import (
    DocumentRepository,
    DocumentRow,
    ExtractionJobRepository,
    ExtractionJobRow,
)

router = APIRouter(tags=["documents"])

DOCUMENTS = "/domains/{domain_id}/documents"
DOCUMENT_PREFIX = "doc"
MULTIPART = "multipart/form-data"
_TIERS = frozenset({0, 1, 2})
_SIGNATURES = {"application/pdf": b"%PDF-", "application/zip": b"PK\x03\x04"}

_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": Problem, "description": "The caller lacks the permission Domain-wide."},
    404: {"model": Problem, "description": "Domain or document not found."},
    413: {"model": Problem, "description": "The document is too large."},
    422: {"model": Problem, "description": "Unsupported or invalid document, URL or fields."},
    502: {"model": Problem, "description": "The URL could not be fetched."},
}
Write = Annotated[Access, Depends(require(Permission.DOCUMENT_WRITE))]
Read = Annotated[Access, Depends(require(Permission.DOCUMENT_READ))]


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


class DocumentDetailOut(DocumentOut):
    extraction_jobs: list[ExtractionJobOut] = Field(description="Newest first.")


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


async def _upload(
    request: Request, settings: DocumentSettings
) -> tuple[str, bytes, bool | None, list[int] | None, dict[str, Any]]:
    try:
        form = await request.form(max_files=1, max_fields=2, max_part_size=settings.max_bytes + 1)
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
        return _safe_name(upload.filename or ""), data, extract, tiers, {"source": "upload"}
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
    """Store a document (multipart upload or allowlisted URL) and queue its extraction."""
    settings = keeper.documents
    if request.headers.get("content-type", "").startswith(MULTIPART):
        filename, data, extract, tiers, metadata = await _upload(request, settings)
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
        if not settings.url_hosts:
            raise _invalid("/body/url", "URL intake is disabled (keeper.documents.url_hosts)")
        filename, data = await _fetch(body, settings)
        extract = body.extract
        tiers = _tiers(body.tiers, "/body/tiers")
        metadata = {"source": "url", "url": body.url}
    media_type = _media_type(filename, data, settings)
    digest = sha256(data)
    extract = settings.extract_on_upload if extract is None else extract
    job_id: int | None = None
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        ref = await PostgresBlobStore().put(session, domain_id, data)
        row, created = await DocumentRepository(session, domain_id).add(
            DocumentRow(
                id=f"{DOCUMENT_PREFIX}_{new_ulid()}",
                filename=filename,
                media_type=media_type,
                sha256=digest,
                size=len(data),
                blob_ref=ref,
                created_by=access.principal.id,
                metadata=metadata,
            )
        )
        if extract:
            job_id = await enqueue_job(
                session,
                EXTRACTION_JOB,
                domain_id=domain_id,
                payload=extraction_payload([row.id], tiers=tiers, requested_by=access.principal.id),
                unique=True,
            )
    annotate(
        request,
        action="document.upload",
        target=row.id,
        sha256=digest.hex(),
        size=len(data),
        media_type=media_type,
        duplicate=not created,
        job=job_id,
    )
    if not created:
        response.status_code = 200
    response.headers["Location"] = f"{request.url.path.rstrip('/')}/{row.id}"
    return DocumentUploaded(document=_document(row), duplicate=not created, job_id=job_id)


@router.get(DOCUMENTS, summary="List documents", responses=_ERRORS)
async def list_documents(
    domain_id: str, access: Read, store: StoreDep, params: PageParamsDep
) -> Page[DocumentOut]:
    """Documents of the Domain, newest first."""
    after = None
    if params.position is not None:
        try:
            after = (
                datetime.fromisoformat(str(params.position["at"])),
                str(params.position["id"]),
            )
        except (KeyError, ValueError):
            raise ProblemError(VALIDATION_FAILED, "The cursor is not a document cursor.") from None
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        rows = await DocumentRepository(session, domain_id).page(
            limit=params.limit + 1, after=after
        )
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
) -> DocumentDetailOut:
    """The document's metadata and its extraction jobs, newest first."""
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        row = await DocumentRepository(session, domain_id).get(document_id)
        if row is None:
            raise ProblemError(NOT_FOUND, f"Document {document_id!r} not found.")
        jobs = await ExtractionJobRepository(session, domain_id).of_document(
            document_id, limit=keeper.documents.jobs_listed
        )
    return DocumentDetailOut(**_document(row).model_dump(), extraction_jobs=[_job(j) for j in jobs])


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
    async with store.sessions() as session, session.begin():
        await require_domain(session, domain_id)
        row = await DocumentRepository(session, domain_id).get(document_id)
    if row is None:
        raise ProblemError(NOT_FOUND, f"Document {document_id!r} not found.")
    ref = row.metadata.get(TEXT_REF_KEY)
    if row.text_hash is None or not isinstance(ref, str):
        raise ProblemError(
            NOT_FOUND, f"The text of document {document_id!r} has not been parsed yet."
        )
    text_hash = row.text_hash.hex()
    limit = keeper.documents.max_text_chars

    async def produce() -> DocumentTextOut:
        async with store.sessions() as session, session.begin():
            await require_domain(session, domain_id)
            try:
                data = await PostgresBlobStore().get(session, domain_id, ref)
            except BlobNotFoundError:
                raise ProblemError(
                    NOT_FOUND, f"The text of document {document_id!r} is not stored."
                ) from None
        text = data.decode("utf-8")
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
