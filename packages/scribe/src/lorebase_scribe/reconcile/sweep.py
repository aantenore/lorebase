"""The `reconcile_sweep` job (design 10.6, trigger 3): one pass over one branch of a Domain.

A pass has two phases, each resumable:

1. **Open candidates** of the branch, newest first: one whose disagreement left the data is
   marked `obsolete` with the reason: a Record it names was deleted (`record_deleted`), its
   Attribute left the schema (`attribute_removed`), the Proposal holding a competing claim was
   closed (`proposal_closed`) or its item rejected (`item_rejected`) and no disagreement is left
   without them, the claims compare equal under the current options (`claims_converged`), or a
   `stale` value was replaced since (`value_replaced`).
2. **Records** of each Kind in `(kind, key)` order, page by page
   (`store.materialize.read_batch_rows`): values in `mat_history` that replaced a value of
   another source identity (stages b and c over both claims); current values older than
   `stale.max_age` or whose period ended (`stale`); duplicates (equal folded unique or title
   values once per Kind, similar titles per Record and, with the semantic stage, stored Record
   vectors at least `semantic.duplicate_threshold` similar; at most
   `duplicates.max_pairs_per_kind` pairs per Kind and job).

Work is counted in candidates examined, Records read, history changes read and title
statements run; past `sweep_max_changes_per_job` the job stops after the current page and
returns the cursor (`resume`) a continuation starts from, as embedding passes do (9.3). Every
candidate is upserted by fingerprint, so a sweep can run again at will. At the end the open
candidates of the Domain are counted for `lorebase.scribe.reconciliation.open`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Kind, Schema
from lorebase_core.reconcile import (
    CandidateStatus,
    CandidateType,
    Claim,
    ReconcileOptions,
    Stage,
    stale_verdict,
)
from lorebase_scribe.reconcile.detector import (
    Detector,
    Raised,
    ReconcileReport,
    compared,
    effective_options,
)
from lorebase_scribe.reconcile.duplicates import normalized_pairs, trigram_pairs
from lorebase_scribe.reconcile.semantic import EmbeddingSemanticStage
from lorebase_scribe.telemetry import OPEN_CANDIDATES, RECONCILIATION_SWEEP_DURATION, tracer
from lorebase_store.commit.schema import load_compiled
from lorebase_store.models.enums import ItemDecision, ProposalStatus
from lorebase_store.reconciliation import current_claims, open_counts, record_history
from lorebase_store.repositories import (
    DomainRepository,
    MaterializedRepository,
    ProposalItemRepository,
    ProposalRepository,
    scope_to_domain,
)
from lorebase_store.repositories.reconciliation_candidates import (
    CandidateFilter,
    CandidateRow,
    ReconciliationCandidateRepository,
)
from lorebase_store.runtime import StoreRuntime

log = logging.getLogger("lorebase.scribe.reconcile")

CANDIDATES_PHASE = "candidates"
RECORDS_PHASE = "records"


class SweepError(ValueError):
    """A sweep that cannot run as asked (unknown Domain or branch, malformed cursor)."""


@dataclass
class SweepReport:
    """Outcome of one sweep job, returned as the job result."""

    domain: str
    branch: str
    counts: ReconcileReport = field(default_factory=ReconcileReport)
    work: int = 0
    resume: list[str] | None = None
    skipped: str | None = None
    duration_ms: int = 0

    @property
    def complete(self) -> bool:
        return self.resume is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "branch": self.branch,
            "counts": self.counts.as_dict(),
            "work": self.work,
            "resume": self.resume,
            "skipped": self.skipped,
            "duration_ms": self.duration_ms,
        }


def _unwrapped(value: Any) -> Any:
    """A stored claim value without its sensitivity wrapper."""
    if isinstance(value, Mapping) and set(value) == {"$sensitive"}:
        inner = value["$sensitive"]
        if isinstance(inner, Mapping):
            return inner.get("value")
    return value


def stored_claims(row: CandidateRow) -> list[Claim]:
    """The claims of a stored candidate (values unwrapped); unreadable ones are left out."""
    found: list[Claim] = []
    for document in row.claims:
        try:
            found.append(Claim.model_validate({**document, "value": _unwrapped(document["value"])}))
        except (ValueError, KeyError):
            continue
    return found


class ReconcileSweep:
    """Runs `reconcile_sweep` jobs against one database."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runtime: StoreRuntime,
        options: ReconcileOptions,
        *,
        principal: str,
        clock: Callable[[], datetime],
        semantic: EmbeddingSemanticStage | None = None,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._options = options
        self._principal = principal
        self._clock = clock
        self._semantic = semantic

    async def run(
        self, domain_id: str, *, branch: str | None = None, resume: Sequence[str] | None = None
    ) -> SweepReport:
        started = time.monotonic()
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, domain_id)
            domain = await DomainRepository(session).get(domain_id)
            if domain is None:
                raise SweepError(f"no Domain {domain_id!r}")
            name = branch or domain.default_branch
            compiled = await MaterializedRepository(
                session, domain_id, name, writes=self._runtime.settings.writes
            ).schema()
            if compiled is None:
                raise SweepError(f"branch {name!r} of {domain_id!r} is not materialized")
        report = SweepReport(domain=domain_id, branch=name)
        options = effective_options(self._options, domain.settings)
        if not options.enabled:
            report.skipped = "disabled"
            return report
        detector = Detector(
            options, principal=self._principal, clock=self._clock, semantic=self._semantic
        )
        schema = load_compiled(compiled.compiled)
        if detector.semantic:
            async with self._sessions() as session, session.begin():
                await scope_to_domain(session, domain_id)
                await detector.load_tags(
                    session, schema, domain_id=domain_id, branch=name, runtime=self._runtime
                )
        with tracer.start_as_current_span(
            "scribe.reconcile_sweep",
            attributes={"lorebase.domain": domain_id, "lorebase.branch": name},
        ) as span:
            phase, position = _cursor(resume)
            if phase == CANDIDATES_PHASE:
                after = (
                    None if position is None else (datetime.fromisoformat(position[0]), position[1])
                )
                report.resume = await self._candidates(detector, schema, report, after)
                position = None
            if report.resume is None:
                report.resume = await self._records(detector, schema, report, position)
            span.set_attribute("lorebase.complete", report.complete)
        if report.complete:
            async with self._sessions() as session, session.begin():
                await scope_to_domain(session, domain_id)
                OPEN_CANDIDATES.update(domain_id, await open_counts(session, domain_id))
        elapsed = time.monotonic() - started
        report.duration_ms = int(elapsed * 1000)
        RECONCILIATION_SWEEP_DURATION.record(elapsed)
        log.info(
            "reconciliation sweep",
            extra={
                "domain": domain_id,
                "branch": name,
                "work": report.work,
                "complete": report.complete,
                **report.counts.as_dict(),
            },
        )
        return report

    def _spent(self, report: SweepReport) -> bool:
        return report.work >= self._options.sweep_max_changes_per_job

    # Phase 1: open candidates ---------------------------------------------------------------

    async def _candidates(
        self,
        detector: Detector,
        schema: Schema,
        report: SweepReport,
        after: tuple[datetime, str] | None,
    ) -> list[str] | None:
        batch = self._runtime.settings.materialize.read_batch_rows
        while True:
            async with self._sessions() as session, session.begin():
                await scope_to_domain(session, report.domain)
                repository = ReconciliationCandidateRepository(session, report.domain)
                rows = await repository.page(
                    CandidateFilter(branch=report.branch, status=CandidateStatus.OPEN),
                    limit=batch,
                    after=after,
                )
                for row in rows:
                    reason = await self._obsolete(session, detector, schema, report, row)
                    if reason is not None and await detector.retire(
                        session, report.domain, row, reason
                    ):
                        report.counts.count("obsolete")
                report.work += len(rows)
            if len(rows) < batch:
                return None
            after = (rows[-1].created_at, rows[-1].id)
            if self._spent(report):
                return [CANDIDATES_PHASE, after[0].isoformat(), after[1]]

    async def _obsolete(
        self,
        session: AsyncSession,
        detector: Detector,
        schema: Schema,
        report: SweepReport,
        row: CandidateRow,
    ) -> str | None:
        """Why the candidate's disagreement left the data, if it did (module doc)."""
        kind = schema.kinds.get(row.kind_id)
        if kind is None:
            return "record_deleted"
        rows = MaterializedRepository(
            session, report.domain, report.branch, writes=self._runtime.settings.writes
        )
        if row.type == CandidateType.DUPLICATE.value:
            found = await rows.records(kind.id, row.keys)
            return None if len(found) == len(set(row.keys)) else "record_deleted"
        attribute = kind.attributes.get(row.attribute_id or "")
        if attribute is None:
            return "attribute_removed"
        key = row.keys[0]
        if row.type == CandidateType.STALE.value:
            current = await current_claims(
                rows,
                kind.id,
                [key],
                attributes=[attribute.id],
                writes=self._runtime.settings.writes,
            )
            if key not in current.records:
                return "record_deleted"
            now = current.claims.get((key, attribute.id))
            claims = stored_claims(row)
            if now is None or not claims or now.provenance_id != claims[0].provenance_id:
                return "value_replaced"
            return None
        if not await rows.records(kind.id, [key]):
            return "record_deleted"
        live, dropped = await self._live(session, report.domain, stored_claims(row))
        verdict = detector.verdict(
            live, branch=report.branch, kind=kind, key=key, attribute=attribute
        )
        if verdict.raised:
            return None
        return dropped or "claims_converged"

    async def _live(
        self, session: AsyncSession, domain_id: str, claims: Sequence[Claim]
    ) -> tuple[list[Claim], str | None]:
        """The claims still standing, and why the others fell (closed Proposal, rejected
        item)."""
        live: list[Claim] = []
        reason: str | None = None
        proposals = ProposalRepository(session, domain_id)
        items = ProposalItemRepository(session, domain_id)
        decisions: dict[str, dict[str, ItemDecision]] = {}
        for claim in claims:
            ref = claim.proposal_item
            if ref is None:
                live.append(claim)
                continue
            proposal = await proposals.get(ref.proposal_id)
            if proposal is None or proposal.status is ProposalStatus.CLOSED:
                reason = reason or "proposal_closed"
                continue
            if ref.proposal_id not in decisions:
                decisions[ref.proposal_id] = {
                    i.item_id: i.decision for i in await items.all(ref.proposal_id)
                }
            if decisions[ref.proposal_id].get(ref.item_id) is ItemDecision.REJECTED:
                reason = reason or "item_rejected"
                continue
            live.append(claim)
        return live, reason

    # Phase 2: Records ---------------------------------------------------------------------------

    async def _records(
        self,
        detector: Detector,
        schema: Schema,
        report: SweepReport,
        position: tuple[str, str] | None,
    ) -> list[str] | None:
        batch = self._runtime.settings.materialize.read_batch_rows
        for kind_id in sorted(schema.kinds):
            if position is not None and kind_id < position[0]:
                continue
            kind = schema.kinds[kind_id]
            after = (
                (position[1] or None) if position is not None and kind_id == position[0] else None
            )
            position = None
            pairs_left = detector.options.duplicates.max_pairs_per_kind
            if after is None and detector.runs(Stage.IDENTITY):
                pairs_left -= await self._normalized(detector, kind, report, pairs_left)
            while True:
                async with self._sessions() as session, session.begin():
                    await scope_to_domain(session, report.domain)
                    rows = MaterializedRepository(
                        session, report.domain, report.branch, writes=self._runtime.settings.writes
                    )
                    page = await rows.record_page(kind.id, after=after, limit=batch)
                    if not page:
                        break
                    raised, work = await self._page(
                        session, detector, rows, kind, [r.key for r in page], report
                    )
                    if detector.runs(Stage.IDENTITY) and pairs_left > 0:
                        similar, statements = await trigram_pairs(
                            session,
                            self._runtime,
                            kind,
                            ((r.key, r.title) for r in page),
                            domain_id=report.domain,
                            read_branch=report.branch,
                            branch=report.branch,
                            threshold=detector.options.duplicates.trigram_threshold,
                            limit=pairs_left,
                        )
                        raised.extend(similar)
                        pairs_left -= len(similar)
                        work += statements
                    if (
                        self._semantic is not None
                        and detector.runs(Stage.SEMANTIC)
                        and pairs_left > 0
                    ):
                        close = await self._semantic.duplicates(
                            session,
                            kind,
                            [r.key for r in page],
                            domain_id=report.domain,
                            read_branch=report.branch,
                            branch=report.branch,
                            threshold=detector.options.semantic.duplicate_threshold,
                            limit=pairs_left,
                        )
                        raised.extend(close)
                        pairs_left -= len(close)
                        work += 1
                    report.counts.merge(await detector.store(session, report.domain, raised))
                    report.work += work + len(page)
                after = page[-1].key
                if len(page) < batch:
                    break
                if self._spent(report):
                    return [RECORDS_PHASE, kind.id, after]
            if self._spent(report):
                following = [k for k in sorted(schema.kinds) if k > kind_id]
                return None if not following else [RECORDS_PHASE, following[0], ""]
        return None

    async def _normalized(
        self, detector: Detector, kind: Kind, report: SweepReport, limit: int
    ) -> int:
        async with self._sessions() as session, session.begin():
            await scope_to_domain(session, report.domain)
            rows = MaterializedRepository(
                session, report.domain, report.branch, writes=self._runtime.settings.writes
            )
            raised = await normalized_pairs(
                session, await rows.layout(), kind, branch=report.branch, limit=limit
            )
            report.counts.merge(await detector.store(session, report.domain, raised))
        report.work += 1
        return len(raised)

    async def _page(
        self,
        session: AsyncSession,
        detector: Detector,
        rows: MaterializedRepository,
        kind: Kind,
        keys: list[str],
        report: SweepReport,
    ) -> tuple[list[Raised], int]:
        """Candidates of one page of Records: replaced values and stale values; also the
        history changes read."""
        writes = self._runtime.settings.writes
        attributes = {a.id: a for a in kind.attributes.values() if compared(a)}
        raised: list[Raised] = []
        steps = 0
        if attributes and detector.runs(Stage.VALUES):
            history = await record_history(
                rows,
                kind.id,
                keys,
                attributes=attributes,
                writes=writes,
                batch=self._runtime.settings.materialize.read_batch_rows,
            )
            steps = history.steps
            for change in history.replacements:
                if change.before.source == change.after.source:
                    continue
                verdict = detector.verdict(
                    [change.before, change.after],
                    branch=report.branch,
                    kind=kind,
                    key=change.key,
                    attribute=attributes[change.attribute_id],
                )
                detector.collect(verdict, attributes[change.attribute_id], raised)
        max_age = detector.options.stale.max_age_delta
        aging = {a_id: a for a_id, a in attributes.items() if max_age is not None or a.valid_time}
        if aging:
            now = self._clock()
            current = await current_claims(rows, kind.id, keys, attributes=aging, writes=writes)
            for (key, attribute_id), claim in current.claims.items():
                verdict = stale_verdict(
                    claim,
                    branch=report.branch,
                    kind_id=kind.id,
                    key=key,
                    attribute_id=attribute_id,
                    now=now,
                    max_age=max_age,
                )
                detector.collect(verdict, aging[attribute_id], raised)
        return raised, steps


def _cursor(resume: Sequence[str] | None) -> tuple[str, Any]:
    """The phase and position a continuation starts from."""
    if not resume:
        return CANDIDATES_PHASE, None
    if len(resume) != 3 or resume[0] not in (CANDIDATES_PHASE, RECORDS_PHASE):
        raise SweepError("`resume` must be [phase, position, position]")
    if resume[0] == CANDIDATES_PHASE:
        try:
            datetime.fromisoformat(resume[1])
        except ValueError:
            raise SweepError("`resume` holds no candidate time") from None
    return resume[0], (resume[1], resume[2])


__all__ = [
    "CANDIDATES_PHASE",
    "RECORDS_PHASE",
    "ReconcileSweep",
    "SweepError",
    "SweepReport",
    "stored_claims",
]
