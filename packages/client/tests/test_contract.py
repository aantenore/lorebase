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
PREFIX = "/api/v1"

RESPONSES: dict[type[m.Model], str] = {
    m.WhoAmI: "WhoAmI",
    m.Domain: "DomainOut",
    m.Branch: "BranchOut",
    m.BranchDetail: "BranchDetail",
    m.Tag: "TagOut",
    m.SchemaDocument: "SchemaOut",
    m.ElementChange: "ElementChangeOut",
    m.SchemaChangeResult: "SchemaChangeOut",
    m.SchemaValidation: "SchemaValidationOut",
    m.Execution: "ExecutionOut",
    m.LinkCount: "LinkCountOut",
    m.Lineage: "LineageOut",
    m.RecordItem: "RecordOut",
    m.RecordRef: "RecordRef",
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
    m.SearchResult: "SearchResponse",
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
    m.Principal: "PrincipalOut",
    m.Grant: "GrantOut",
    m.AuditEntry: "AuditEntryOut",
    m.Page[m.Domain]: "Page_DomainOut_",
    m.Page[m.Branch]: "Page_BranchOut_",
    m.Page[m.Tag]: "Page_TagOut_",
    m.Page[m.ApiKey]: "Page_ApiKeyOut_",
    m.Page[m.Principal]: "Page_PrincipalOut_",
    m.Page[m.Grant]: "Page_GrantOut_",
    m.Page[m.AuditEntry]: "Page_AuditEntryOut_",
}
REQUESTS: dict[type[m.RequestModel], str] = {
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
    m.CreateApiKey: "CreateApiKey",
    m.CreateGrant: "CreateGrant",
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


# Endpoints ----------------------------------------------------------------------------------


def sample_calls() -> list[Any]:
    """One call per builder, with placeholder arguments."""
    d, r = "dom", "rev"
    query = m.QueryRequest(kind="Part")
    return [
        ep.whoami(),
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
        ep.list_records(d, "Part"),
        ep.get_record(d, "Part", "P-1"),
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
        ep.branch_log(d, "agent/x"),
        ep.get_revision(d, r),
        ep.revision_counts(d, r),
        ep.revision_changeset(d, r),
        ep.diff(d, "a", "b"),
        ep.create_api_key(d, m.CreateApiKey(name="bot")),
        ep.list_api_keys(d),
        ep.revoke_api_key(d, "key"),
        ep.list_principals(),
        ep.create_grant(d, m.CreateGrant(subject="s", role="admin")),
        ep.list_grants(d),
        ep.revoke_grant(d, "g"),
        ep.list_audit(d),
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


def test_every_documented_operation_has_a_builder() -> None:
    documented = operations()
    covered = {best_match(documented, c.method, PREFIX + c.path) for c in sample_calls()}
    missing = sorted(set(documented) - covered)  # type: ignore[type-var]
    assert missing == [], f"operations without a client call: {missing}"
