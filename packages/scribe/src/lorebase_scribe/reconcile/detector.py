"""Reconciliation detection in Scribe (design 10.6): the triggers' shared part.

The stages are pure functions of `lorebase_core.reconcile`; a `Detector` applies them with the
options in force for a Domain (`scribe.reconciliation`, overridden by the Domain's
`reconciliation` setting), stores what they raise through `ReconciliationCandidateRepository`
(upsert by fingerprint, in the caller's transaction) and counts the outcomes in
`lorebase.scribe.reconciliation.candidates`.

- Values of derived and vault Attributes are not compared (vault values are references).
- Claims whose source identity cannot be told (a merge resolution or schema migration whose
  carried identity is not known) are left out: they cannot be told apart from any other.
- The semantic stage (d, `lorebase_scribe.reconcile.semantic`): a `SemanticStage` given to the
  detector sees the `contradiction` and `superseded` drafts of `text` Attributes the cheaper
  stages raised, when `semantic.enabled` and the stage is listed, and returns the drafts to
  store (with signals added); those it leaves out are counted `filtered`. Values of vault
  Attributes and of Attributes with any effective sensitivity tag (`load_tags`) are never
  sent to a model.
- Candidates of vault Attributes are stored without evidence quotes (`vault=True`).
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

from lorebase_core.expr.sensitivity import Tags, effective_tags
from lorebase_core.model import Attribute, Kind, Schema, new_ulid
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
from lorebase_store.materialize.sensitivity import reference_tags
from lorebase_store.repositories.reconciliation_candidates import (
    CandidateRow,
    ReconciliationCandidateRepository,
)
from lorebase_store.runtime import StoreRuntime

log = logging.getLogger("lorebase.scribe.reconcile")

OVERRIDE_KEY = "reconciliation"
"""Key of the Domain setting that overrides `scribe.reconciliation`."""

_TEXTUAL = frozenset({AttributeType.STRING, AttributeType.TEXT})
_REVIEWED = frozenset({CandidateType.CONTRADICTION, CandidateType.SUPERSEDED})


DOMAIN_MEMBERS: Mapping[str, frozenset[str] | None] = {
    "enabled": None,
    "stages": None,
    "triggers": frozenset({"extraction", "source_sync", "document_versions"}),
    "hold_auto_accept": None,
    "normalize": frozenset({"numeric_tolerance", "negation_words"}),
    "duplicates": frozenset({"trigram_threshold"}),
    "semantic": frozenset(
        {
            "enabled",
            "equivalence_threshold",
            "equivalent_similarity",
            "duplicate_threshold",
        }
    ),
    "stale": frozenset({"max_age", "source_unseen"}),
}
"""What a Domain's `reconciliation` setting may override: whole members (None) or the listed
members of a section. Files (`normalize.units_file`), work limits (`sweep_*`,
`max_claims_per_candidate`, `duplicates.max_pairs_per_kind`, `normalize.max_quantity_length`),
`merge_check` and `semantic.judge` (model spend) are the operator's only."""


def _domain_override(base: ReconcileOptions, raw: Mapping[str, Any]) -> dict[str, Any]:
    """The members of `raw` a Domain may set, merged into `base`'s dump; the others are
    logged and ignored (module doc of `effective_options`)."""
    merged = base.model_dump(mode="python")
    for name, value in raw.items():
        if name not in DOMAIN_MEMBERS:
            log.warning("the Domain setting %s.%s is not allowed, ignored", OVERRIDE_KEY, name)
            continue
        allowed = DOMAIN_MEMBERS[name]
        if name == "stages":
            # A Domain may leave stages out, never add one the operator left out.
            if not isinstance(value, list | tuple):
                log.warning("the Domain setting %s.stages is not a list, ignored", OVERRIDE_KEY)
                continue
            value = [s for s in value if s in {stage.value for stage in base.stages}]
        elif name == "semantic" and not base.semantic.enabled:
            log.warning("the Domain setting %s.semantic is ignored: the stage is off", OVERRIDE_KEY)
            continue
        if allowed is not None:
            if not isinstance(value, Mapping):
                log.warning(
                    "the Domain setting %s.%s is not an object, ignored", OVERRIDE_KEY, name
                )
                continue
            section = dict(merged[name])
            for member, inner in value.items():
                if member not in allowed:
                    log.warning(
                        "the Domain setting %s.%s.%s is not allowed, ignored",
                        OVERRIDE_KEY,
                        name,
                        member,
                    )
                    continue
                section[member] = inner
            value = section
        candidate = {**merged, name: value}
        try:
            ReconcileOptions.model_validate(candidate)
        except ValidationError as error:
            log.warning(
                "the Domain setting %s.%s is not valid, ignored: %s", OVERRIDE_KEY, name, error
            )
            continue
        merged = candidate
    return merged


def effective_options(
    base: ReconcileOptions, domain_settings: Mapping[str, Any] | None
) -> ReconcileOptions:
    """`base` with the members the Domain's `reconciliation` setting overrides.

    Only the members of `DOMAIN_MEMBERS` are taken; a Domain can leave stages out but not add
    one, and tunes the semantic stage only when the operator turned it on. A member that is
    not allowed or not valid is logged and ignored, the rest of the setting still applies; a
    setting that is not an object is ignored whole."""
    raw = (domain_settings or {}).get(OVERRIDE_KEY)
    if raw is None:
        return base
    if not isinstance(raw, Mapping):
        log.warning("the Domain setting %s is not an object, ignored", OVERRIDE_KEY)
        return base
    if not raw:
        return base
    found = ReconcileOptions.model_validate(_domain_override(base, raw))
    return base if found == base else found


def compared(attribute: Attribute | None) -> bool:
    """Whether values of `attribute` are compared at all (module doc)."""
    return attribute is not None and not attribute.is_derived and not attribute.in_vault


def shareable(attribute: Attribute | None, tags: Mapping[str, Tags]) -> bool:
    """Whether values of `attribute` may be sent to a model: not in the vault and without any
    sensitivity tag, its own or effective (`tags`, as extraction reads them). Unknown
    Attributes are not."""
    return (
        attribute is not None
        and not attribute.in_vault
        and not attribute.sensitivity
        and not tags.get(attribute.id)
    )


class SemanticStage(Protocol):
    """Stage d of design 10.6: compares competing ``text`` values and returns the drafts to
    store, each with its signals (``equivalent``, ``similarity``, ``judge``) added; it may drop
    a draft only above ``semantic.equivalence_threshold``. The detector hands it the value
    candidates of ``text`` Attributes that are `shareable` only; `attributes` (by id) and
    `tags` (effective sensitivity tags by Attribute id) let it check that again: a draft whose
    Attribute is not shareable must go on unchanged, its values sent to no model."""

    async def review(
        self,
        drafts: Sequence[CandidateDraft],
        options: ReconcileOptions,
        *,
        attributes: Mapping[str, Attribute],
        tags: Mapping[str, Tags],
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
        self._tags: Mapping[str, Tags] | None = None

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

    @property
    def semantic(self) -> bool:
        """Whether the semantic stage runs for these options."""
        return self._semantic is not None and self.runs(Stage.SEMANTIC)

    async def load_tags(
        self,
        session: AsyncSession,
        schema: Schema,
        *,
        domain_id: str,
        branch: str,
        runtime: StoreRuntime,
    ) -> None:
        """Read the effective sensitivity tags of `schema` on `branch` (its own, the default
        branch's and those carried through Derivations), which the semantic stage needs: without
        them it sends nothing to a model. A no-op when the stage does not run."""
        if not self.semantic:
            return
        reference = await reference_tags(session, domain_id, branch, runtime=runtime)
        self._tags = effective_tags(schema, reference)

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
        drafts = await self._review([r.draft for r in raised], attributes, report)
        repository = ReconciliationCandidateRepository(session, domain_id)
        now = self._clock()
        for draft in drafts:
            attribute = attributes.get(draft.fingerprint)
            outcome = await repository.upsert(
                draft,
                sensitivity=() if attribute is None else attribute.sensitivity,
                vault=attribute is not None and attribute.in_vault,
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

    async def _review(
        self,
        drafts: list[CandidateDraft],
        attributes: Mapping[bytes, Attribute | None],
        report: ReconcileReport,
    ) -> list[CandidateDraft]:
        """The drafts after the semantic stage: it sees the value candidates of `text`
        Attributes that are `shareable` only (none before `load_tags`); those it leaves out are
        counted `filtered`. A stage that fails leaves the drafts as they were."""
        if self._semantic is None or not self.runs(Stage.SEMANTIC):
            return drafts
        tags = self._tags
        if tags is None:
            log.warning("semantic reconciliation stage skipped: sensitivity tags not loaded")
            return drafts
        reviewed = [
            d
            for d in drafts
            if d.type in _REVIEWED
            and (attribute := attributes.get(d.fingerprint)) is not None
            and attribute.type.name is AttributeType.TEXT
            and shareable(attribute, tags)
        ]
        if not reviewed:
            return drafts
        by_id = {a.id: a for d in reviewed if (a := attributes.get(d.fingerprint)) is not None}
        try:
            returned = await self._semantic.review(
                reviewed, self.options, attributes=by_id, tags=tags
            )
        except Exception as error:
            log.warning("semantic reconciliation stage failed, no signal: %s", error)
            return drafts
        asked = {d.fingerprint for d in reviewed}
        kept = {d.fingerprint: d for d in returned if d.fingerprint in asked}
        found: list[CandidateDraft] = []
        for draft in drafts:
            if draft.fingerprint not in asked:
                found.append(draft)
            elif draft.fingerprint in kept:
                found.append(kept[draft.fingerprint])
            else:
                report.count("filtered")
                RECONCILIATION_CANDIDATES.add(1, {"type": draft.type.value, "outcome": "filtered"})
        return found

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
    "DOMAIN_MEMBERS",
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
    "shareable",
]
