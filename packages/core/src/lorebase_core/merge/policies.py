"""Merge policies (design 6.8, 6.10): options applied before any person sees a Conflict.

Three policies resolve value Conflicts without a person:

- ``prefer``: one side wins, globally, per Kind or Relation, or per Attribute.
- ``authority``: a ranked list of origins (Sources, principals, actors, origin types); the
  side whose last change of the Attribute comes from the higher ranked origin wins.
- ``newest_wins``: the side whose last change of the Attribute is the more recent wins.

``authority`` and ``newest_wins`` read the Lineage of each side (the provenance entry of the
last change of every Attribute) through an explicit loader; they only apply to Attribute
Conflicts (``DV1``, ``DV3``) and leave the Conflict to the next rule, and finally to a person,
when they cannot decide.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator

from lorebase_core.merge.conflicts import Side
from lorebase_core.model import OperationOrigin, ProvenanceEntry, SourceSyncOrigin

__all__ = [
    "DEFAULT_ATOMIC_PROPERTIES",
    "ORIGIN_LABEL_PREFIXES",
    "ChangeStamp",
    "MergePolicy",
    "PolicyDecision",
    "PolicyName",
    "PolicyRule",
    "decide_authority",
    "decide_newest",
    "origin_labels",
]

#: Schema element properties merged as whole values, never entry by entry.
DEFAULT_ATOMIC_PROPERTIES = frozenset({"type", "default", "definition"})

#: Prefixes of origin labels: ``source:{source id}``, ``principal:{principal}`` (the principal
#: of an Operation), ``actor:{actor}`` (who wrote the provenance entry), ``operation:{api
#: name}`` and ``origin:{origin type}`` (``source_sync``, ``operation``, ``extraction``, ...).
ORIGIN_LABEL_PREFIXES: Final = frozenset({"source", "principal", "actor", "operation", "origin"})


class PolicyName(StrEnum):
    PREFER = "prefer"
    AUTHORITY = "authority"
    NEWEST_WINS = "newest_wins"

    @property
    def resolver(self) -> str:
        """The resolver recorded on the resolutions of this policy."""
        return f"policy:{self.value}"


@dataclass(frozen=True)
class PolicyRule:
    """One step of the rule chain of a Conflict: ``side`` for ``prefer``, ``ranking`` for
    ``authority``, nothing more for ``newest_wins``."""

    name: PolicyName
    side: Side | None = None
    ranking: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    """The side a rule picked and why, recorded with the automatic resolution."""

    name: PolicyName
    side: Side
    details: dict[str, JsonValue] = field(default_factory=dict)


def _check_label(label: str) -> str:
    prefix, sep, rest = label.partition(":")
    if not sep or not rest or prefix not in ORIGIN_LABEL_PREFIXES:
        allowed = ", ".join(sorted(ORIGIN_LABEL_PREFIXES))
        raise ValueError(f"origin label {label!r} must be '<prefix>:<value>' with prefix {allowed}")
    return label


def _check_ranking(ranking: tuple[str, ...]) -> tuple[str, ...]:
    for label in ranking:
        _check_label(label)
    if len(set(ranking)) != len(ranking):
        raise ValueError("an authority ranking lists every origin once")
    return ranking


class MergePolicy(BaseModel):
    """Options of one merge. Every default keeps the behavior of design 6.8.

    - ``fast_forward``: when ours is an ancestor of theirs, move to theirs instead of
      producing a merge Revision (default ``True``).
    - ``allow_checked_casts``: values written on one side are cast to a type changed on the
      other side with ``checked`` casts; failures are type coercion Conflicts. When ``False``
      every such value is a type coercion Conflict (default ``True``).
    - ``cascade_dangling``: a Link added on one side to a Record deleted on the other is
      dropped when its Relation has ``on_delete = cascade`` (default ``True``).
    - ``apply_defaults``: a missing value of a newly required Attribute takes the Attribute
      default when it has one (default ``True``).
    - ``prefer``, ``prefer_elements``, ``prefer_attributes``: resolve value and schema
      Conflicts (``DV1`` to ``DV3``, ``SC1``, ``SC2``) for one side, globally, per Kind or
      Relation id, or per Attribute id (default: none).
    - ``authority``, ``authority_attributes``: ranked origin labels (highest first, see
      ``ORIGIN_LABEL_PREFIXES``), globally or per Attribute id. On an Attribute Conflict the
      side whose last change comes from the higher ranked origin wins; an origin missing from
      the ranking ranks below every listed one. No decision when both sides rank the same,
      when neither is listed, or when the Lineage of a side is unknown (default: none).
    - ``schema_authority``: an Attribute's schema ``authority`` (a Source) ranks first for that
      Attribute (default ``False``).
    - ``newest_wins``, ``newest_wins_attributes``: on an Attribute Conflict, globally or for
      the listed Attribute ids, the side whose last change has the later provenance timestamp
      wins. Equal timestamps are decided by the greater provenance entry id, so the outcome
      does not depend on which branch is ours; no decision when the ids are equal too or the
      Lineage of a side is unknown (default: off).
    - ``sample_size``: member keys listed in grouped Conflicts (default 10).
    - ``atomic_properties``: schema element properties merged as whole values.

    Rules are tried from the most specific to the least: per Attribute (``prefer``, then
    ``authority``, then ``newest_wins``), per Kind or Relation (``prefer``), then global
    (``authority``, ``newest_wins``, ``prefer``). The first rule that decides resolves the
    Conflict; when none does, the Conflict is left to a person.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fast_forward: bool = True
    allow_checked_casts: bool = True
    cascade_dangling: bool = True
    apply_defaults: bool = True
    prefer: Side | None = None
    prefer_elements: dict[str, Side] = Field(default_factory=dict)
    prefer_attributes: dict[str, Side] = Field(default_factory=dict)
    authority: tuple[str, ...] = ()
    authority_attributes: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    schema_authority: bool = False
    newest_wins: bool = False
    newest_wins_attributes: frozenset[str] = frozenset()
    sample_size: int = Field(default=10, ge=1)
    atomic_properties: frozenset[str] = DEFAULT_ATOMIC_PROPERTIES

    @field_validator("prefer")
    @classmethod
    def _side(cls, side: Side | None) -> Side | None:
        if side is Side.BASE:
            raise ValueError("prefer is ours or theirs")
        return side

    @field_validator("prefer_elements", "prefer_attributes")
    @classmethod
    def _sides(cls, sides: dict[str, Side]) -> dict[str, Side]:
        if Side.BASE in sides.values():
            raise ValueError("prefer is ours or theirs")
        return sides

    @field_validator("authority")
    @classmethod
    def _ranking(cls, ranking: tuple[str, ...]) -> tuple[str, ...]:
        return _check_ranking(ranking)

    @field_validator("authority_attributes")
    @classmethod
    def _rankings(cls, rankings: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for ranking in rankings.values():
            if not ranking:
                raise ValueError("an authority ranking lists at least one origin")
            _check_ranking(ranking)
        return rankings

    def preferred(self, element: str | None, attribute: str | None) -> Side | None:
        """The side ``prefer`` picks for a Conflict on ``element`` and ``attribute``."""
        if attribute is not None and attribute in self.prefer_attributes:
            return self.prefer_attributes[attribute]
        if element is not None and element in self.prefer_elements:
            return self.prefer_elements[element]
        return self.prefer

    def rules(
        self,
        element: str | None,
        attribute: str | None,
        *,
        attribute_authority: str | None = None,
    ) -> tuple[PolicyRule, ...]:
        """The rules tried, in order, on a Conflict of ``element`` and ``attribute``.

        ``attribute`` is set for Attribute Conflicts only; ``attribute_authority`` is the
        schema ``authority`` (a Source id) of that Attribute.
        """
        rules: list[PolicyRule] = []
        if attribute is not None:
            if attribute in self.prefer_attributes:
                rules.append(PolicyRule(PolicyName.PREFER, side=self.prefer_attributes[attribute]))
            ranking = self._attribute_ranking(attribute, attribute_authority)
            if ranking:
                rules.append(PolicyRule(PolicyName.AUTHORITY, ranking=ranking))
            if attribute in self.newest_wins_attributes:
                rules.append(PolicyRule(PolicyName.NEWEST_WINS))
        if element is not None and element in self.prefer_elements:
            rules.append(PolicyRule(PolicyName.PREFER, side=self.prefer_elements[element]))
        if attribute is not None:
            if self.authority:
                rules.append(PolicyRule(PolicyName.AUTHORITY, ranking=self.authority))
            if self.newest_wins:
                rules.append(PolicyRule(PolicyName.NEWEST_WINS))
        if self.prefer is not None:
            rules.append(PolicyRule(PolicyName.PREFER, side=self.prefer))
        return tuple(rules)

    def _attribute_ranking(self, attribute: str, authority: str | None) -> tuple[str, ...]:
        head = (f"source:{authority}",) if self.schema_authority and authority else ()
        return tuple(dict.fromkeys((*head, *self.authority_attributes.get(attribute, ()))))


# Lineage-based decisions -------------------------------------------------------------------


def origin_labels(entry: ProvenanceEntry) -> frozenset[str]:
    """The labels an authority ranking can match for one provenance entry."""
    origin = entry.origin
    labels = {f"origin:{origin.type}", f"actor:{entry.actor}"}
    if isinstance(origin, SourceSyncOrigin):
        labels.add(f"source:{origin.source_id}")
    elif isinstance(origin, OperationOrigin):
        labels.add(f"principal:{origin.principal}")
        labels.add(f"operation:{origin.operation}")
    return frozenset(labels)


@dataclass(frozen=True)
class ChangeStamp:
    """Who and when of the last change of an Attribute on one side: the provenance entry id,
    its time (timezone aware) and the origin labels an authority ranking can match."""

    id: str
    at: datetime
    labels: frozenset[str]

    @classmethod
    def of(cls, entry: ProvenanceEntry) -> ChangeStamp:
        return cls(entry.id, _aware(datetime.fromisoformat(entry.at)), origin_labels(entry))

    @classmethod
    def from_stored(
        cls, id_: str, origin_type: str, origin: Mapping[str, Any], actor: str, at: datetime
    ) -> ChangeStamp:
        """A stamp of a stored provenance entry. Entries whose origin is not a Lineage origin
        (the entry of a whole commit or merge) only carry ``origin:`` and ``actor:`` labels."""
        try:
            entry = ProvenanceEntry.model_validate(
                {"id": id_, "origin": dict(origin), "actor": actor, "at": _aware(at).isoformat()}
            )
        except ValidationError:
            labels = frozenset({f"origin:{origin_type}", f"actor:{actor}"})
            return cls(id_, _aware(at), labels)
        return cls.of(entry)


def _aware(at: datetime) -> datetime:
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _rank(stamp: ChangeStamp, ranking: tuple[str, ...]) -> tuple[int | None, str | None]:
    for index, label in enumerate(ranking):
        if label in stamp.labels:
            return index, label
    return None, None


def decide_authority(
    ranking: Iterable[str], ours: ChangeStamp | None, theirs: ChangeStamp | None
) -> PolicyDecision | None:
    """The side whose change comes from the higher ranked origin, if one does."""
    if ours is None or theirs is None:
        return None
    ranked = tuple(ranking)
    ours_rank, ours_label = _rank(ours, ranked)
    theirs_rank, theirs_label = _rank(theirs, ranked)
    if ours_rank == theirs_rank:
        return None
    if theirs_rank is None or (ours_rank is not None and ours_rank < theirs_rank):
        side = Side.OURS
    else:
        side = Side.THEIRS
    return PolicyDecision(
        PolicyName.AUTHORITY,
        side,
        {
            "ours_origin": ours_label,
            "theirs_origin": theirs_label,
            "ours_provenance": ours.id,
            "theirs_provenance": theirs.id,
        },
    )


def decide_newest(ours: ChangeStamp | None, theirs: ChangeStamp | None) -> PolicyDecision | None:
    """The side whose change is the more recent; equal times fall to the greater entry id."""
    if ours is None or theirs is None:
        return None
    details: dict[str, JsonValue] = {
        "ours_at": ours.at.isoformat(),
        "theirs_at": theirs.at.isoformat(),
        "ours_provenance": ours.id,
        "theirs_provenance": theirs.id,
    }
    if ours.at != theirs.at:
        side = Side.OURS if ours.at > theirs.at else Side.THEIRS
    elif ours.id != theirs.id:
        side = Side.OURS if ours.id > theirs.id else Side.THEIRS
        details["tie_break"] = "provenance_id"
    else:
        return None
    return PolicyDecision(PolicyName.NEWEST_WINS, side, details)
