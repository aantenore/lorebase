"""Approval rules of protected branches (design 10.3, 10.7): the approvals a Proposal needs,
how many of them must come from its stewards, and which roles must each have approved.

The operator sets them under `keeper.proposals`; a Domain overrides each with its setting of
the same name. Keeper enforces them when a person merges (`lorebase_keeper.services.proposals`
evaluates the reviews against them). Automatic merges (Scribe: Source sync, direct
extraction) into a protected target are the operator's trust in automation and waive the
plain approvals count, but not the rules that name who must approve: with steward approvals
or required roles in force (`ApprovalRules.requiring`) they are not performed and the
Proposal stays open for review.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import Field, ValidationError

from lorebase_platform.settings import ConfigError, SectionModel, Settings

APPROVALS_KEY = "required_approvals"
"""Domain setting overriding `keeper.proposals.required_approvals`."""
STEWARD_APPROVALS_KEY = "required_approvals_from_stewards"
"""Domain setting overriding `keeper.proposals.required_approvals_from_stewards`."""
ROLES_KEY = "required_roles"
"""Domain setting overriding `keeper.proposals.required_roles`."""
HELD_BY_APPROVALS = "approvals"
"""Why an automatic merge was not performed, in the result of its run."""
SETTINGS_PATH = ("keeper", "proposals")
"""Where the operator's rules are in the settings."""


class ApprovalRuleSettings(SectionModel):
    """The operator's approval rules of protected branches (part of `keeper.proposals`)."""

    required_approvals: int = Field(
        default=1,
        ge=0,
        description="Approvals a Proposal needs before it merges into a protected branch.",
    )
    required_approvals_from_stewards: int = Field(
        default=0,
        ge=0,
        description="Of those, approvals that must come from the Proposal's assignees (design "
        "10.7); the Proposal needs the larger of the two in all. 0 turns the rule off.",
    )
    required_roles: list[str] = Field(
        default_factory=list,
        description="RBAC roles that must each have approved a Proposal before it merges into "
        "a protected branch: one counted approval from a principal holding the role who may "
        "review the Proposal. Does not raise the approvals count; empty turns the rule off.",
    )


@dataclass(frozen=True)
class ApprovalRules:
    """The effective rules of one Domain."""

    approvals: int
    from_stewards: int
    roles: tuple[str, ...]

    @property
    def requiring(self) -> tuple[str, ...]:
        """Names of the rules that hold an automatic merge: those naming who must approve
        (stewards, roles). The plain `required_approvals` count is not one: an operator who
        lets automation merge into protected branches (`scribe.sources.auto_merge_protected`,
        `scribe.extraction.direct_merge_protected`) waives it for those merges."""
        found = []
        if self.from_stewards > 0:
            found.append(STEWARD_APPROVALS_KEY)
        if self.roles:
            found.append(ROLES_KEY)
        return tuple(found)


def _count(value: Any, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def effective_rules(
    domain_settings: Mapping[str, Any] | None, defaults: ApprovalRuleSettings
) -> ApprovalRules:
    """`defaults` with the Domain's overrides; an override of the wrong type is ignored."""
    own = domain_settings or {}
    roles = own.get(ROLES_KEY)
    if not (isinstance(roles, list) and all(isinstance(r, str) and r for r in roles)):
        roles = defaults.required_roles
    return ApprovalRules(
        approvals=_count(own.get(APPROVALS_KEY), defaults.required_approvals),
        from_stewards=_count(
            own.get(STEWARD_APPROVALS_KEY), defaults.required_approvals_from_stewards
        ),
        roles=tuple(dict.fromkeys(roles)),
    )


def approval_rule_settings(settings: Settings) -> ApprovalRuleSettings:
    """The operator's rules as loaded (`keeper.proposals`), for processes other than Keeper,
    which validates the whole section."""
    node: Any = settings.model_extra or {}
    for part in SETTINGS_PATH:
        node = node.get(part) if isinstance(node, Mapping) else None
    if not isinstance(node, Mapping):
        return ApprovalRuleSettings()
    fields = ApprovalRuleSettings.model_fields
    try:
        return ApprovalRuleSettings.model_validate({k: v for k, v in node.items() if k in fields})
    except ValidationError as exc:
        raise ConfigError.from_validation(exc, prefix=".".join(SETTINGS_PATH)) from None
