"""Event types of the outbox and the sensitive values their payloads carry (design 10.2, 10.5).

**Types.** A commit writes `revision.committed`; an Operation that declares side effects writes
one `operation.<name>` event per name, in its commit transaction. Webhooks subscribe to exact
types or to `operation.*`, every Operation event.

**Sensitive values.** A payload never holds a value that needs a clearance as such: the writer
wraps it as `{"$sensitive": {"tags": [...], "value": ...}}`, the value of a vault Attribute
being its vault reference (`{"$vault": token}`), never the personal data. `disclose` renders a
payload for one subscriber when it is sent: a wrapped value whose tags the subscriber is
cleared for is shown (a vault reference opened; an erased one shows `erased`), any other is
replaced by `masked`, and so is a vault reference found outside a wrapper. A wrapper without
tags is never cleared, and writers wrap with every tag any value they did not produce whose
JSON holds `$sensitive` or `$vault` keys (`carries_markers`), so a value written by a caller
cannot pose as a wrapper or a reference. Stored payloads and deliveries therefore keep no vault
value, and an erasure reaches deliveries not sent yet. Values of sensitive Attributes outside
the vault are kept as committed, like the Revision objects that hold them.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Iterator, Mapping
from typing import Any

from lorebase_core.model import VAULT_KEY, Sensitivity, vault_token
from lorebase_core.model.operation import SIDE_EFFECT_MAX_LENGTH
from lorebase_store.commit.pipeline import COMMITTED_EVENT
from lorebase_store.vault import VaultState, VaultValue

OPERATION_EVENT_PREFIX = "operation."
OPERATION_EVENTS = "operation.*"
"""Subscribes to the events of every Operation side effect."""
EVENT_TYPES: tuple[str, ...] = (COMMITTED_EVENT, OPERATION_EVENTS)
"""Event types a webhook subscribes to when it names none."""
SENSITIVE_KEY = "$sensitive"

_OPERATION_EVENT = re.compile(r"operation\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


def operation_event_type(side_effect: str) -> str:
    """The outbox event type of an Operation side effect."""
    return f"{OPERATION_EVENT_PREFIX}{side_effect}"


def event_type_problem(name: str) -> str | None:
    """Why `name` is not an event type a webhook may subscribe to; None when it is."""
    if name in EVENT_TYPES:
        return None
    if (
        _OPERATION_EVENT.fullmatch(name)
        and len(name) - len(OPERATION_EVENT_PREFIX) <= SIDE_EFFECT_MAX_LENGTH
    ):
        return None
    return (
        f"unknown event type {name!r}: expected {COMMITTED_EVENT}, {OPERATION_EVENTS} or "
        "operation.<side effect>"
    )


def subscribed(event_type: str, subscriptions: Collection[str]) -> bool:
    """Whether a subscription to `subscriptions` receives events of `event_type`."""
    if event_type in subscriptions:
        return True
    return event_type.startswith(OPERATION_EVENT_PREFIX) and OPERATION_EVENTS in subscriptions


def sensitive_value(tags: Iterable[Sensitivity], value: Any) -> dict[str, Any]:
    """`value` wrapped with the tags that clear it (see module doc)."""
    return {SENSITIVE_KEY: {"tags": sorted(tag.value for tag in tags), "value": value}}


def carries_markers(value: Any) -> bool:
    """Whether `value` holds, at any depth, an object with a `$sensitive` or `$vault` key."""
    if isinstance(value, dict):
        return (
            SENSITIVE_KEY in value
            or VAULT_KEY in value
            or any(map(carries_markers, value.values()))
        )
    if isinstance(value, list):
        return any(map(carries_markers, value))
    return False


def _wrapped(node: Any) -> tuple[frozenset[str], Any] | None:
    if isinstance(node, dict) and len(node) == 1 and SENSITIVE_KEY in node:
        body = node[SENSITIVE_KEY]
        if isinstance(body, dict) and isinstance(body.get("tags"), list):
            return frozenset(str(t) for t in body["tags"]), body.get("value")
    return None


def _cleared(tags: frozenset[str], clearances: Collection[str]) -> bool:
    return bool(tags) and all(tag in clearances for tag in tags)


def cleared_tokens(payload: Any, clearances: Collection[str]) -> set[str]:
    """Vault tokens that `disclose` opens for a subscriber with `clearances`."""
    return set(_tokens(payload, clearances, inside=False))


def _tokens(node: Any, clearances: Collection[str], *, inside: bool) -> Iterator[str]:
    wrapped = _wrapped(node)
    if wrapped is not None:
        tags, value = wrapped
        if _cleared(tags, clearances):
            yield from _tokens(value, clearances, inside=True)
        return
    if isinstance(node, dict):
        token = vault_token(node) if VAULT_KEY in node else None
        if token is not None:
            if inside:
                yield token
            return
        for item in node.values():
            yield from _tokens(item, clearances, inside=inside)
    elif isinstance(node, list):
        for item in node:
            yield from _tokens(item, clearances, inside=inside)


def disclose(
    payload: Any,
    clearances: Collection[str],
    opened: Mapping[str, VaultValue],
    *,
    masked: str,
    erased: str,
) -> Any:
    """`payload` as a subscriber with `clearances` receives it (see module doc). `opened`
    holds the vault values of `cleared_tokens`."""
    return _disclose(payload, frozenset(clearances), opened, masked, erased, inside=False)


def _disclose(
    node: Any,
    clearances: frozenset[str],
    opened: Mapping[str, VaultValue],
    masked: str,
    erased: str,
    *,
    inside: bool,
) -> Any:
    wrapped = _wrapped(node)
    if wrapped is not None:
        tags, value = wrapped
        if not _cleared(tags, clearances):
            return None if value is None else masked
        return _disclose(value, clearances, opened, masked, erased, inside=True)
    if isinstance(node, dict):
        if VAULT_KEY in node:
            token = vault_token(node)
            if token is None or not inside:
                return masked
            found = opened.get(token)
            if found is None or found.state is VaultState.UNAVAILABLE:
                return masked
            return erased if found.state is VaultState.ERASED else found.value
        return {
            key: _disclose(item, clearances, opened, masked, erased, inside=inside)
            for key, item in node.items()
        }
    if isinstance(node, list):
        return [_disclose(item, clearances, opened, masked, erased, inside=inside) for item in node]
    return node
