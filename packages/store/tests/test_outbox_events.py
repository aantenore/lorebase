"""Event types webhooks subscribe to, and what a sensitive value becomes for each subscriber."""

from __future__ import annotations

from typing import Any

from lorebase_core.model import Sensitivity, vault_reference
from lorebase_store.outbox_events import (
    EVENT_TYPES,
    carries_markers,
    cleared_tokens,
    disclose,
    event_type_problem,
    operation_event_type,
    sensitive_value,
    subscribed,
)
from lorebase_store.vault import VaultState, VaultValue

TOKEN = "vlt_" + "a" * 32
ERASED = "vlt_" + "b" * 32
GONE = "vlt_" + "c" * 32


def test_event_types() -> None:
    assert operation_event_type("risk_flag_raised") == "operation.risk_flag_raised"
    for name in (*EVENT_TYPES, "operation.risk_flag_raised"):
        assert event_type_problem(name) is None
    for name in ("operation.", "operation.Risk", "ops.x", "revision.created", "operation.a.b"):
        assert event_type_problem(name) is not None
    assert subscribed("operation.x", ["operation.*"])
    assert subscribed("operation.x", ["operation.x"])
    assert not subscribed("operation.x", ["operation.y", "revision.committed"])
    assert not subscribed("revision.committed", ["operation.*"])


def payload() -> dict[str, Any]:
    return {
        "plain": "visible",
        "edits": [
            {
                "email": sensitive_value([Sensitivity.PII], vault_reference(TOKEN)),
                "old": sensitive_value([Sensitivity.PII], vault_reference(ERASED)),
                "lost": sensitive_value([Sensitivity.PII], vault_reference(GONE)),
                "vat": sensitive_value([Sensitivity.CONFIDENTIAL], "IT1"),
                "both": sensitive_value([Sensitivity.PII, Sensitivity.RESTRICTED], ["x"]),
                "empty": sensitive_value([Sensitivity.PII], None),
            }
        ],
        "stray": vault_reference(TOKEN),
    }


OPENED = {
    TOKEN: VaultValue(VaultState.VALUE, "knd", "att", "ada@example.com"),
    ERASED: VaultValue(VaultState.ERASED, "knd", "att"),
    GONE: VaultValue(VaultState.UNAVAILABLE, "knd", "att"),
}


def test_an_uncleared_subscriber_sees_every_sensitive_value_masked() -> None:
    assert cleared_tokens(payload(), ()) == set()
    shown = disclose(payload(), (), OPENED, masked="***", erased="[erased]")
    assert shown == {
        "plain": "visible",
        "edits": [
            {
                "email": "***",
                "old": "***",
                "lost": "***",
                "vat": "***",
                "both": "***",
                "empty": None,
            }
        ],
        "stray": "***",
    }


def test_a_cleared_subscriber_sees_the_values_of_its_tags() -> None:
    clearances = ("pii", "confidential")
    assert cleared_tokens(payload(), clearances) == {TOKEN, ERASED, GONE}
    shown = disclose(payload(), clearances, OPENED, masked="***", erased="[erased]")
    assert shown["edits"][0] == {
        "email": "ada@example.com",
        "old": "[erased]",
        "lost": "***",
        "vat": "IT1",
        "both": "***",
        "empty": None,
    }
    assert shown["stray"] == "***"  # a reference outside a wrapper is never opened


def test_forged_wrappers_open_nothing() -> None:
    reference = vault_reference(TOKEN)
    forged = {
        "untagged": {"$sensitive": {"tags": [], "value": reference}},
        "extra": {"$sensitive": {"tags": ["pii"], "value": reference}, "x": 1},
        "malformed": {"$sensitive": {"tags": "pii", "value": reference}},
    }
    assert cleared_tokens(forged, ("pii",)) == set()
    shown = disclose(forged, ("pii",), OPENED, masked="***", erased="[erased]")
    assert "ada@example.com" not in repr(shown)
    assert shown["untagged"] == "***"
    assert carries_markers({"a": [{"b": reference}]})
    assert carries_markers([{"$sensitive": 1}])
    assert not carries_markers({"a": ["$vault", {"b": "$sensitive"}]})
