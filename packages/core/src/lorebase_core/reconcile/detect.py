"""Detection stages of reconciliation (design 10.6, stages a to c), as pure functions.

The triggers in Scribe gather claims (from provenance, Proposal items and branch history) and
hand them here; what comes back is a verdict and, when the claims disagree, the candidate to
upsert. Nothing here reads a database or calls a model: the semantic stage (d) is applied by the
caller to the drafts these functions return.

- **Values** (stage b). Claims of one Record and Attribute disagree when two of them hold
  different canonical values from different source identities (`disagree`); equal values from
  several identities are agreement.
- **Normalization** (stage c, ``string`` and ``text`` Attributes). Claims whose values compare
  equal once normalized are grouped (`normalize_claims`); when no two groups disagree any more
  the candidate is ``filtered``. Signals found (``unit``, ``negation``) go with the candidate.
- **Classification.** On a ``valid_time`` Attribute, claims with disjoint periods are
  ``superseded``; otherwise ``contradiction`` (`classify`).
- **Stale.** A current value older than ``stale.max_age``, whose period has ended, or whose
  source identity has a newer pending claim.
- **Duplicates** (stage a). Two Records of one Kind that look alike: one candidate per pair of
  keys, whatever rule found it (the rules are recorded in ``signals``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import combinations

from pydantic import JsonValue

from lorebase_core.reconcile.candidates import (
    CandidateDraft,
    CandidateType,
    Signal,
    classify,
    disagree,
)
from lorebase_core.reconcile.claims import Claim
from lorebase_core.reconcile.normalize import UnitTable, normalize_claims
from lorebase_core.reconcile.options import ReconcileOptions, Stage


class Outcome(StrEnum):
    """What the stages made of a set of claims."""

    AGREE = "agree"
    """No disagreement: nothing is raised."""
    FILTERED = "filtered"
    """The values differ, but not once normalized: nothing is raised."""
    RAISED = "raised"
    """A candidate is raised."""


@dataclass(frozen=True, slots=True)
class Verdict:
    outcome: Outcome
    draft: CandidateDraft | None = None

    @property
    def raised(self) -> bool:
        return self.outcome is Outcome.RAISED


AGREE = Verdict(Outcome.AGREE)
FILTERED = Verdict(Outcome.FILTERED)


class DuplicateRule(StrEnum):
    """How a duplicate pair was found (recorded in the candidate's ``signals``)."""

    NORMALIZED = "normalized"
    """Equal on a unique or title Attribute once case and spacing are folded."""
    TRIGRAM = "trigram"
    """Titles above ``duplicates.trigram_threshold`` by trigram similarity."""
    AMBIGUOUS = "ambiguous"
    """An extraction mention left ambiguous between the two Records."""
    SEMANTIC = "semantic"
    """Stored Record vectors above ``semantic.duplicate_threshold`` (stage d)."""


class StaleReason(StrEnum):
    MAX_AGE = "max_age"
    PERIOD_ENDED = "period_ended"
    PENDING_CLAIM = "pending_claim"


def _groups_disagree(groups: Sequence[Sequence[Claim]]) -> bool:
    """Whether two claims of different groups come from different (or unknown) identities."""
    for first, second in combinations(groups, 2):
        for a in first:
            for b in second:
                if a.source is None or b.source is None or a.source != b.source:
                    return True
    return False


def value_verdict(
    claims: Iterable[Claim],
    *,
    branch: str,
    kind_id: str,
    key: str,
    attribute_id: str,
    valid_time: bool,
    textual: bool,
    options: ReconcileOptions,
    units: UnitTable | None = None,
    proposal_id: str | None = None,
) -> Verdict:
    """Stages b and c over the claims of one Record and Attribute (module doc). `textual`:
    the Attribute is ``string`` or ``text``, so the normalization stage applies; `units` is
    the unit table it reads (the table shipped with ``lorebase_core`` when left out)."""
    listed = list({c.claim_id: c for c in claims}.values())
    if not options.runs(Stage.VALUES) or not disagree(listed):
        return AGREE
    signals: dict[str, JsonValue] = {}
    if textual and options.runs(Stage.NORMALIZE):
        table = units if units is not None else UnitTable.load(options.normalize.units_file)
        normalized = normalize_claims(listed, options=options.normalize, units=table)
        if not _groups_disagree(normalized.groups):
            return FILTERED
        signals.update(dict.fromkeys(sorted(normalized.signals), True))
    kind = classify(listed, valid_time=valid_time)
    if kind is CandidateType.SUPERSEDED:
        signals[Signal.PERIODS.value] = True
    draft = CandidateDraft(
        type=kind,
        branch=branch,
        kind_id=kind_id,
        keys=(key,),
        attribute_id=attribute_id,
        claims=tuple(listed),
        signals=signals,
        proposal_id=proposal_id,
    )
    return Verdict(Outcome.RAISED, draft)


def stale_reasons(
    current: Claim,
    *,
    now: datetime,
    max_age: timedelta | None,
    pending: Iterable[Claim] = (),
) -> list[StaleReason]:
    """Why the current value is stale, if it is: older than `max_age` (by provenance time),
    its period ended before `now`, or a newer `pending` claim of its source identity holds
    another value."""
    reasons: list[StaleReason] = []
    if max_age is not None and now - current.written_at > max_age:
        reasons.append(StaleReason.MAX_AGE)
    if current.valid_to is not None and datetime.fromisoformat(current.valid_to) <= now:
        reasons.append(StaleReason.PERIOD_ENDED)
    if current.source is not None and any(
        p.source == current.source and p.value_hash != current.value_hash and p.at > current.at
        for p in pending
    ):
        reasons.append(StaleReason.PENDING_CLAIM)
    return reasons


def stale_verdict(
    current: Claim,
    *,
    branch: str,
    kind_id: str,
    key: str,
    attribute_id: str,
    now: datetime,
    max_age: timedelta | None,
    pending: Sequence[Claim] = (),
    proposal_id: str | None = None,
) -> Verdict:
    """A ``stale`` candidate for the current value (`stale_reasons`); its claims are the
    current one and the newer pending claims of its source identity."""
    reasons = stale_reasons(current, now=now, max_age=max_age, pending=pending)
    if not reasons:
        return AGREE
    newer = [
        p
        for p in pending
        if p.source == current.source and p.value_hash != current.value_hash and p.at > current.at
    ]
    draft = CandidateDraft(
        type=CandidateType.STALE,
        branch=branch,
        kind_id=kind_id,
        keys=(key,),
        attribute_id=attribute_id,
        claims=(current, *newer),
        signals={"stale": [r.value for r in reasons]},
        proposal_id=proposal_id,
    )
    return Verdict(Outcome.RAISED, draft)


def duplicate_draft(
    *,
    branch: str,
    kind_id: str,
    keys: tuple[str, str],
    rule: DuplicateRule,
    attribute_id: str | None = None,
    score: float | None = None,
    proposal_id: str | None = None,
) -> CandidateDraft:
    """A ``duplicate`` candidate for a pair of keys. Its fingerprint names the pair only, so
    every rule that finds the pair folds into one candidate; the rule, the Attribute it
    compared and the similarity go to ``signals``."""
    if keys[0] == keys[1]:
        raise ValueError("a duplicate pair needs two keys")
    detail: dict[str, JsonValue] = {}
    if attribute_id is not None:
        detail["attribute"] = attribute_id
    if score is not None:
        detail[Signal.SIMILARITY.value] = round(score, 4)
    return CandidateDraft(
        type=CandidateType.DUPLICATE,
        branch=branch,
        kind_id=kind_id,
        keys=keys,
        signals={rule.value: detail or True},
        proposal_id=proposal_id,
    )


def pairs(keys: Sequence[str], *, limit: int) -> list[tuple[str, str]]:
    """Distinct unordered pairs of `keys` (sorted within a pair), at most `limit`."""
    found: list[tuple[str, str]] = []
    for first, second in combinations(sorted(set(keys)), 2):
        if len(found) >= limit:
            break
        found.append((first, second))
    return found


__all__ = [
    "AGREE",
    "FILTERED",
    "DuplicateRule",
    "Outcome",
    "StaleReason",
    "Verdict",
    "duplicate_draft",
    "pairs",
    "stale_reasons",
    "stale_verdict",
    "value_verdict",
]
