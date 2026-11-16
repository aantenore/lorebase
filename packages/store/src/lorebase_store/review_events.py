"""Proposal and Conflict events of the outbox (design 10.5) and who may receive them.

**Types.** `proposal.opened` (a Proposal is created open), `proposal.updated` (its title,
description or policies change), `proposal.merged`, `proposal.closed`; `conflict.opened` (a
merge attempt reports a Conflict the previous attempt of the Proposal did not report open)
and `conflict.resolved` (a Conflict open in the previous attempt is resolved by a stored
resolution, or no longer reported). They are written in the transaction of the change, so an
event exists only if the change committed. Webhooks subscribe to them by name or with
`proposal.*` and `conflict.*`; none is part of the default subscription.

**Audience.** A payload may carry `$audience`: the branches and the sensitivity tags a
subscriber needs to receive the event at all. A webhook reads the branches its `branches`
list names (every branch when unset) and the values of the tags it holds clearances for;
an event whose audience it does not cover is not delivered to it (no masked placeholder that
would tell the event exists). `$audience` is removed before the payload is delivered.
Proposal and Conflict events name the target and the source branch; a Conflict about an
Attribute also names the tags of that Attribute, the same rule `conflict_in_progress` applies
to readers.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from lorebase_core.model import Sensitivity

PROPOSAL_OPENED = "proposal.opened"
PROPOSAL_UPDATED = "proposal.updated"
PROPOSAL_MERGED = "proposal.merged"
PROPOSAL_CLOSED = "proposal.closed"
CONFLICT_OPENED = "conflict.opened"
CONFLICT_RESOLVED = "conflict.resolved"
PROPOSAL_EVENTS = "proposal.*"
"""Subscribes to every Proposal event."""
CONFLICT_EVENTS = "conflict.*"
"""Subscribes to every Conflict event."""
REVIEW_EVENT_TYPES: tuple[str, ...] = (
    PROPOSAL_OPENED,
    PROPOSAL_UPDATED,
    PROPOSAL_MERGED,
    PROPOSAL_CLOSED,
    CONFLICT_OPENED,
    CONFLICT_RESOLVED,
)
REVIEW_WILDCARDS: dict[str, str] = {PROPOSAL_EVENTS: "proposal.", CONFLICT_EVENTS: "conflict."}
"""Wildcard subscriptions and the prefix of the event types each covers."""
UPDATED_FIELDS = frozenset({"title", "description", "policies"})
"""Proposal columns whose change is a `proposal.updated` event."""

AUDIENCE_KEY = "$audience"


def audience(*, branches: Iterable[str], tags: Iterable[Sensitivity | str] = ()) -> dict[str, Any]:
    """The `$audience` member: branches and tags a subscriber must cover (module doc)."""
    return {
        "branches": sorted(set(branches)),
        "tags": sorted({Sensitivity(tag).value for tag in tags}),
    }


def admitted(
    payload: Mapping[str, Any],
    *,
    branches: Collection[str] | None,
    clearances: Collection[str],
) -> bool:
    """Whether a subscriber reading `branches` (None: every branch) and cleared for
    `clearances` receives an event with `payload`. A malformed audience admits nobody."""
    found = payload.get(AUDIENCE_KEY)
    if found is None:
        return True
    if not isinstance(found, Mapping):
        return False
    wanted_branches = found.get("branches", ())
    tags = found.get("tags", ())
    if not isinstance(wanted_branches, list) or not isinstance(tags, list):
        return False
    if branches is not None and any(branch not in branches for branch in wanted_branches):
        return False
    return all(tag in clearances for tag in tags)


def without_audience(payload: Mapping[str, Any]) -> dict[str, Any]:
    """`payload` as delivered: without `$audience`."""
    return {key: value for key, value in payload.items() if key != AUDIENCE_KEY}


def timestamp(at: datetime | None) -> str | None:
    return None if at is None else at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def proposal_payload(
    domain_id: str,
    *,
    proposal_id: str,
    source: str,
    target: str,
    origin: str,
    status: str,
    author: str,
    at: datetime | None,
    merged_revision: bytes | None = None,
    changed: Collection[str] = (),
) -> dict[str, Any]:
    """Payload of a Proposal event: ids, branches, origin, status and author, never the
    title, the description or anything read from the source branch."""
    payload: dict[str, Any] = {
        "domain": domain_id,
        "branch": target,
        "source_branch": source,
        "proposal": proposal_id,
        "origin": origin,
        "status": status,
        "author": author,
        "at": timestamp(at),
    }
    if merged_revision is not None:
        payload["revision"] = merged_revision.hex()
    if changed:
        payload["changed"] = sorted(changed)
    payload[AUDIENCE_KEY] = audience(branches=(target, source))
    return payload
