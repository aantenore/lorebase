"""The document version trigger of reconciliation (design 9.2 "Document versions and
lifecycle", 10.6 trigger 3): job `document_version_check`.

When a keyed document gets a new version, or a document is retired, the values that came from
the earlier versions may have lost their evidence. The check looks at every value whose current
provenance (the entry its Lineage points at, on the branch the extraction Proposal targets) is
`extraction` from one of the `checked` documents:

- mode `version`: the versions of the key older than the new one; the text compared is the new
  version's parsed text (the text Scribe stored, or the document parsed again when it was not
  extracted);
- mode `retire`: the retired document and, when it was the newest version of its key, the
  versions before it (their evidence lived on through it); the text compared is empty.

The values are found through the accepted extraction items of the checked documents (Record and
value items, their evidence naming the document): a value counts when its current claim comes
from that document and holds the item's value. A value the new version's own extraction proposes
again (an item of the new version, not rejected, with the same value) is supported and left
alone. For the others the old evidence quote is looked for verbatim in the text, with the
whitespace-insensitive match of evidence verification (`tier2.locate`). Present: nothing
changes. Absent, or no longer readable (the document was purged): a `stale` candidate with
signal `evidence_gone` (`lorebase_core.reconcile.evidence_gone_verdict`). The value is never
retracted or changed and no model is called (AC-33).

Derived, vault and primary key Attributes are not checked (they are never reconciliation
claims), nor Link values (candidates name Record Attributes). Candidates follow the Domain's
reconciliation options (`enabled`, `triggers.document_versions`) and the detector's storage:
upsert by fingerprint, claims capped, evidence quotes wrapped with the Attribute's tags.

A job looks at `scribe.extraction.versions.max_values_per_job` values; the rest goes to a
continuation job queued from the cursor (`resume`), like sweeps.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Schema
from lorebase_core.reconcile import ReconcileOptions, evidence_gone_verdict, value_hash
from lorebase_scribe.extraction.sandbox import parse_isolated
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_scribe.extraction.tier2 import locate
from lorebase_scribe.reconcile.detector import (
    Detector,
    Raised,
    ReconcileReport,
    compared,
    document_identity,
    effective_options,
)
from lorebase_store.blobs import TEXT_REF_KEY, BlobNotFoundError, BlobStore, PostgresBlobStore
from lorebase_store.commit.schema import load_compiled
from lorebase_store.models.enums import ItemDecision
from lorebase_store.reconciliation import current_claims
from lorebase_store.repositories import (
    DocumentRepository,
    DocumentRow,
    DomainRepository,
    ExtractionJobRepository,
    MaterializedRepository,
    ProposalItemRepository,
    ProposalRepository,
    scope_to_domain,
)
from lorebase_store.repositories.documents import ACTIVE, PURGED, SUPERSEDED
from lorebase_store.repositories.proposal_items import RECORD_ITEM, VALUE_ITEM, ItemRow
from lorebase_store.runtime import StoreRuntime

log = logging.getLogger("lorebase.scribe.reconcile")

VERSION_MODE = "version"
RETIRE_MODE = "retire"

Cursor = tuple[int, str, int, str, str]
"""Position of a value in the check: document version, job id, item seq, item id, Attribute."""


class VersionCheckError(ValueError):
    """A check that cannot run as asked (unknown Domain or document, malformed cursor)."""


@dataclass(frozen=True, slots=True)
class _Value:
    """A value an accepted extraction item of a checked document put on a branch."""

    cursor: Cursor
    document: DocumentRow
    branch: str
    kind_id: str
    key: str
    attribute_id: str
    value: Any
    evidence: Mapping[str, Any]


@dataclass
class VersionCheckReport:
    """Outcome of one check job, returned as the job result."""

    domain: str
    document: str
    mode: str
    counts: ReconcileReport = field(default_factory=ReconcileReport)
    work: int = 0
    resume: list[str] | None = None
    skipped: str | None = None
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "document": self.document,
            "mode": self.mode,
            "counts": self.counts.as_dict(),
            "work": self.work,
            "resume": self.resume,
            "skipped": self.skipped,
            "duration_ms": self.duration_ms,
        }


def _cursor(raw: Sequence[str] | None) -> Cursor | None:
    if raw is None:
        return None
    try:
        version, job, seq, item, attribute = raw
        return int(version), str(job), int(seq), str(item), str(attribute)
    except (TypeError, ValueError):
        raise VersionCheckError("`resume` must be [version, job, seq, item, attribute]") from None


def _encode(cursor: Cursor) -> list[str]:
    version, job, seq, item, attribute = cursor
    return [str(version), job, str(seq), item, attribute]


def _item_values(item: ItemRow, document: DocumentRow, branch: str, schema: Schema) -> list[_Value]:
    """The values `item` took from `document`, checked Attributes only (module doc)."""
    kind_id = str(item.subject.get("kind") or "")
    kind = schema.kinds.get(kind_id)
    key = item.subject.get("key")
    if kind is None or key is None:
        return []
    pairs: list[tuple[str, Any, Any]] = []
    if item.item_type == VALUE_ITEM:
        pairs.append(
            (str(item.subject.get("attribute") or ""), item.subject.get("value"), item.evidence)
        )
    else:
        values = item.subject.get("values") or {}
        for attribute_id, evidence in (item.subject.get("evidence") or {}).items():
            pairs.append((str(attribute_id), values.get(attribute_id), evidence))
    edits = (item.edits or {}).get("values") if isinstance(item.edits, Mapping) else None
    found: list[_Value] = []
    for attribute_id, value, evidence in pairs:
        if not isinstance(evidence, Mapping) or evidence.get("document_id") != document.id:
            continue
        if attribute_id in kind.primary_key or not compared(kind.attributes.get(attribute_id)):
            continue
        if isinstance(edits, Mapping) and attribute_id in edits:
            value = edits[attribute_id]
        if value is None:
            continue
        found.append(
            _Value(
                cursor=(document.version, item.job_id or "", item.seq, item.item_id, attribute_id),
                document=document,
                branch=branch,
                kind_id=kind.id,
                key=str(key),
                attribute_id=attribute_id,
                value=value,
                evidence=evidence,
            )
        )
    return found


class DocumentVersionCheck:
    """Runs `document_version_check` jobs against one database (module doc)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime: StoreRuntime,
        settings: ExtractionSettings,
        reconciliation: ReconcileOptions,
        *,
        principal: str,
        clock: Callable[[], datetime],
        blobs: BlobStore | None = None,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._settings = settings
        self._reconciliation = reconciliation
        self._principal = principal
        self._clock = clock
        self._blobs = blobs or PostgresBlobStore()

    async def run(
        self,
        domain_id: str,
        document_id: str,
        *,
        mode: str,
        resume: Sequence[str] | None = None,
    ) -> VersionCheckReport:
        if mode not in (VERSION_MODE, RETIRE_MODE):
            raise VersionCheckError(f"unknown mode {mode!r}")
        started = time.monotonic()
        after = _cursor(resume)
        report = VersionCheckReport(domain_id, document_id, mode)
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            domain = await DomainRepository(session).get(domain_id)
            if domain is None:
                raise VersionCheckError(f"no Domain {domain_id!r}")
            options = effective_options(self._reconciliation, domain.settings)
            if not options.enabled or not options.triggers.document_versions:
                report.skipped = "disabled"
                return report
            documents = DocumentRepository(session, domain_id)
            document = await documents.get(document_id)
            if document is None:
                raise VersionCheckError(f"no document {document_id!r} in {domain_id!r}")
            checked = await self._checked(documents, document, mode)
            if not checked:
                report.skipped = "nothing_to_check"
                return report
            values = await self._values(session, domain_id, checked)
            supported = (
                await self._supported(session, domain_id, document)
                if mode == VERSION_MODE
                else set()
            )
        if after is not None:
            values = [v for v in values if v.cursor > after]
        limit = self._settings.versions.max_values_per_job
        if len(values) > limit:
            values = values[:limit]
            report.resume = _encode(values[-1].cursor)
        report.work = len(values)
        if values:
            text = "" if mode == RETIRE_MODE else await self._text(domain_id, document, report)
            detector = Detector(options, principal=self._principal, clock=self._clock)
            await self._check(detector, domain_id, document, mode, values, supported, text, report)
        report.duration_ms = int((time.monotonic() - started) * 1000)
        log.info("document version checked", extra=report.as_dict())
        return report

    async def _checked(
        self, documents: DocumentRepository, document: DocumentRow, mode: str
    ) -> list[DocumentRow]:
        """The documents whose values are checked (module doc), oldest version first."""
        key = document.source_key
        if mode == RETIRE_MODE:
            if key is None:
                return [document]
            newest = await documents.current_version(key)
            if newest is None or newest.id != document.id:
                return [document]
        elif key is None or document.replaces is None:
            return []
        versions = await documents.versions(key, limit=document.version, before=document.version)
        earlier = sorted(versions, key=lambda d: d.version)
        return [*earlier, document] if mode == RETIRE_MODE else earlier

    async def _values(
        self, session: AsyncSession, domain_id: str, checked: Sequence[DocumentRow]
    ) -> list[_Value]:
        """The values the accepted items of the checked documents put on a branch, in cursor
        order; per branch and value the last item (newest version and job) is kept."""
        jobs_repo = ExtractionJobRepository(session, domain_id)
        proposals = ProposalRepository(session, domain_id)
        items = ProposalItemRepository(session, domain_id)
        schemas: dict[str, Schema | None] = {}
        latest: dict[tuple[str, str, str, str], _Value] = {}
        limit = self._settings.versions.max_values_per_job
        for document in checked:
            jobs = await jobs_repo.of_document(document.id, limit=limit)
            branches: dict[str, str] = {}
            for job in jobs:
                if job.proposal_id is None:
                    continue
                proposal = await proposals.get(job.proposal_id)
                if proposal is not None:
                    branches[job.id] = proposal.target_ref
            found = await items.of_jobs(
                branches,
                decisions=(ItemDecision.ACCEPTED,),
                item_types=(VALUE_ITEM, RECORD_ITEM),
            )
            for item in found:
                branch = branches.get(item.job_id or "")
                if branch is None:
                    continue
                if branch not in schemas:
                    schemas[branch] = await self._schema(session, domain_id, branch)
                schema = schemas[branch]
                if schema is None:
                    continue
                for value in _item_values(item, document, branch, schema):
                    slot = (value.branch, value.kind_id, value.key, value.attribute_id)
                    held = latest.get(slot)
                    if held is None or held.cursor < value.cursor:
                        latest[slot] = value
        return sorted(latest.values(), key=lambda v: v.cursor)

    async def _schema(self, session: AsyncSession, domain_id: str, branch: str) -> Schema | None:
        compiled = await MaterializedRepository(
            session, domain_id, branch, writes=self._runtime.settings.writes
        ).schema()
        return None if compiled is None else load_compiled(compiled.compiled)

    async def _supported(
        self, session: AsyncSession, domain_id: str, document: DocumentRow
    ) -> set[tuple[str, str, str, str]]:
        """`(kind id, key, Attribute id, value hash)` of the values the new version's own
        extraction proposes (items not rejected)."""
        jobs = await ExtractionJobRepository(session, domain_id).of_document(
            document.id, limit=self._settings.versions.max_values_per_job
        )
        found = await ProposalItemRepository(session, domain_id).of_jobs(
            [j.id for j in jobs],
            decisions=(ItemDecision.PENDING, ItemDecision.ACCEPTED),
            item_types=(VALUE_ITEM, RECORD_ITEM),
        )
        supported: set[tuple[str, str, str, str]] = set()
        for item in found:
            kind_id = str(item.subject.get("kind") or "")
            key = item.subject.get("key")
            if key is None:
                continue
            if item.item_type == VALUE_ITEM:
                pairs = [(item.subject.get("attribute"), item.subject.get("value"))]
            else:
                pairs = list((item.subject.get("values") or {}).items())
            for attribute_id, value in pairs:
                if value is None or attribute_id is None:
                    continue
                supported.add((kind_id, str(key), str(attribute_id), value_hash(value)))
        return supported

    async def _text(self, domain_id: str, document: DocumentRow, report: VersionCheckReport) -> str:
        """The new version's parsed text: the one extraction stored, or the document parsed
        again; empty when the document was purged or retired in the meantime."""
        if document.status not in (ACTIVE, SUPERSEDED):
            report.counts.count("document_unavailable")
            return ""
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            ref = document.metadata.get(TEXT_REF_KEY)
            if isinstance(ref, str):
                try:
                    return (await self._blobs.get(session, domain_id, ref)).decode("utf-8")
                except BlobNotFoundError:
                    pass
            data = await self._blobs.get(session, domain_id, document.blob_ref)
        parsed = await parse_isolated(
            data,
            document.media_type,
            self._settings.parser,
            max_chars=self._settings.max_document_chars,
        )
        report.counts.count("parsed")
        return parsed.text[: self._settings.max_document_chars]

    async def _check(
        self,
        detector: Detector,
        domain_id: str,
        document: DocumentRow,
        mode: str,
        values: Sequence[_Value],
        supported: set[tuple[str, str, str, str]],
        text: str,
        report: VersionCheckReport,
    ) -> None:
        by_branch: dict[str, list[_Value]] = defaultdict(list)
        for value in values:
            by_branch[value.branch].append(value)
        writes = self._runtime.settings.writes
        to_version = document.version if mode == VERSION_MODE else None
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            raised: list[Raised] = []
            for branch, group in by_branch.items():
                schema = await self._schema(session, domain_id, branch)
                if schema is None:
                    report.counts.count("branch_unavailable", len(group))
                    continue
                rows = MaterializedRepository(session, domain_id, branch, writes=writes)
                by_kind: dict[str, list[_Value]] = defaultdict(list)
                for value in group:
                    by_kind[value.kind_id].append(value)
                for kind_id, kind_values in by_kind.items():
                    current = await current_claims(
                        rows,
                        kind_id,
                        {v.key for v in kind_values},
                        attributes={v.attribute_id for v in kind_values},
                        writes=writes,
                    )
                    kind = schema.kinds.get(kind_id)
                    for value in kind_values:
                        claim = current.claims.get((value.key, value.attribute_id))
                        if (
                            kind is None
                            or claim is None
                            or claim.source != document_identity(value.document.id)
                            or claim.value_hash != value_hash(value.value)
                        ):
                            report.counts.count("not_current")
                            continue
                        slot = (kind_id, value.key, value.attribute_id, claim.value_hash)
                        if slot in supported:
                            report.counts.count("supported")
                            continue
                        quote = value.evidence.get("quote")
                        readable = isinstance(quote, str) and value.document.status != PURGED
                        if readable and locate(str(quote), text) is not None:
                            report.counts.count("present")
                            continue
                        verdict = evidence_gone_verdict(
                            claim,
                            branch=branch,
                            kind_id=kind_id,
                            key=value.key,
                            attribute_id=value.attribute_id,
                            document_id=value.document.id,
                            from_version=value.document.version,
                            to_version=to_version,
                            quote=str(quote) if readable else None,
                            evidence=dict(value.evidence),
                        )
                        detector.collect(verdict, kind.attributes.get(value.attribute_id), raised)
            outcome = await detector.store(session, domain_id, raised)
            report.counts.merge(outcome)


__all__ = [
    "RETIRE_MODE",
    "VERSION_MODE",
    "DocumentVersionCheck",
    "VersionCheckError",
    "VersionCheckReport",
]
