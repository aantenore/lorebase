"""The client models and endpoint builders agree with Keeper's OpenAPI document.

`docs/openapi.json` is the committed document (a tooling test keeps it equal to what the
application generates). Response models must have exactly the members the document lists,
request models no member the document does not know, and every documented operation must be
reachable through an endpoint builder.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from lorebase_client import endpoints as ep
from lorebase_client import models as m

DOCUMENT = json.loads((Path(__file__).resolve().parents[3] / "docs" / "openapi.json").read_text())
SCHEMAS: dict[str, Any] = DOCUMENT["components"]["schemas"]
# The JSON body of the document upload is declared inline, not as a component.
SCHEMAS["DocumentFromUrl"] = DOCUMENT["paths"]["/api/v1/domains/{domain_id}/documents"]["post"][
    "requestBody"
]["content"]["application/json"]["schema"]
PREFIX = "/api/v1"

RESPONSES: dict[type[m.Model], str] = {
    m.WhoAmI: "WhoAmI",
    m.SessionInfo: "SessionInfo",
    m.QuotaReport: "QuotaReport",
    m.QuotaUse: "QuotaUse",
    m.HealthSummary: "HealthSummary",
    m.DomainHealth: "DomainHealthOut",
    m.KindHealthReport: "KindHealthOut",
    m.Domain: "DomainOut",
    m.Branch: "BranchOut",
    m.BranchDetail: "BranchDetail",
    m.Tag: "TagOut",
    m.SchemaDocument: "SchemaOut",
    m.SchemaSearch: "SchemaSearchOut",
    m.EmbeddingModel: "EmbeddingOut",
    m.ElementChange: "ElementChangeOut",
    m.SchemaChangeResult: "SchemaChangeOut",
    m.SchemaValidation: "SchemaValidationOut",
    m.Execution: "ExecutionOut",
    m.AgentWrite: "AgentWriteOut",
    m.ProposalRef: "ProposalRefOut",
    m.AgentApproval: "AgentApprovalOut",
    m.AgentAttempt: "AgentAttemptOut",
    m.AgentMerge: "AgentMergeOut",
    m.NextHint: "NextHint",
    m.LinkCount: "LinkCountOut",
    m.Lineage: "LineageOut",
    m.RecordItem: "RecordOut",
    m.RecordRef: "RecordRef",
    m.Provenance: "ProvenanceOut",
    m.AttributeChange: "AttributeChangeOut",
    m.RecordChange: "RecordChangeOut",
    m.RecordHistory: "RecordHistoryOut",
    m.LineageProposal: "LineageProposalOut",
    m.ExtractionEvidence: "ExtractionEvidenceOut",
    m.ValueLineage: "ValueLineageOut",
    m.RecordLineage: "RecordLineageOut",
    m.RecordResponse: "RecordResponse",
    m.RecordPage: "RecordPage",
    m.AggregateCount: "AggregateCount",
    m.Aggregate: "AggregateOut",
    m.QueryResult: "QueryResponse",
    m.LinkItem: "LinkOut",
    m.LinkPage: "LinkPage",
    m.Edge: "Edge",
    m.TraverseHop: "TraverseHopOut",
    m.TraverseResult: "TraverseResponse",
    m.NeighborhoodResult: "NeighborhoodResponse",
    m.SearchHit: "SearchHit",
    m.SearchCorrection: "SearchCorrection",
    m.SearchResult: "SearchResponse",
    m.ResolveCandidate: "ResolveCandidate",
    m.ResolveSkipped: "ResolveSkipped",
    m.ResolveResult: "ResolveResponse",
    m.Coverage: "CoverageOut",
    m.AttributeCoverage: "AttributeCoverage",
    m.SourceFreshness: "SourceFreshness",
    m.KindCoverage: "KindCoverage",
    m.Revision: "RevisionOut",
    m.LogPage: "LogPage",
    m.PartitionCount: "PartitionCount",
    m.RevisionCounts: "CountsResponse",
    m.ValueChange: "ValueChange",
    m.Change: "ChangeOut",
    m.PartitionSummary: "PartitionSummary",
    m.ChangesPage: "ChangesPage",
    m.ApiKey: "ApiKeyOut",
    m.ApiKeyCreated: "ApiKeyCreated",
    m.EraseResult: "EraseOut",
    m.VaultKeys: "VaultKeysOut",
    m.VaultRotation: "RotationQueued",
    m.RebaseTagsResult: "RebaseTagsOut",
    m.TagChange: "TagChangeOut",
    m.Principal: "PrincipalOut",
    m.Grant: "GrantOut",
    m.AuditEntry: "AuditEntryOut",
    m.AuditVerification: "AuditVerificationOut",
    m.AuditBreak: "AuditBreakOut",
    m.Page[m.Domain]: "Page_DomainOut_",
    m.Page[m.Branch]: "Page_BranchOut_",
    m.Page[m.Tag]: "Page_TagOut_",
    m.Page[m.ApiKey]: "Page_ApiKeyOut_",
    m.Page[m.Principal]: "Page_PrincipalOut_",
    m.Page[m.Grant]: "Page_GrantOut_",
    m.Page[m.AuditEntry]: "Page_AuditEntryOut_",
    m.Check: "CheckOut",
    m.Review: "ReviewOut",
    m.Approval: "ApprovalOut",
    m.Resolution: "ResolutionOut",
    m.Attempt: "AttemptOut",
    m.ConflictItem: "ConflictOut",
    m.Conflicts: "ConflictsOut",
    m.Proposal: "lorebase_keeper__routes__proposals__ProposalOut",
    m.ProposalDetail: "ProposalDetailOut",
    m.ReviewResult: "ReviewResult",
    m.MergeResult: "MergeOut",
    m.Page[m.Proposal]: "Page_ProposalOut_",
    m.ContextPack: "ContextPackOut",
    m.Explain: "ExplainOut",
    m.RevisionEvent: "RevisionEventOut",
    m.ChangesFeed: "ChangesFeedPage",
    m.Webhook: "WebhookOut",
    m.WebhookCreated: "WebhookCreated",
    m.WebhookList: "WebhookList",
    m.SyncQueued: "SyncQueued",
    m.SourceRun: "SourceRunOut",
    m.Page[m.SourceRun]: "Page_SourceRunOut_",
    m.Document: "DocumentOut",
    m.ExtractionJob: "ExtractionJobOut",
    m.DocumentDetail: "DocumentDetailOut",
    m.DocumentUploaded: "DocumentUploaded",
    m.DocumentRetired: "DocumentRetired",
    m.DocumentPurged: "DocumentPurged",
    m.Page[m.Document]: "Page_DocumentOut_",
    m.TraceRecordRef: "TraceRecordRef",
    m.TraceLinkRef: "TraceLinkRef",
    m.TraceRefs: "TraceRefs",
    m.TraceSession: "TraceSessionOut",
    m.TraceSessionPage: "TraceSessionPage",
    m.TraceStep: "TraceStepOut",
    m.TraceTimeline: "TraceTimeline",
    m.NoteAccepted: "NoteAccepted",
    m.ProposalItem: "ItemOut",
    m.Page[m.ProposalItem]: "Page_ItemOut_",
    m.MaterializedItems: "MaterializedOut",
    m.ItemsDecided: "DecideOut",
    m.TypeGuess: "TypeGuessOut",
    m.SuggestionDuplicate: "DuplicateOut",
    m.SchemaSuggestion: "SuggestionOut",
    m.Page[m.SchemaSuggestion]: "Page_SuggestionOut_",
    m.SuggestionProposal: "SuggestionProposalOut",
    m.SuggestionsDecided: "DecideSuggestionsOut",
    m.ReconciliationClaim: "ReconciliationClaimOut",
    m.ReconciliationCandidate: "ReconciliationCandidateOut",
    m.Page[m.ReconciliationCandidate]: "Page_ReconciliationCandidateOut_",
    m.ReconciliationDecision: "ReconciliationDecisionOut",
    m.BulkItem: "ReconciliationBulkItemOut",
    m.BulkDecision: "ReconciliationBulkOut",
    m.SweepQueued: "ReconciliationSweepOut",
}
REQUESTS: dict[type[m.RequestModel], str] = {
    m.EraseRequest: "EraseIn",
    m.RebaseTagsRequest: "RebaseTagsRequest",
    m.DomainCreate: "DomainCreate",
    m.DomainUpdate: "DomainUpdate",
    m.BranchCreate: "BranchCreate",
    m.BranchUpdate: "BranchUpdate",
    m.TagCreate: "TagCreate",
    m.ElementRemoval: "ElementRemoval",
    m.SchemaChangeRequest: "SchemaChangeRequest",
    m.SchemaValidateRequest: "SchemaValidateRequest",
    m.ExecuteRequest: "ExecuteRequest",
    m.QueryRequest: "QueryRequest",
    m.RecordAddress: "RecordAddress",
    m.TraverseStep: "TraverseStep",
    m.TraverseRequest: "TraverseRequest",
    m.NeighborhoodRequest: "NeighborhoodRequest",
    m.SearchRequest: "SearchRequest",
    m.SearchTuning: "SearchTuning",
    m.ResolveRequest: "ResolveRequest",
    m.NoteRequest: "NoteIn",
    m.CreateApiKey: "CreateApiKey",
    m.CreateGrant: "CreateGrant",
    m.ProposalCreate: "ProposalCreate",
    m.ProposalUpdate: "ProposalUpdate",
    m.ReviewIn: "ReviewIn",
    m.MergeIn: "MergeIn",
    m.ResolutionIn: "ResolutionIn",
    m.FingerprintedResolutionIn: "FingerprintedResolutionIn",
    m.ResolveManyIn: "ResolveManyIn",
    m.ContextRequest: "ContextRequest",
    m.CreateWebhook: "CreateWebhook",
    m.SyncRequest: "SyncRequest",
    m.DocumentFromUrl: "DocumentFromUrl",
    m.RetirementIn: "RetirementIn",
    m.PurgeIn: "PurgeIn",
    m.ItemSelection: "ItemSelection",
    m.ItemDecision: "ItemDecisionIn",
    m.DecideItems: "DecideIn",
    m.SuggestionDecision: "SuggestionDecisionIn",
    m.DecideSuggestions: "DecideSuggestionsIn",
    m.ReconcileIn: "ReconciliationResolveIn",
    m.ReasonIn: "ReconciliationReasonIn",
    m.ReconcileFilter: "ReconciliationFilterIn",
    m.BulkReconcileIn: "ReconciliationBulkIn",
    m.SweepRequest: "ReconciliationSweepIn",
}
# Members the server accepts through a Python-only alias or computes; none today.
IGNORED: dict[str, set[str]] = {}


def members(model: type[BaseModel]) -> dict[str, bool]:
    """Member name (by alias) to whether the model requires it."""
    return {
        (field.alias or name): field.is_required() for name, field in model.model_fields.items()
    }


@pytest.mark.parametrize(("model", "schema"), RESPONSES.items(), ids=lambda v: str(v))
def test_response_models_list_the_members_of_the_document(
    model: type[m.Model], schema: str
) -> None:
    documented = SCHEMAS[schema]
    properties = set(documented.get("properties", {}))
    ours = members(model)
    assert set(ours) == properties, f"{model.__name__} vs {schema}"
    required = set(documented.get("required", []))
    # Whatever the client insists on, the server always sends.
    assert {name for name, needed in ours.items() if needed} <= required, model.__name__


@pytest.mark.parametrize(("model", "schema"), REQUESTS.items(), ids=lambda v: str(v))
def test_request_models_send_only_what_the_document_accepts(
    model: type[m.RequestModel], schema: str
) -> None:
    documented = SCHEMAS[schema]
    properties = set(documented.get("properties", {}))
    ours = members(model)
    assert set(ours) <= properties, f"{model.__name__} sends unknown members of {schema}"
    # Everything the server requires, the client requires too.
    assert set(documented.get("required", [])) <= {n for n, needed in ours.items() if needed}
    # And the client leaves nothing out that callers may want to set.
    assert properties - set(ours) <= IGNORED.get(schema, set()), (
        f"{schema} has members the client does not expose"
    )


def test_enumerations_the_client_checks_match_the_document() -> None:
    sections = SCHEMAS["ElementRemoval"]["properties"]["section"]["enum"]
    assert set(sections) == set(get_args(m.SchemaSection))
    modes = SCHEMAS["SearchRequest"]["properties"]["mode"]["anyOf"][0]["enum"]
    assert set(modes) == set(get_args(m.SearchMode))
    assert set(SCHEMAS["GrantScope"]["enum"]) == set(
        get_args(m.CreateGrant.model_fields["scope_type"].annotation)
    )
    for literal, name in [
        (m.ProposalStatus, "ProposalStatus"),
        (m.ProposalOrigin, "ProposalOrigin"),
        (m.ReviewVerdict, "ReviewVerdict"),
    ]:
        assert set(SCHEMAS[name]["enum"]) == set(get_args(literal)), name
    assert set(SCHEMAS["ItemOut"]["properties"]["decision"]["enum"]) == set(
        get_args(m.ItemDecisionValue)
    )
    assert set(SCHEMAS["ItemOut"]["properties"]["type"]["enum"]) == set(get_args(m.ItemType))
    assert set(SCHEMAS["CheckOut"]["properties"]["status"]["enum"]) == set(get_args(m.CheckStatus))
    assert set(SCHEMAS["ConflictOut"]["properties"]["status"]["enum"]) == set(
        get_args(m.ConflictStatus)
    )
    assert set(SCHEMAS["ContextRequest"]["properties"]["format"]["enum"]) == set(
        get_args(m.ContextFormat)
    )
    assert set(SCHEMAS["CandidateType"]["enum"]) == set(get_args(m.CandidateType))
    assert set(SCHEMAS["CandidateStatus"]["enum"]) == set(get_args(m.CandidateStatus))


# Endpoints ----------------------------------------------------------------------------------


def sample_calls() -> list[Any]:
    """One call per builder, with placeholder arguments."""
    d, r = "dom", "rev"
    query = m.QueryRequest(kind="Part")
    return [
        ep.whoami(),
        ep.session_info(),
        ep.create_domain(m.DomainCreate(id="supply")),
        ep.list_domains(),
        ep.get_domain(d),
        ep.update_domain(d, m.DomainUpdate(description="x")),
        ep.delete_domain(d, confirm=d),
        ep.list_branches(d),
        ep.create_branch(d, m.BranchCreate(name="work")),
        ep.get_branch(d, "agent/x"),
        ep.protect_branch(d, "agent/x", protected=True),
        ep.delete_branch(d, "agent/x"),
        ep.list_tags(d),
        ep.create_tag(d, m.TagCreate(name="v1")),
        ep.get_schema(d),
        ep.get_schema_yaml(d),
        ep.get_schema_element(d, "kinds", "Part"),
        ep.validate_schema(d, m.SchemaValidateRequest(yaml="x")),
        ep.apply_schema(d, "agent/x", m.SchemaChangeRequest(yaml="x")),
        ep.execute_operation(d, "import_records", m.ExecuteRequest()),
        ep.execute_operation_as_agent(d, "import_records", m.ExecuteRequest(task="t")),
        ep.list_records(d, "Part"),
        ep.get_record(d, "Part", "P-1"),
        ep.record_history(d, "Part", "P-1"),
        ep.record_lineage(d, "Part", "P-1"),
        ep.list_record_links(d, "Part", "P-1", "supplied_by"),
        ep.list_relation_links(d, "supplied_by"),
        ep.query(d, query),
        ep.traverse(
            d,
            m.TraverseRequest(
                start=m.RecordAddress(kind="Part", key="P"), path=[m.TraverseStep(relation="r")]
            ),
        ),
        ep.neighborhood(d, m.NeighborhoodRequest(seeds=[m.RecordAddress(kind="Part", key="P")])),
        ep.search(d, m.SearchRequest(q="x")),
        ep.resolve(d, m.ResolveRequest(text="x")),
        ep.branch_log(d, "agent/x"),
        ep.get_revision(d, r),
        ep.revision_counts(d, r),
        ep.revision_changeset(d, r),
        ep.diff(d, "a", "b"),
        ep.create_api_key(d, m.CreateApiKey(name="bot")),
        ep.erase_vault(d, m.EraseRequest(kind="Supplier", key="S-1")),
        ep.vault_keys(),
        ep.rotate_vault(),
        ep.rebase_branch_tags(d, "work", m.RebaseTagsRequest()),
        ep.list_api_keys(d),
        ep.revoke_api_key(d, "key"),
        ep.list_principals(),
        ep.create_grant(d, m.CreateGrant(subject="s", role="admin")),
        ep.list_grants(d),
        ep.revoke_grant(d, "g"),
        ep.list_audit(d),
        ep.verify_audit(d),
        ep.list_trace_sessions(),
        ep.get_trace_session("s1"),
        ep.add_trace_note("s1", m.NoteRequest(text="x")),
        ep.infer_schema_postgres(d, {"postgres": {"dsn": "secret://env/ERP_DSN"}}),
        ep.infer_schema_files(d, {"parts.csv": b"id\n1\n"}),
        ep.preview_files(d, {"parts.parquet": b"PAR1"}, rows=5),
        ep.create_proposal(d, m.ProposalCreate(source_ref="a", target_ref="b", title="t")),
        ep.list_proposals(d),
        ep.get_proposal(d, "p"),
        ep.update_proposal(d, "p", m.ProposalUpdate(title="t")),
        ep.close_proposal(d, "p"),
        ep.review_proposal(d, "p", m.ReviewIn(verdict="approve")),
        ep.merge_proposal(d, "p"),
        ep.list_conflicts(d, "p"),
        ep.resolve_conflict(d, "p", "c", m.ResolutionIn(choice="ours")),
        ep.resolve_conflicts(d, "p", m.ResolveManyIn(resolutions=[])),
        ep.context_pack(d, m.ContextRequest(search="x")),
        ep.context_pack_markdown(d, m.ContextRequest(search="x")),
        ep.explain_record(d, "Part", "P-1"),
        ep.explain_record_markdown(d, "Part", "P-1"),
        ep.domain_health(d),
        ep.domain_health_markdown(d),
        ep.kind_health(d, "Part"),
        ep.kind_health_markdown(d, "Part"),
        ep.branch_changes(d, "agent/x"),
        ep.branch_events(d, "agent/x"),
        ep.create_webhook(d, m.CreateWebhook(url="https://example.test/hook")),
        ep.list_webhooks(d),
        ep.update_webhook(d, "w", m.UpdateWebhook(clearances=["pii"])),
        ep.delete_webhook(d, "w"),
        ep.sync_source(d, "erp.parts", m.SyncRequest()),
        ep.list_source_runs(d, "erp.parts"),
        ep.upload_document(d, b"x", filename="a.txt"),
        ep.upload_document_url(d, m.DocumentFromUrl(url="https://example.test/a.pdf")),
        ep.list_documents(d),
        ep.document_versions(d, "doc_1"),
        ep.retire_document(d, "doc_1", m.RetirementIn(reason="x")),
        ep.purge_document(d, "doc_1", m.PurgeIn(reason="x", confirm="doc_1")),
        ep.get_document(d, "doc_1"),
        ep.get_document_text(d, "doc_1"),
        ep.list_proposal_items(d, "prp_1", status=["pending"], min_confidence=0.5),
        ep.decide_proposal_items(d, "prp_1", m.DecideItems()),
        ep.list_schema_suggestions(d, status=["pending"], all=True),
        ep.merge_schema_suggestions(d),
        ep.preview_derivation(d, {"patch": {}, "derivation": "x"}),
        ep.preview_source(d, {"patch": {}, "source": "s", "rows": []}),
        ep.decide_schema_suggestions(
            d, m.DecideSuggestions(decisions=[{"suggestion": "ssg_1", "decision": "rejected"}])
        ),
        ep.list_reconciliation(d, branch="main", type="contradiction", status="open"),
        ep.get_reconciliation_candidate(d, "rcn_1"),
        ep.resolve_reconciliation(d, "rcn_1", m.ReconcileIn(choice="keep_current", reason="r")),
        ep.dismiss_reconciliation(d, "rcn_1", m.ReasonIn(reason="r")),
        ep.reopen_reconciliation(d, "rcn_1", m.ReasonIn(reason="r")),
        ep.resolve_reconciliation_bulk(
            d, m.BulkReconcileIn(ids=["rcn_1"], choice="dismiss", reason="r")
        ),
        ep.queue_reconciliation_sweep(d, m.SweepRequest()),
    ]


def template_pattern(path: str) -> re.Pattern[str]:
    """Regex of an OpenAPI path template; `{branch}` takes hierarchical names."""
    parts = re.split(r"(\{[^}]+\})", path)
    out = []
    for part in parts:
        if part.startswith("{"):
            out.append(".+" if part == "{branch}" else "[^/]+")
        else:
            out.append(re.escape(part))
    return re.compile("^" + "".join(out) + "$")


def operations() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method.upper(), path): spec
        for path, item in DOCUMENT["paths"].items()
        if path.startswith(PREFIX + "/")
        for method, spec in item.items()
    }


def best_match(
    documented: dict[tuple[str, str], dict[str, Any]], method: str, path: str
) -> tuple[str, str] | None:
    """The most specific documented operation a request path belongs to."""
    found = [key for key in documented if key[0] == method and template_pattern(key[1]).match(path)]
    return max(found, key=lambda key: len(key[1])) if found else None


def test_every_builder_targets_a_documented_operation() -> None:
    documented = operations()
    for call in sample_calls():
        full = PREFIX + call.path
        key = best_match(documented, call.method, full)
        assert key is not None, f"{call.method} {full} is not in the document"
        # Query parameters must be ones the operation declares.
        declared = {p["name"] for p in documented[key].get("parameters", [])}
        used = {name for name, value in call.params.items() if value is not None}
        assert used <= declared, f"{call.method} {full}: unknown parameters {used - declared}"


BROWSER_ONLY = {
    ("GET", f"{PREFIX}/auth/login"),
    ("GET", f"{PREFIX}/auth/callback"),
    ("POST", f"{PREFIX}/auth/logout"),
}
"""Web sign-in routes of a browser, not something the client calls."""


def test_every_documented_operation_has_a_builder() -> None:
    documented = operations()
    covered = {best_match(documented, c.method, PREFIX + c.path) for c in sample_calls()}
    missing = sorted(set(documented) - covered - BROWSER_ONLY)  # type: ignore[type-var]
    assert missing == [], f"operations without a client call: {missing}"
