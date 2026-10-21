"""Agent writes without a database: branch naming, settings, the decision and the description."""

from __future__ import annotations

import pytest

from lorebase_keeper.errors import NOT_FOUND, ProblemError, ProblemType
from lorebase_keeper.services import agent_writes as aw
from lorebase_keeper.services.config import AgentWriteSettings
from lorebase_keeper.services.errors import IDEMPOTENCY_CONFLICT, REVIEW_REQUIRED
from lorebase_keeper.services.proposals import Approval
from lorebase_keeper.settings import KeeperSettings
from lorebase_store.models.enums import PrincipalType

DEFAULTS = AgentWriteSettings()
AGENT = {"agent": True}


def settings(**changes: object) -> AgentWriteSettings:
    return AgentWriteSettings.model_validate(changes)


# Names -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Audit Q4", "audit-q4"),
        ("  risk--review ", "risk-review"),
        ("a/b\\c", "a-b-c"),
        ("v1.2_final", "v1.2_final"),
        ("..hidden", "hidden"),
        ("Zażółć gęślą", "za-g-l"),
        ("***", ""),
        ("", ""),
    ],
)
def test_slug_normalizes_to_branch_safe_text(text: str, expected: str) -> None:
    assert aw.slug(text) == expected


def test_branch_name_joins_prefix_principal_and_task() -> None:
    assert (
        aw.branch_name(DEFAULTS, "svc_01KEZF7C", "Audit S-1001")
        == "agent/svc_01kezf7c/audit-s-1001"
    )
    custom = settings(branch_prefix="bots/ai")
    assert aw.branch_name(custom, "svc_1", "t") == "bots/ai/svc_1/t"
    assert aw.owner_prefix(DEFAULTS, "svc_1") == "agent/svc_1/"


@pytest.mark.parametrize("task", ["", "***", "x" * 61])
def test_unusable_tasks_are_validation_problems(task: str) -> None:
    with pytest.raises(ProblemError) as caught:
        aw.task_slug(task, DEFAULTS)
    assert caught.value.status_code == 422
    assert caught.value.extensions["errors"][0]["pointer"] == "/body/task"


def test_task_length_comes_from_settings() -> None:
    assert aw.task_slug("x" * 61, settings(max_task_length=61)) == "x" * 61


def test_only_the_three_part_names_are_task_branches() -> None:
    assert aw.is_task_branch(DEFAULTS, "agent/svc_1/audit")
    assert not aw.is_task_branch(DEFAULTS, "agent/svc_1")
    assert not aw.is_task_branch(DEFAULTS, "agent/svc_1/audit/deeper")
    assert not aw.is_task_branch(DEFAULTS, "main")
    assert not aw.is_task_branch(DEFAULTS, "agents/svc_1/audit")
    assert aw.is_task_branch(settings(branch_prefix="a/b"), "a/b/svc_1/audit") is True


# Settings --------------------------------------------------------------------------------


def test_defaults_make_agents_propose_and_nobody_merge() -> None:
    assert DEFAULTS.enabled
    assert DEFAULTS.auto_propose
    assert not DEFAULTS.auto_merge
    assert DEFAULTS.allowed_roles_for_auto_merge == ()
    assert DEFAULTS.principal_types == (PrincipalType.SERVICE,)
    assert KeeperSettings().agent_writes == DEFAULTS


@pytest.mark.parametrize(
    "changes",
    [
        {"branch_prefix": "/agent"},
        {"branch_prefix": "bad prefix"},
        {"max_open_branches_per_principal": 0},
        {"title_template": "{who}: {task}"},
        {"title_template": "{}"},
        {"unknown": 1},
    ],
)
def test_invalid_settings_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"."):
        settings(**changes)


def test_a_domain_overrides_members_of_the_installation_settings() -> None:
    base = settings(auto_merge=False, allowed_roles_for_auto_merge=["maintainer"])
    domain = {"agent_writes": {"auto_merge": True, "allowed_roles_for_auto_merge": ["editor"]}}
    merged = aw.effective_settings(base, domain)
    assert merged.auto_merge is True
    assert merged.allowed_roles_for_auto_merge == ("editor",)
    assert merged.branch_prefix == base.branch_prefix
    assert aw.effective_settings(base, {}) is base
    assert aw.effective_settings(base, None) is base


@pytest.mark.parametrize(
    "override", [{"auto_merge": "maybe"}, {"nope": True}, "text", ["x"], {"branch_prefix": "/"}]
)
def test_an_invalid_domain_override_is_a_validation_problem(override: object) -> None:
    with pytest.raises(ProblemError) as caught:
        aw.effective_settings(DEFAULTS, {"agent_writes": override})
    assert caught.value.status_code == 422
    assert "agent_writes" in str(caught.value.problem_detail)


# Decision --------------------------------------------------------------------------------


def decide(
    config: AgentWriteSettings = DEFAULTS,
    *,
    task: str | None = "audit",
    auto_propose: bool | None = None,
    auto_merge: bool | None = None,
    agent: bool = True,
    roles: tuple[str, ...] = ("editor",),
) -> aw.Decision:
    request = aw.AgentRequest(task=task, auto_propose=auto_propose, auto_merge=auto_merge)
    return aw.decide(config, request, agent=agent, roles=roles)


def test_an_agent_with_a_task_proposes_by_default() -> None:
    decision = decide()
    assert decision.propose
    assert not decision.merge
    assert decision.refusal is None


def test_a_request_without_a_task_gets_no_proposal() -> None:
    assert decide(task=None).refusal == "task-required"
    assert decide(task=None, auto_propose=True).refusal == "task-required"


def test_other_principals_must_ask_for_a_proposal() -> None:
    assert decide(agent=False).refusal == "declined"
    assert decide(agent=False, auto_propose=True).propose


def test_a_request_can_decline_and_the_settings_can_turn_the_flow_off() -> None:
    assert decide(auto_propose=False).refusal == "declined"
    off = settings(enabled=False)
    assert decide(off).refusal == "disabled"
    assert decide(off, auto_propose=True).refusal == "disabled"
    assert decide(settings(auto_propose=False)).refusal == "declined"
    assert decide(settings(auto_propose=False), auto_propose=True).propose


def test_merging_needs_the_request_or_settings_and_an_allowed_role() -> None:
    allowed = settings(allowed_roles_for_auto_merge=["editor", "maintainer"])
    assert decide(allowed, auto_merge=True).merge
    assert not decide(allowed).merge  # not asked for
    assert decide(settings(auto_merge=True, allowed_roles_for_auto_merge=["editor"])).merge
    assert not decide(
        settings(auto_merge=True, allowed_roles_for_auto_merge=["editor"]), auto_merge=False
    ).merge


def test_the_role_decides_not_the_request() -> None:
    allowed = settings(allowed_roles_for_auto_merge=["maintainer"])
    refused = decide(allowed, auto_merge=True, roles=("editor", "viewer"))
    assert refused.merge_requested
    assert not refused.merge_allowed
    assert not refused.merge
    assert decide(allowed, auto_merge=True, roles=("editor", "maintainer")).merge
    assert not decide(DEFAULTS, auto_merge=True).merge  # no role is allowed by default
    assert not decide(allowed, auto_merge=True, roles=()).merge


def test_nothing_merges_without_a_proposal() -> None:
    allowed = settings(allowed_roles_for_auto_merge=["editor"])
    decision = decide(allowed, auto_merge=True, auto_propose=False)
    assert decision.merge_allowed
    assert decision.merge_requested
    assert not decision.propose
    assert not decision.merge


def approval(
    *, required: int = 1, approvers: tuple[str, ...] = (), protected: bool = True
) -> Approval:
    return Approval(
        protected=protected,
        required=required,
        approvers=approvers,
        changes_requested=(),
        blocking=True,
    )


@pytest.mark.parametrize(
    ("state", "policy_review", "expected"),
    [
        (approval(approvers=("usr_a",)), False, []),
        (approval(), False, ["approvals-pending"]),
        (approval(required=0), False, []),
        (approval(required=0), True, ["review-policy-needs-approval"]),
        (approval(approvers=("usr_a",)), True, []),
        (approval(), True, ["approvals-pending", "review-policy-needs-approval"]),
        (approval(protected=False), False, []),
        (approval(protected=False), True, ["review-policy-needs-approval"]),
    ],
)
def test_merge_blockers_keep_required_approvals_and_review_policies(
    state: Approval, policy_review: bool, expected: list[str]
) -> None:
    assert aw.merge_blockers(state, policy_review=policy_review) == expected


# Proposal text ---------------------------------------------------------------------------


def test_operation_line_lists_sorted_parameters_cut_to_the_limit() -> None:
    config = settings(summary_value_chars=12)
    line = aw.operation_line(
        "flag_supplier_risk",
        {"supplier": "S-1001", "severity": "high", "reason": "x" * 40},
        "inv_1",
        config,
    )
    assert line == (
        '- flag_supplier_risk(reason="xxxxxxxxxx…, severity="high", supplier="S-1001") [inv_1]'
    )


def test_operation_line_can_leave_values_out() -> None:
    config = settings(summary_parameter_values=False)
    line = aw.operation_line("flag", {"b": 1, "a": "secret"}, "inv_2", config)
    assert line == "- flag(a, b) [inv_2]"


def test_description_starts_with_the_intent_and_accumulates_executions() -> None:
    first = aw.describe("Flag the supplier after the audit", "- op(a=1) [inv_1]", None, DEFAULTS)
    assert first == "Flag the supplier after the audit\n\nOperations:\n- op(a=1) [inv_1]"
    second = aw.describe(None, "- op(a=2) [inv_2]", first, DEFAULTS)
    assert second == (
        "Flag the supplier after the audit\n\nOperations:\n- op(a=1) [inv_1]\n- op(a=2) [inv_2]"
    )
    assert aw.describe(None, "- op(a=2) [inv_2]", second, DEFAULTS) == second  # idempotent
    changed = aw.describe("New intent", "- op(a=3) [inv_3]", second, DEFAULTS)
    assert changed.startswith("New intent\n\nOperations:\n")
    assert changed.endswith("- op(a=3) [inv_3]")


def test_description_without_an_intent_is_the_list() -> None:
    assert aw.describe(None, "- op() [inv_1]", None, DEFAULTS) == "Operations:\n- op() [inv_1]"


def test_description_keeps_the_latest_executions() -> None:
    config = settings(max_summary_operations=2)
    text: str | None = None
    for n in range(4):
        text = aw.describe("Intent", f"- op(n={n}) [inv_{n}]", text, config)
    assert text is not None
    assert text.splitlines()[-2:] == ["- op(n=2) [inv_2]", "- op(n=3) [inv_3]"]
    assert "inv_1" not in text


# Problems --------------------------------------------------------------------------------


def refusal(problem: ProblemType) -> ProblemError:
    return ProblemError(problem, "refused", extensions={"operation": "flag_supplier_risk"})


def test_the_flow_takes_over_review_and_idempotency_refusals_only() -> None:
    assert aw.diverts(refusal(REVIEW_REQUIRED))
    assert aw.diverts(refusal(IDEMPOTENCY_CONFLICT))
    assert not aw.diverts(refusal(NOT_FOUND))


def test_review_required_problems_gain_next_hints() -> None:
    base = "/api/v1/domains/supply"
    problem = aw.hinted(refusal(REVIEW_REQUIRED), base, "task-required")
    steps = problem.extensions["next"]
    assert steps[0]["action"] == "name_a_task"
    assert steps[0]["href"] == f"{base}/operations/flag_supplier_risk/execute"
    assert [s["action"] for s in steps[1:]] == [
        "create_branch",
        "execute_on_branch",
        "open_proposal",
    ]
    declined = aw.hinted(refusal(REVIEW_REQUIRED), base, "declined")
    assert declined.extensions["next"][0]["action"] == "auto_propose"
    off = aw.hinted(refusal(REVIEW_REQUIRED), base, "disabled")
    assert off.extensions["next"][0]["action"] == "agent_writes_disabled"


def test_other_problems_are_left_alone() -> None:
    conflict = refusal(IDEMPOTENCY_CONFLICT)
    assert aw.hinted(conflict, "/api", "task-required") is conflict
    assert "next" not in conflict.extensions


def proposal_summary() -> aw.ProposalSummary:
    return aw.ProposalSummary(
        id="prp_1",
        title="t",
        status="open",
        source_ref="agent/svc_1/audit",
        target_ref="main",
        url="/p/prp_1",
        created=True,
    )


def steps(status: aw.Status, merge: aw.MergeSummary, **extra: str | None) -> list[aw.Hint]:
    return aw.next_steps(
        status,
        "/api/v1/domains/supply",
        proposal_summary(),
        approval=aw.ApprovalSummary(required=1, approvals=0, satisfied=False),
        merge=merge,
        merged_revision=extra.get("merged_revision"),
    )


def test_next_steps_follow_the_state_of_the_proposal() -> None:
    waiting = steps("proposed", aw.MergeSummary(True, True, False, ["approvals-pending"]))
    assert [s["action"] for s in waiting] == ["await_review", "get_proposal"]
    assert waiting[0]["permission"] == "proposal.review"
    manual = steps("proposed", aw.MergeSummary(False, False, False))
    assert manual[0]["action"] == "merge"
    assert manual[0]["href"].endswith("/proposals/prp_1/merge")
    conflicted = steps("conflicts", aw.MergeSummary(True, True, False))
    assert [s["action"] for s in conflicted] == [
        "inspect_conflicts",
        "resolve_conflicts",
        "close_and_retry",
    ]
    merged = steps("merged", aw.MergeSummary(True, True, True), merged_revision="abc")
    assert [s["action"] for s in merged] == ["refresh"]
    assert "abc" in merged[0]["detail"]
