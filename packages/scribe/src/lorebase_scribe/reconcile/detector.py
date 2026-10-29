"""Reconciliation detection in Scribe (design 10.6): the triggers' shared part.

The stages are pure functions of `lorebase_core.reconcile`; a `Detector` applies them with the
options in force for a Domain (`scribe.reconciliation`, overridden by the Domain's
`reconciliation` setting), stores what they raise through `ReconciliationCandidateRepository`
(upsert by fingerprint, in the caller's transaction) and counts the outcomes in
`lorebase.scribe.reconciliation.candidates`.

- Values of derived and vault Attributes are not compared (vault values are references).
- Claims whose source identity cannot be told (a merge resolution or schema migration whose
  carried identity is not known) are left out: they cannot be told apart from any other.
- The semantic stage (d) is an extension point: a `SemanticStage` given to the detector sees
  the drafts the cheaper stages raised, when `semantic.enabled` and the stage is listed, and
  returns the drafts to store (with signals added). None ships yet, so no model is called.
- `obsolete` is set only by `retire`, with the reason the disagreement left the data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_core.model import Attribute, Kind, new_ulid
from lorebase_core.reconcile import (
    CANDIDATE_ID_PREFIX,
    CandidateDraft,
    CandidateType,
    Claim,
    DuplicateRule,
    IdentityKind,
    Outcome,
    ReconcileOptions,
    Stage,
    UnitTable,
    Verdict,
    duplicate_draft,
    identity,
    value_verdict,
)
from lorebase_core.types import AttributeType
from lorebase_scribe.telemetry import RECONCILIATION_CANDIDATES
from lorebase_store.repositories.reconciliation_candidates import (
    CandidateRow,
    ReconciliationCandidateRepository,
)

log = logging.getLogger("lorebase.scribe.reconcile")

OVERRIDE_KEY = "reconciliation"
"""Key of the Domain setting that overrides `scribe.reconciliation`."""

_TEXTUAL = frozenset({AttributeType.STRING, AttributeType.TEXT})


def effective_options(
    base: ReconcileOptions, domain_settings: Mapping[str, Any] | None
) -> ReconcileOptions:
    """`base` with the members the Domain's `reconciliation` setting overrides. A setting that
    is not valid is logged and ignored: detection then runs with `base`."""
    raw = (domain_settings or {}).get(OVERRIDE_KEY)
    if raw is None:
        return base
    try:
        if not isinstance(raw, Mapping):
            raise ValueError("must be an object")
        return ReconcileOptions.model_validate({**base.model_dump(mode="python"), **raw})
    except (ValidationError, ValueError) as error:
        log.warning("the Domain setting %s is not valid, ignored: %s", OVERRIDE_KEY, error)
        return base


def compared(attribute: Attribute | None) -> bool:
    """Whether values of `attribute` are compared at all (module doc)."""
    return attribute is not None and not attribute.is_derived and not attribute.in_vault


class SemanticStage(Protocol):
    """Stage d of design 10.6: compares competing ``text`` values (and Record vectors for
    duplicates) and returns the drafts to store, each with its signals (``equivalent``,
    ``similarity``, ``judge``) added; it may drop a draft only above
    ``semantic.equivalence_threshold``."""

    async def review(
        self, drafts: Sequence[CandidateDraft], options: ReconcileOptions
    ) -> list[CandidateDraft]: ...


@dataclass
class ReconcileReport:
    """Outcomes of one trigger or sweep, by name (`raised`, `claim_added`, `filtered`,
    `obsolete`, `held`, ...)."""

    counts: dict[str, int] = field(default_factory=dict)

    def count(self, name: str, n: int = 1) -> None:
        if n:
            self.counts[name] = self.counts.get(name, 0) + n

    def merge(self, other: ReconcileReport) -> None:
        for name, n in other.counts.items():
            self.count(name, n)

    def as_dict(self) -> dict[str, int]:
        return dict(self.counts)


@dataclass(frozen=True, slots=True)
class Raised:
    """A draft to store and the Attribute whose values it holds (for their sensitivity)."""

    draft: CandidateDraft
    attribute: Attribute | None = None


def new_candidate_id() -> str:
    return f"{CANDIDATE_ID_PREFIX}_{new_ulid()}"


class Detector:
    """The stages with one Domain's options, and the store of what they raise."""

    def __init__(
        self,
        options: ReconcileOptions,
        *,
        principal: str,
        clock: Callable[[], datetime],
        semantic: SemanticStage | None = None,
    ) -> None:
        self.options = options
        self._principal = principal
        self._clock = clock
        self._semantic = semantic
        self._units: UnitTable | None = None

    @property
    def units(self) -> UnitTable:
        if self._units is None:
            self._units = UnitTable.load(self.options.normalize.units_file)
        return self._units

    @property
    def enabled(self) -> bool:
        return self.options.enabled

    def runs(self, stage: Stage) -> bool:
        return self.options.runs(stage)

    def verdict(
        self,
        claims: Iterable[Claim],
        *,
        branch: str,
        kind: Kind,
        key: str,
        attribute: Attribute,
        proposal_id: str | None = None,
    ) -> Verdict:
        """Stages b and c over the claims of one value (module doc for what is left out)."""
        known = [c for c in claims if c.source is not None]
        return value_verdict(
            known,
            branch=branch,
            kind_id=kind.id,
            key=key,
            attribute_id=attribute.id,
            valid_time=bool(attribute.valid_time),
            textual=attribute.type.name in _TEXTUAL,
            options=self.options,
            units=self.units if self.runs(Stage.NORMALIZE) else None,
            proposal_id=proposal_id,
        )

    def collect(
        self, verdict: Verdict, attribute: Attribute | None, raised: list[Raised]
    ) -> Verdict:
        """Add a raised draft to `raised`; count a filtered one at once."""
        if verdict.draft is not None:
            raised.append(Raised(verdict.draft, attribute))
        elif verdict.outcome is Outcome.FILTERED:
            RECONCILIATION_CANDIDATES.add(
                1, {"type": CandidateType.CONTRADICTION.value, "outcome": "filtered"}
            )
        return verdict

    async def store(
        self, session: AsyncSession, domain_id: str, raised: Sequence[Raised]
    ) -> ReconcileReport:
        """Upsert the drafts in the caller's transaction (after `scope_to_domain`)."""
        report = ReconcileReport()
        if not raised:
            return report
        attributes = {r.draft.fingerprint: r.attribute for r in raised}
        drafts = [r.draft for r in raised]
        if self._semantic is not None and self.runs(Stage.SEMANTIC):
            drafts = await self._semantic.review(drafts, self.options)
        repository = ReconciliationCandidateRepository(session, domain_id)
        now = self._clock()
        for draft in drafts:
            attribute = attributes.get(draft.fingerprint)
            outcome = await repository.upsert(
                draft,
                sensitivity=() if attribute is None else attribute.sensitivity,
                max_claims=self.options.max_claims_per_candidate,
                now=now,
                new_id=new_candidate_id,
            )
            if outcome.created:
                name = "raised"
            elif outcome.claims_added:
                name = "claim_added"
            else:
                name = "seen"
            report.count(name)
            if name != "seen":
                RECONCILIATION_CANDIDATES.add(1, {"type": draft.type.value, "outcome": name})
        return report

    async def retire(
        self, session: AsyncSession, domain_id: str, row: CandidateRow, reason: str
    ) -> bool:
        """Mark an open candidate obsolete with `reason`; false when it is no longer open."""
        repository = ReconciliationCandidateRepository(session, domain_id)
        current = await repository.get(row.id, for_update=True)
        if current is None or current.status != "open":
            return False
        await repository.mark_obsolete(row.id, reason=reason, by=self._principal, at=self._clock())
        RECONCILIATION_CANDIDATES.add(1, {"type": row.type, "outcome": "obsolete"})
        return True


def document_identity(document_id: str) -> str:
    return identity(IdentityKind.DOCUMENT, document_id)


def ambiguous_duplicates(branch: str, pairs: Iterable[tuple[str, str, str, float]]) -> list[Raised]:
    """`duplicate` drafts for extraction mentions left ambiguous between two Records:
    `(kind id, key, other key, score)` each."""
    found: dict[bytes, Raised] = {}
    for kind_id, first, second, score in pairs:
        if first == second:
            continue
        draft = duplicate_draft(
            branch=branch,
            kind_id=kind_id,
            keys=(first, second),
            rule=DuplicateRule.AMBIGUOUS,
            score=score,
        )
        found.setdefault(draft.fingerprint, Raised(draft))
    return list(found.values())


__all__ = [
    "OVERRIDE_KEY",
    "Detector",
    "Raised",
    "ReconcileReport",
    "SemanticStage",
    "ambiguous_duplicates",
    "compared",
    "document_identity",
    "effective_options",
    "new_candidate_id",
]
