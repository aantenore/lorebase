"""Reconciliation candidates (design 10.6): types, statuses, fingerprints, classification.

A candidate groups claims that disagree. Its fingerprint hashes values, not source
identities, like the Conflict fingerprint (6.10): a new source repeating a known value adds a
claim to the existing candidate instead of opening another, so a dismissed contradiction stays
dismissed until a new value appears.

Periods classify a contradiction on a ``valid_time`` Attribute: claims of different values
whose periods do not overlap are ``superseded`` (the older value stays in history); overlapping
or equal periods, or a claim without a period, are a ``contradiction`` (AC-32). Periods are
half open, ``[valid_from, valid_to)``, an unset end being open.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from lorebase_core.canonical import canonical_bytes
from lorebase_core.model.ids import new_ulid
from lorebase_core.reconcile.claims import Claim

#: Prefix of candidate ids (``rcn_<ULID>``).
CANDIDATE_ID_PREFIX = "rcn"


def new_candidate_id() -> str:
    return f"{CANDIDATE_ID_PREFIX}_{new_ulid()}"


class CandidateType(StrEnum):
    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"
    SUPERSEDED = "superseded"
    STALE = "stale"


class CandidateStatus(StrEnum):
    """``open`` until a person decides; only the pipeline sets ``obsolete`` (AC-33)."""

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    OBSOLETE = "obsolete"


class Signal(StrEnum):
    """What the stages found, recorded in a candidate's ``signals``."""

    UNIT = "unit"
    NEGATION = "negation"
    EQUIVALENT = "equivalent"
    SIMILARITY = "similarity"
    JUDGE = "judge"
    PERIODS = "periods"


class DecisionAction(StrEnum):
    """What a recorded decision did to a candidate."""

    RESOLVE = "resolve"
    DISMISS = "dismiss"
    REOPEN = "reopen"
    OBSOLETE = "obsolete"


#: Statuses each decision may start from, and the status it leaves.
TRANSITIONS: dict[DecisionAction, tuple[frozenset[CandidateStatus], CandidateStatus]] = {
    DecisionAction.RESOLVE: (frozenset({CandidateStatus.OPEN}), CandidateStatus.RESOLVED),
    DecisionAction.DISMISS: (frozenset({CandidateStatus.OPEN}), CandidateStatus.DISMISSED),
    DecisionAction.REOPEN: (frozenset({CandidateStatus.DISMISSED}), CandidateStatus.OPEN),
    DecisionAction.OBSOLETE: (frozenset({CandidateStatus.OPEN}), CandidateStatus.OBSOLETE),
}


def candidate_fingerprint(
    candidate_type: CandidateType,
    branch: str,
    kind_id: str,
    keys: Iterable[str],
    attribute_id: str | None,
    value_hashes: Iterable[str],
    scope: str | None = None,
) -> bytes:
    """``sha256(type, branch, kind_id, keys, attribute_id, sorted distinct value hashes)``,
    with ``scope`` appended when set (candidates without claims that one cause raises per
    subject, such as ``source_unseen`` per Source and sighting).

    Keys are sorted (a duplicate pair is the same pair either way round)."""
    head: list[object] = [
        candidate_type.value,
        branch,
        kind_id,
        sorted(set(keys)),
        attribute_id,
        sorted(set(value_hashes)),
    ]
    if scope is not None:
        head.append(scope)
    return hashlib.sha256(canonical_bytes(head)).digest()


class CandidateDraft(BaseModel):
    """A candidate as a stage raises it, before it is stored (upserted by fingerprint)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: CandidateType
    branch: str = Field(min_length=1)
    kind_id: str
    keys: tuple[str, ...] = Field(min_length=1)
    attribute_id: str | None = None
    claims: tuple[Claim, ...] = ()
    signals: dict[str, JsonValue] = Field(default_factory=dict)
    proposal_id: str | None = None
    #: Part of the fingerprint only (`candidate_fingerprint`); not stored.
    scope: str | None = None

    @field_validator("keys")
    @classmethod
    def _sorted_keys(cls, keys: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(keys)))

    @property
    def fingerprint(self) -> bytes:
        return candidate_fingerprint(
            self.type,
            self.branch,
            self.kind_id,
            self.keys,
            self.attribute_id,
            (c.value_hash for c in self.claims),
            self.scope,
        )


@dataclass(frozen=True, slots=True)
class MergedClaims:
    """Stored claims after new ones were folded in: the newest ``kept``, ``added`` new claim
    ids, and the total ``count`` of distinct claims seen (kept or not)."""

    kept: list[dict[str, Any]]
    added: int
    count: int
    dropped: int = 0


def _claim_order(claim: dict[str, Any]) -> tuple[str, str]:
    return (str(claim.get("at", "")), str(claim.get("claim_id", "")))


def merge_claims(
    stored: Sequence[dict[str, Any]],
    incoming: Sequence[dict[str, Any]],
    *,
    stored_count: int,
    max_claims: int,
) -> MergedClaims:
    """Fold ``incoming`` claim documents into ``stored`` ones by ``claim_id`` (a stored claim
    is kept as stored), keeping the ``max_claims`` newest by ``(at, claim_id)``, oldest first.

    ``count`` grows by the claims not stored yet; a claim dropped earlier and seen again is
    counted again, so the count is an upper bound once claims were dropped."""
    if max_claims < 1:
        raise ValueError("max_claims must be positive")
    known = {c["claim_id"] for c in stored}
    fresh: dict[str, dict[str, Any]] = {}
    for claim in incoming:
        if claim["claim_id"] not in known:
            fresh.setdefault(claim["claim_id"], claim)
    every = sorted([*stored, *fresh.values()], key=_claim_order)
    kept = every[-max_claims:]
    return MergedClaims(
        kept=kept,
        added=len(fresh),
        count=max(stored_count, len(stored)) + len(fresh),
        dropped=len(every) - len(kept),
    )


def disagree(claims: Iterable[Claim]) -> bool:
    """Whether at least two claims hold different values from different source identities
    (an unknown identity differs from every other)."""
    listed = list(claims)
    for first, second in combinations(listed, 2):
        if first.value_hash == second.value_hash:
            continue
        if first.source is None or second.source is None or first.source != second.source:
            return True
    return False


def _overlap(first: Claim, second: Claim) -> bool:
    # Canonical timestamps are fixed-width UTC: they order as text. Unset ends are open.
    starts_before_end = (
        first.valid_to is None or second.valid_from is None or (second.valid_from < first.valid_to)
    )
    ends_after_start = (
        second.valid_to is None or first.valid_from is None or (first.valid_from < second.valid_to)
    )
    return starts_before_end and ends_after_start


def classify(claims: Iterable[Claim], *, valid_time: bool) -> CandidateType:
    """``superseded`` when the Attribute has ``valid_time``, every claim has a period and no two
    claims of different values overlap; ``contradiction`` otherwise (module doc)."""
    listed = list(claims)
    if not valid_time or any(not c.has_period for c in listed):
        return CandidateType.CONTRADICTION
    for first, second in combinations(listed, 2):
        if first.value_hash != second.value_hash and _overlap(first, second):
            return CandidateType.CONTRADICTION
    return CandidateType.SUPERSEDED


__all__ = [
    "CANDIDATE_ID_PREFIX",
    "TRANSITIONS",
    "CandidateDraft",
    "CandidateStatus",
    "CandidateType",
    "DecisionAction",
    "MergedClaims",
    "Signal",
    "candidate_fingerprint",
    "classify",
    "disagree",
    "merge_claims",
    "new_candidate_id",
]
