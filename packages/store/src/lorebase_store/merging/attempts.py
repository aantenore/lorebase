"""Merge attempts computed from stored resolutions (design 6.10), independent of storage.

`compute_attempt` runs the core merge with the resolutions of a Proposal. Resolutions are
stored by fingerprint and may outnumber the Conflicts of the current run: a Conflict can
appear only once another one is resolved (a custom value that breaks uniqueness), and a
resolution can lose its Conflict when a head moves. The merge is therefore re-run with the
resolutions that matched so far until no new one matches; every Conflict seen on the way is
reported as open (in the final run) or resolved (its resolution was applied), and resolutions
that never matched are returned as unmatched.

`complete_provenance` attributes every change of a merge Revision: values taken from the
source keep their provenance, values decided by a resolution get the provenance entry of that
resolution (`merge_resolution`, design 6.10) and the rest the merge's own entry. Only the
Attributes a change sets are attributed (`taken_provenance`): an element taken whole from the
source brings the source's whole Lineage, and its unchanged values keep the Revision that last
changed them on the target.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from lorebase_core.merge import (
    CHOICES,
    AutoResolution,
    Conflict,
    ConflictKind,
    MergeAttempt,
    MergedTree,
    MergeInput,
    MergeOutcome,
    ObjectStore,
    Resolution,
    ResolutionChoice,
    ResolutionError,
    merge,
)
from lorebase_core.model import looks_like_reference
from lorebase_core.revision import ChangesetEntry
from lorebase_core.tree import PARTITION_SCHEMA
from lorebase_store.materialize.rows import ELEMENT_PROVENANCE_KEY, changed_attributes

__all__ = [
    "AttemptComputation",
    "ConflictState",
    "Decision",
    "check_resolution",
    "complete_provenance",
    "compute_attempt",
    "decisions_of",
    "taken_provenance",
]

#: Grouped kinds whose custom resolution may give values for some members only.
_PARTIAL_CUSTOM = frozenset({ConflictKind.UNIQUENESS, ConflictKind.REQUIRED_RELATION})


@dataclass(frozen=True)
class ConflictState:
    """A Conflict of an attempt with its full member keys and its state.

    `resolution` is the stored resolution that applies to it. An open Conflict with a
    resolution was `rejected`: the merge reported it again after applying the resolution.
    """

    conflict: Conflict
    members: tuple[str, ...]
    open: bool
    resolution: Resolution | None = None

    @property
    def rejected(self) -> bool:
        return self.open and self.resolution is not None


@dataclass(frozen=True)
class AttemptComputation:
    """Outcome of a merge with stored resolutions applied."""

    outcome: MergeOutcome
    conflicts: tuple[ConflictState, ...]
    applied: tuple[Resolution, ...]
    unmatched: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return isinstance(self.outcome, MergedTree)

    @property
    def open_count(self) -> int:
        return sum(1 for state in self.conflicts if state.open)

    @property
    def resolved_count(self) -> int:
        return sum(1 for state in self.conflicts if not state.open)


def compute_attempt(
    merge_input: MergeInput, resolutions: Iterable[Resolution]
) -> AttemptComputation:
    """Merge `merge_input` with the stored `resolutions` (see the module docstring).

    Raises `ResolutionError` when a stored resolution does not fit its Conflict.
    """
    stored = {r.fingerprint: r for r in resolutions}
    applied: dict[str, Resolution] = {}
    seen: dict[str, tuple[Conflict, tuple[str, ...]]] = {}
    outcome: MergeOutcome | None = None
    for _ in range(len(stored) + 1):
        outcome = merge(replace(merge_input, resolutions=tuple(applied.values())))
        if isinstance(outcome, MergeAttempt):
            for conflict in outcome.conflicts:
                if conflict.fingerprint not in seen:
                    members = tuple(outcome.members.get(conflict.fingerprint, ()))
                    seen[conflict.fingerprint] = (conflict, members)
        matched = {fp: r for fp, r in stored.items() if fp in seen and fp not in applied}
        if not matched:
            break
        applied.update(matched)
    assert outcome is not None
    open_now = (
        {c.fingerprint: c for c in outcome.conflicts} if isinstance(outcome, MergeAttempt) else {}
    )
    states: list[ConflictState] = []
    for fingerprint, (conflict, members) in seen.items():
        if fingerprint in open_now:
            states.append(
                ConflictState(
                    open_now[fingerprint], members, open=True, resolution=applied.get(fingerprint)
                )
            )
        elif fingerprint in applied:
            states.append(
                ConflictState(conflict, members, open=False, resolution=applied[fingerprint])
            )
    return AttemptComputation(
        outcome=outcome,
        conflicts=tuple(states),
        applied=tuple(applied.values()),
        unmatched=tuple(fp for fp in stored if fp not in applied),
    )


def check_resolution(conflict: Conflict, members: Sequence[str], resolution: Resolution) -> None:
    """Raise `ResolutionError` unless `resolution` is acceptable for `conflict`.

    The choice must be one of the Conflict kind's choices; a custom resolution of a grouped
    Conflict given per member must cover every member unless it also has a default `value`
    (uniqueness and required relation Conflicts accept values for some members only).
    """
    if resolution.fingerprint != conflict.fingerprint:
        raise ResolutionError("the resolution names another Conflict")
    if resolution.choice not in CHOICES[conflict.kind]:
        allowed = ", ".join(c.value for c in CHOICES[conflict.kind])
        raise ResolutionError(
            f"{resolution.choice.value} does not resolve a {conflict.kind.value} Conflict "
            f"(accepted: {allowed})"
        )
    if resolution.choice is ResolutionChoice.CUSTOM and (
        _holds_reference((conflict.base, conflict.ours, conflict.theirs))
        or _holds_reference((resolution.value, resolution.values))
    ):
        # A custom value would bypass the vault (design 13.5), and a reference would copy
        # another Record's value: vault values are resolved by picking a side.
        raise ResolutionError(
            "a Conflict over vault values takes one of its sides, not a custom value"
        )
    if (
        resolution.choice is ResolutionChoice.CUSTOM
        and resolution.values is not None
        and resolution.value is None
        and conflict.kind not in _PARTIAL_CUSTOM
    ):
        missing = [m for m in members if m not in resolution.values]
        if missing:
            raise ResolutionError(
                f"custom values miss {len(missing)} member(s), for example {missing[0]!r}"
            )


def _holds_reference(value: Any) -> bool:
    """Whether a vault reference appears anywhere in `value`."""
    if looks_like_reference(value):
        return True
    if isinstance(value, Mapping):
        return any(_holds_reference(item) for item in value.values())
    if isinstance(value, list | tuple):
        return any(_holds_reference(item) for item in value)
    return False


# Provenance -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """A resolution or automatic resolution, located for attribution."""

    fingerprint: str
    resolver: str
    choice: ResolutionChoice
    partition: str
    key: str | None
    attribute: str | None
    members: tuple[str, ...] = ()


def decisions_of(
    computation: AttemptComputation, auto: Sequence[AutoResolution] = ()
) -> list[Decision]:
    """Decisions of a clean merge: applied resolutions, then automatic resolutions."""
    found: list[Decision] = []
    for state in computation.conflicts:
        if state.resolution is None or state.open:
            continue
        conflict = state.conflict
        found.append(
            Decision(
                fingerprint=conflict.fingerprint,
                resolver=state.resolution.resolver,
                choice=state.resolution.choice,
                partition=conflict.partition,
                key=conflict.key,
                attribute=conflict.attribute,
                members=state.members or conflict.sample,
            )
        )
    for item in auto:
        found.append(
            Decision(
                fingerprint=item.fingerprint,
                resolver=item.resolver,
                choice=item.choice,
                partition=item.partition,
                key=item.key,
                attribute=item.attribute,
                members=item.sample,
            )
        )
    return found


def _index(decisions: Sequence[Decision]) -> dict[tuple[str, str, str | None], Decision]:
    """Decisions by `(partition, key, attribute)`; schema decisions by element only."""
    index: dict[tuple[str, str, str | None], Decision] = {}
    for decision in decisions:
        attribute = None if decision.partition == PARTITION_SCHEMA else decision.attribute
        keys = [decision.key] if decision.key is not None else []
        keys.extend(decision.members)
        for key in keys:
            index.setdefault((decision.partition, key, attribute), decision)
    return index


def _values(objects: ObjectStore, digest: bytes | None) -> Mapping[str, Any] | None:
    if digest is None:
        return None
    body = objects.get(digest)
    if not isinstance(body, dict):
        return None
    values = body.get("values")
    return values if isinstance(values, dict) else {}


def _changed(entry: ChangesetEntry, objects: ObjectStore) -> list[str] | None:
    """Attributes whose value the change sets; None for schema elements and deletions."""
    if entry.partition == PARTITION_SCHEMA or entry.new_hash is None:
        return None
    return changed_attributes(
        _values(objects, entry.old_hash), _values(objects, entry.new_hash) or {}
    )


def _restricted(entry: ChangesetEntry, changed: list[str] | None) -> dict[str, str]:
    if changed is None:
        return dict(entry.provenance)
    kept = {*changed, ELEMENT_PROVENANCE_KEY}
    return {a: p for a, p in entry.provenance.items() if a in kept}


def taken_provenance(
    entries: Sequence[ChangesetEntry], objects: ObjectStore
) -> list[ChangesetEntry]:
    """Entries whose provenance names only the Attributes they change (and the element key);
    schema elements and deletions are kept as they are."""
    return [
        replace(entry, provenance=_restricted(entry, _changed(entry, objects))) for entry in entries
    ]


def complete_provenance(
    entries: Sequence[ChangesetEntry],
    objects: ObjectStore,
    decisions: Sequence[Decision],
    decision_ids: Mapping[str, str],
    fallback: str,
) -> tuple[list[ChangesetEntry], set[str]]:
    """Entries whose provenance covers every changed Attribute; returns the decisions used.

    `decision_ids` maps decision fingerprints to provenance entry ids; `fallback` is the id
    used for changes that neither the source nor a decision accounts for. Provenance of
    Attributes a change leaves as they are is dropped (`taken_provenance`).
    """
    index = _index(decisions)
    used: set[str] = set()

    def decided(partition: str, key: str, attribute: str | None) -> str | None:
        decision = index.get((partition, key, attribute))
        if decision is None and attribute is not None:
            decision = index.get((partition, key, None))
        if decision is None or decision.fingerprint not in decision_ids:
            return None
        used.add(decision.fingerprint)
        return decision_ids[decision.fingerprint]

    completed: list[ChangesetEntry] = []
    for entry in entries:
        found = _changed(entry, objects)
        provenance = _restricted(entry, found)
        changed = found or []
        for attribute in changed:
            chosen = decided(entry.partition, entry.key, attribute)
            if chosen is not None:
                provenance[attribute] = chosen
            elif attribute not in provenance:
                provenance[attribute] = fallback
        if not changed:
            chosen = decided(entry.partition, entry.key, None)
            if chosen is not None or not provenance:
                provenance = {ELEMENT_PROVENANCE_KEY: chosen or fallback}
        completed.append(replace(entry, provenance=provenance))
    return completed, used
