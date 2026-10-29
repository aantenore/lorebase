"""The extraction trigger of reconciliation (design 10.6, trigger 1; 9.2 step 11).

Before items are assembled, the claims of the current values the extraction would change are
read from the target branch (`gather`). While items are assembled, `contests` tells whether a
value item raises a `contradiction` against them: with `hold_auto_accept` such an item stays
pending whatever its confidence. Once the Proposal holds the items, `raised` gives the
candidates to store: one per contested value (its claims are the current value's and the
item's, with the evidence passage and the period the item carries) and one `duplicate` per
mention left ambiguous between two Records.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from lorebase_core.model import Schema
from lorebase_core.reconcile import CandidateType, Claim, Stage
from lorebase_scribe.extraction.candidates import Resolved, ValueCandidate
from lorebase_scribe.reconcile.detector import (
    Detector,
    Raised,
    ambiguous_duplicates,
    compared,
    document_identity,
)
from lorebase_store.reconciliation import current_claims
from lorebase_store.repositories import MaterializedRepository
from lorebase_store.repositories.proposal_items import VALUE_ITEM, ItemRow
from lorebase_store.settings import WriteSettings

ClaimKey = tuple[str, str, str]
"""Kind id, key and Attribute id of a value."""

#: Members of item evidence that are the value's validity period, not its passage.
PERIOD_MEMBERS = ("valid_from", "valid_to")
_PROVISIONAL = "provisional"


def evidence_period(evidence: Mapping[str, Any] | None) -> tuple[Any, Any]:
    found = evidence or {}
    return found.get("valid_from"), found.get("valid_to")


class ExtractionCheck:
    """The claims of the current values an extraction would change, and the stages over them."""

    def __init__(
        self,
        detector: Detector,
        schema: Schema,
        branch: str,
        claims: Mapping[ClaimKey, Claim],
        *,
        now: datetime,
    ) -> None:
        self.detector = detector
        self._schema = schema
        self._branch = branch
        self._claims = claims
        self._now = now

    @classmethod
    async def gather(
        cls,
        detector: Detector,
        repository: MaterializedRepository,
        schema: Schema,
        values: Iterable[ValueCandidate],
        *,
        writes: WriteSettings,
        now: datetime,
    ) -> ExtractionCheck:
        """Read the claims of the current values `values` name (matched Records only)."""
        wanted: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        for value in values:
            subject = value.subject
            if not subject.usable or subject.resolution.status != "matched":
                continue
            kind = schema.kinds.get(subject.kind_id)
            if kind is None or not compared(kind.attributes.get(value.attribute_id)):
                continue
            wanted[subject.kind_id][str(subject.key)].add(value.attribute_id)
        claims: dict[ClaimKey, Claim] = {}
        for kind_id, by_key in wanted.items():
            attributes = {a for names in by_key.values() for a in names}
            found = await current_claims(
                repository, kind_id, by_key, attributes=attributes, writes=writes
            )
            for (key, attribute_id), claim in found.claims.items():
                claims[(kind_id, key, attribute_id)] = claim
        return cls(detector, schema, repository.branch, claims, now=now)

    @property
    def holds(self) -> bool:
        """Whether contested value items stay pending (`hold_auto_accept`)."""
        return self.detector.options.hold_auto_accept

    def _item_claim(
        self,
        proposal_id: str,
        item_id: str,
        value: Any,
        evidence: Mapping[str, Any],
    ) -> Claim | None:
        document = evidence.get("document_id")
        if not isinstance(document, str) or not document:
            return None
        valid_from, valid_to = evidence_period(evidence)
        passage = {k: v for k, v in evidence.items() if k not in PERIOD_MEMBERS}
        return Claim.of_proposal_item(
            proposal_id,
            item_id,
            value,
            source=document_identity(document),
            at=self._now,
            evidence=passage,
            valid_from=valid_from,
            valid_to=valid_to,
        )

    def _verdict(
        self, kind_id: str, key: str, attribute_id: str, claim: Claim, proposal_id: str | None
    ) -> Any:
        current = self._claims.get((kind_id, key, attribute_id))
        kind = self._schema.kinds.get(kind_id)
        attribute = None if kind is None else kind.attributes.get(attribute_id)
        if current is None or kind is None or attribute is None:
            return None, attribute
        verdict = self.detector.verdict(
            [current, claim],
            branch=self._branch,
            kind=kind,
            key=key,
            attribute=attribute,
            proposal_id=proposal_id,
        )
        return verdict, attribute

    def contests(self, value: ValueCandidate) -> bool:
        """Whether the value item would raise a `contradiction` (module doc)."""
        subject = value.subject
        evidence = {
            **value.evidence.as_dict(),
            "valid_from": value.valid_from,
            "valid_to": value.valid_to,
        }
        claim = self._item_claim(_PROVISIONAL, _PROVISIONAL, value.value, evidence)
        if claim is None:
            return False
        verdict, _attribute = self._verdict(
            subject.kind_id, str(subject.key), value.attribute_id, claim, None
        )
        return (
            verdict is not None
            and verdict.draft is not None
            and verdict.draft.type is CandidateType.CONTRADICTION
        )

    def raised(
        self, items: Sequence[ItemRow], proposal_id: str, mentions: Iterable[Resolved]
    ) -> list[Raised]:
        """The candidates of the Proposal's value items and ambiguous mentions."""
        raised: list[Raised] = []
        for item in items:
            if item.item_type != VALUE_ITEM or item.attribute_id is None:
                continue
            subject = item.subject
            claim = self._item_claim(
                proposal_id, item.item_id, subject.get("value"), item.evidence or {}
            )
            if claim is None:
                continue
            verdict, attribute = self._verdict(
                str(subject["kind"]), str(subject["key"]), item.attribute_id, claim, proposal_id
            )
            if verdict is not None:
                self.detector.collect(verdict, attribute, raised)
        if self.detector.runs(Stage.IDENTITY):
            raised.extend(ambiguous_duplicates(self._branch, ambiguous_pairs(mentions)))
        return raised


def ambiguous_pairs(mentions: Iterable[Resolved]) -> list[tuple[str, str, str, float]]:
    """`(kind id, key, other key, score)` of the two best candidates of each ambiguous
    mention (candidates of the mention's Kind only)."""
    found: list[tuple[str, str, str, float]] = []
    for mention in mentions:
        resolution = mention.resolution
        if resolution.status != "ambiguous":
            continue
        keys: list[tuple[str, float]] = []
        for candidate in resolution.candidates:
            if candidate.kind_id == resolution.kind_id and candidate.key not in dict(keys):
                keys.append((candidate.key, candidate.score))
            if len(keys) == 2:
                break
        if len(keys) == 2:
            score = min(keys[0][1], keys[1][1])
            found.append((resolution.kind_id, keys[0][0], keys[1][0], score))
    return found


__all__ = ["PERIOD_MEMBERS", "ExtractionCheck", "ambiguous_pairs", "evidence_period"]
