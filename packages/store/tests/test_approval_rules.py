"""Approval rules of protected branches shared by Keeper and Scribe
(`lorebase_store.approval_rules`)."""

from __future__ import annotations

from typing import Any

import pytest

from lorebase_platform.settings import ConfigError, Settings
from lorebase_store.approval_rules import (
    APPROVALS_KEY,
    ROLES_KEY,
    STEWARD_APPROVALS_KEY,
    ApprovalRules,
    ApprovalRuleSettings,
    approval_rule_settings,
    effective_rules,
)

OPERATOR = ApprovalRuleSettings(
    required_approvals=2, required_approvals_from_stewards=1, required_roles=["x", "y", "x"]
)


def test_without_domain_settings_the_operator_rules_apply() -> None:
    assert effective_rules(None, OPERATOR) == ApprovalRules(2, 1, ("x", "y"))
    assert effective_rules({}, ApprovalRuleSettings()) == ApprovalRules(1, 0, ())


@pytest.mark.parametrize(
    ("own", "expected"),
    [
        ({APPROVALS_KEY: 0}, ApprovalRules(0, 1, ("x", "y"))),
        ({STEWARD_APPROVALS_KEY: 3}, ApprovalRules(2, 3, ("x", "y"))),
        ({ROLES_KEY: ["z"]}, ApprovalRules(2, 1, ("z",))),
        ({ROLES_KEY: []}, ApprovalRules(2, 1, ())),
        # Overrides of the wrong type are ignored.
        ({APPROVALS_KEY: "x", STEWARD_APPROVALS_KEY: True}, ApprovalRules(2, 1, ("x", "y"))),
        ({APPROVALS_KEY: -1, ROLES_KEY: ["z", 3]}, ApprovalRules(2, 1, ("x", "y"))),
    ],
)
def test_a_domain_overrides_each_rule(own: dict[str, Any], expected: ApprovalRules) -> None:
    assert effective_rules(own, OPERATOR) == expected


@pytest.mark.parametrize(
    ("rules", "requiring"),
    [
        (ApprovalRules(0, 0, ()), ()),
        (ApprovalRules(3, 0, ()), ()),
        (ApprovalRules(0, 2, ()), (STEWARD_APPROVALS_KEY,)),
        (ApprovalRules(0, 0, ("r",)), (ROLES_KEY,)),
        (ApprovalRules(1, 1, ("r",)), (STEWARD_APPROVALS_KEY, ROLES_KEY)),
    ],
)
def test_requiring_names_the_rules_that_hold_an_automatic_merge(
    rules: ApprovalRules, requiring: tuple[str, ...]
) -> None:
    assert rules.requiring == requiring


def test_other_processes_read_the_rules_of_keeper_proposals() -> None:
    assert approval_rule_settings(Settings()) == ApprovalRuleSettings()
    loaded = Settings(
        keeper={
            "proposals": {
                "required_approvals": 0,
                "required_roles": ["steward"],
                "allow_self_approval": True,
            }
        }
    )
    assert approval_rule_settings(loaded) == ApprovalRuleSettings(
        required_approvals=0, required_roles=["steward"]
    )
    with pytest.raises(ConfigError, match=r"keeper\.proposals"):
        approval_rule_settings(Settings(keeper={"proposals": {"required_approvals": -1}}))
