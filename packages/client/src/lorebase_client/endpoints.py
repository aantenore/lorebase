"""One builder per API endpoint, returning a `Call` that describes the request.

The sync and async clients expose these through resource objects; nothing here does I/O.
Paths are relative to the API prefix. List builders take `limit` and `cursor`; GET reads that
carry an ETag are marked `cacheable`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from lorebase_client import models as m
from lorebase_client.core import (
    Call,
    parse_json,
    parse_model,
    parse_none,
    parse_text,
)


def _seg(value: str) -> str:
    return quote(value, safe="")


def _branch(name: str) -> str:
    """Branch names are hierarchical: slashes stay in the path."""
    return quote(name, safe="/")


def _domain(domain: str) -> str:
    return f"/domains/{_seg(domain)}"


def _csv(values: Sequence[str] | None) -> str | None:
    return None if not values else ",".join(values)


# Identity ---------------------------------------------------------------------------------


def whoami() -> Call[m.WhoAmI]:
    return Call("GET", "/auth/whoami", parse_model(m.WhoAmI), safe=True)


# Domains ----------------------------------------------------------------------------------


def create_domain(body: m.DomainCreate, *, idempotency_key: str | None = None) -> Call[m.Domain]:
    return Call(
        "POST",
        "/domains",
        parse_model(m.Domain),
        body=body.payload(),
        accepts_key=True,
        idempotency_key=idempotency_key,
    )


def list_domains(*, limit: int | None = None, cursor: str | None = None) -> Call[m.Page[m.Domain]]:
    return Call(
        "GET",
        "/domains",
        parse_model(m.Page[m.Domain]),
        params={"limit": limit, "cursor": cursor},
        safe=True,
    )


def get_domain(domain: str) -> Call[m.Domain]:
    return Call("GET", _domain(domain), parse_model(m.Domain), safe=True)


def update_domain(domain: str, body: m.DomainUpdate) -> Call[m.Domain]:
    return Call("PATCH", _domain(domain), parse_model(m.Domain), body=body.payload())


def delete_domain(domain: str, *, confirm: str) -> Call[None]:
    return Call("DELETE", _domain(domain), parse_none, params={"confirm": confirm}, safe=True)


# Branches and tags ------------------------------------------------------------------------


def list_branches(
    domain: str, *, limit: int | None = None, cursor: str | None = None
) -> Call[m.Page[m.Branch]]:
    return Call(
        "GET",
        f"{_domain(domain)}/branches",
        parse_model(m.Page[m.Branch]),
        params={"limit": limit, "cursor": cursor},
        safe=True,
    )


def create_branch(
    domain: str, body: m.BranchCreate, *, idempotency_key: str | None = None
) -> Call[m.Branch]:
    return Call(
        "POST",
        f"{_domain(domain)}/branches",
        parse_model(m.Branch),
        body=body.payload(),
        accepts_key=True,
        idempotency_key=idempotency_key,
    )


def get_branch(domain: str, branch: str) -> Call[m.BranchDetail]:
    return Call(
        "GET",
        f"{_domain(domain)}/branches/{_branch(branch)}",
        parse_model(m.BranchDetail),
        safe=True,
    )


def protect_branch(domain: str, branch: str, *, protected: bool) -> Call[m.Branch]:
    return Call(
        "PATCH",
        f"{_domain(domain)}/branches/{_branch(branch)}",
        parse_model(m.Branch),
        body=m.BranchUpdate(protected=protected).payload(),
        safe=True,
    )


def delete_branch(domain: str, branch: str) -> Call[None]:
    return Call("DELETE", f"{_domain(domain)}/branches/{_branch(branch)}", parse_none, safe=True)


def list_tags(
    domain: str, *, limit: int | None = None, cursor: str | None = None
) -> Call[m.Page[m.Tag]]:
    return Call(
        "GET",
        f"{_domain(domain)}/tags",
        parse_model(m.Page[m.Tag]),
        params={"limit": limit, "cursor": cursor},
        safe=True,
    )


def create_tag(
    domain: str, body: m.TagCreate, *, idempotency_key: str | None = None
) -> Call[m.Tag]:
    return Call(
        "POST",
        f"{_domain(domain)}/tags",
        parse_model(m.Tag),
        body=body.payload(),
        accepts_key=True,
        idempotency_key=idempotency_key,
    )


# Schema -----------------------------------------------------------------------------------


def get_schema(
    domain: str, *, branch: str | None = None, at: str | None = None
) -> Call[m.SchemaDocument]:
    return Call(
        "GET",
        f"{_domain(domain)}/schema",
        parse_model(m.SchemaDocument),
        params={"branch": branch, "at": at, "format": "json"},
        safe=True,
        cacheable=True,
    )


def get_schema_yaml(domain: str, *, branch: str | None = None, at: str | None = None) -> Call[str]:
    return Call(
        "GET",
        f"{_domain(domain)}/schema",
        parse_text,
        params={"branch": branch, "at": at, "format": "yaml"},
        accept="text/yaml",
        safe=True,
        cacheable=True,
    )


def get_schema_element(
    domain: str,
    section: m.SchemaSection,
    name: str,
    *,
    branch: str | None = None,
    at: str | None = None,
) -> Call[dict[str, Any]]:
    return Call(
        "GET",
        f"{_domain(domain)}/schema/{section}/{_seg(name)}",
        parse_json,
        params={"branch": branch, "at": at},
        safe=True,
    )


def validate_schema(domain: str, body: m.SchemaValidateRequest) -> Call[m.SchemaValidation]:
    return Call(
        "POST",
        f"{_domain(domain)}/schema:validate",
        parse_model(m.SchemaValidation),
        body=body.payload(),
        safe=True,
    )


def apply_schema(
    domain: str, branch: str, body: m.SchemaChangeRequest
) -> Call[m.SchemaChangeResult]:
    return Call(
        "POST",
        f"{_domain(domain)}/branches/{_branch(branch)}/schema/changes",
        parse_model(m.SchemaChangeResult),
        body=body.payload(),
    )


# Operations -------------------------------------------------------------------------------


def execute_operation(
    domain: str,
    operation: str,
    body: m.ExecuteRequest,
    *,
    idempotency_key: str | None = None,
) -> Call[m.Execution]:
    return Call(
        "POST",
        f"{_domain(domain)}/operations/{_seg(operation)}/execute",
        parse_model(m.Execution),
        body=body.payload(),
        accepts_key=True,
        idempotency_key=idempotency_key,
    )


# Reads ------------------------------------------------------------------------------------


def _order(order_by: Sequence[str] | None) -> str | None:
    return _csv(order_by)


def list_records(
    domain: str,
    kind: str,
    *,
    branch: str | None = None,
    at: str | None = None,
    where: Mapping[str, Any] | None = None,
    order_by: Sequence[str] | None = None,
    select: Sequence[str] | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.RecordPage]:
    return Call(
        "GET",
        f"{_domain(domain)}/kinds/{_seg(kind)}/records",
        parse_model(m.RecordPage),
        params={
            "branch": branch,
            "at": at,
            "where": None if not where else json.dumps(where, separators=(",", ":")),
            "order_by": _order(order_by),
            "select": _csv(select),
            "limit": limit,
            "cursor": cursor,
        },
        safe=True,
        cacheable=True,
    )


def get_record(
    domain: str,
    kind: str,
    key: str,
    *,
    branch: str | None = None,
    at: str | None = None,
    links: bool = False,
    lineage: bool = False,
) -> Call[m.RecordResponse]:
    return Call(
        "GET",
        f"{_domain(domain)}/kinds/{_seg(kind)}/records/{_seg(key)}",
        parse_model(m.RecordResponse),
        params={
            "branch": branch,
            "at": at,
            "links": links or None,
            "lineage": lineage or None,
        },
        safe=True,
        cacheable=True,
    )


def list_record_links(
    domain: str,
    kind: str,
    key: str,
    relation: str,
    *,
    branch: str | None = None,
    at: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.LinkPage]:
    return Call(
        "GET",
        f"{_domain(domain)}/kinds/{_seg(kind)}/records/{_seg(key)}/links/{_seg(relation)}",
        parse_model(m.LinkPage),
        params={"branch": branch, "at": at, "limit": limit, "cursor": cursor},
        safe=True,
        cacheable=True,
    )


def list_relation_links(
    domain: str,
    relation: str,
    *,
    branch: str | None = None,
    at: str | None = None,
    from_key: str | None = None,
    to_key: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.LinkPage]:
    return Call(
        "GET",
        f"{_domain(domain)}/relations/{_seg(relation)}/links",
        parse_model(m.LinkPage),
        params={
            "branch": branch,
            "at": at,
            "from_key": from_key,
            "to_key": to_key,
            "limit": limit,
            "cursor": cursor,
        },
        safe=True,
        cacheable=True,
    )


def query(domain: str, body: m.QueryRequest) -> Call[m.QueryResult]:
    return Call(
        "POST",
        f"{_domain(domain)}/query",
        parse_model(m.QueryResult),
        body=body.payload(),
        safe=True,
    )


def traverse(domain: str, body: m.TraverseRequest) -> Call[m.TraverseResult]:
    return Call(
        "POST",
        f"{_domain(domain)}/traverse",
        parse_model(m.TraverseResult),
        body=body.payload(),
        safe=True,
    )


def neighborhood(domain: str, body: m.NeighborhoodRequest) -> Call[m.NeighborhoodResult]:
    return Call(
        "POST",
        f"{_domain(domain)}/neighborhood",
        parse_model(m.NeighborhoodResult),
        body=body.payload(),
        safe=True,
    )


def search(domain: str, body: m.SearchRequest) -> Call[m.SearchResult]:
    return Call(
        "POST",
        f"{_domain(domain)}/search",
        parse_model(m.SearchResult),
        body=body.payload(),
        safe=True,
    )


# Revisions, log and diff ------------------------------------------------------------------


def branch_log(
    domain: str,
    branch: str,
    *,
    all_parents: bool = False,
    origin_type: str | None = None,
    author: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.LogPage]:
    return Call(
        "GET",
        f"{_domain(domain)}/branches/{_branch(branch)}/log",
        parse_model(m.LogPage),
        params={
            "all": all_parents or None,
            "origin_type": origin_type,
            "author": author,
            "limit": limit,
            "cursor": cursor,
        },
        safe=True,
        cacheable=True,
    )


def get_revision(domain: str, revision: str) -> Call[m.Revision]:
    return Call(
        "GET",
        f"{_domain(domain)}/revisions/{_seg(revision)}",
        parse_model(m.Revision),
        safe=True,
        cacheable=True,
    )


def revision_counts(domain: str, revision: str) -> Call[m.RevisionCounts]:
    return Call(
        "GET",
        f"{_domain(domain)}/revisions/{_seg(revision)}/counts",
        parse_model(m.RevisionCounts),
        safe=True,
        cacheable=True,
    )


def revision_changeset(
    domain: str, revision: str, *, limit: int | None = None, cursor: str | None = None
) -> Call[m.ChangesPage]:
    return Call(
        "GET",
        f"{_domain(domain)}/revisions/{_seg(revision)}/changeset",
        parse_model(m.ChangesPage),
        params={"limit": limit, "cursor": cursor},
        safe=True,
        cacheable=True,
    )


def diff(
    domain: str,
    from_: str,
    to: str,
    *,
    kinds: Sequence[str] | None = None,
    relations: Sequence[str] | None = None,
    schema: bool | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.ChangesPage]:
    return Call(
        "GET",
        f"{_domain(domain)}/diff",
        parse_model(m.ChangesPage),
        params={
            "from": from_,
            "to": to,
            "kinds": list(kinds) if kinds else None,
            "relations": list(relations) if relations else None,
            "schema": schema,
            "limit": limit,
            "cursor": cursor,
        },
        safe=True,
        cacheable=True,
    )


# Administration ---------------------------------------------------------------------------


def create_api_key(domain: str, body: m.CreateApiKey) -> Call[m.ApiKeyCreated]:
    return Call(
        "POST", f"{_domain(domain)}/api-keys", parse_model(m.ApiKeyCreated), body=body.payload()
    )


def list_api_keys(
    domain: str, *, limit: int | None = None, cursor: str | None = None
) -> Call[m.Page[m.ApiKey]]:
    return Call(
        "GET",
        f"{_domain(domain)}/api-keys",
        parse_model(m.Page[m.ApiKey]),
        params={"limit": limit, "cursor": cursor},
        safe=True,
    )


def revoke_api_key(domain: str, key_id: str) -> Call[None]:
    return Call("DELETE", f"{_domain(domain)}/api-keys/{_seg(key_id)}", parse_none, safe=True)


def list_principals(
    *, type_: str | None = None, limit: int | None = None, cursor: str | None = None
) -> Call[m.Page[m.Principal]]:
    return Call(
        "GET",
        "/principals",
        parse_model(m.Page[m.Principal]),
        params={"type": type_, "limit": limit, "cursor": cursor},
        safe=True,
    )


def create_grant(domain: str, body: m.CreateGrant) -> Call[m.Grant]:
    return Call("POST", f"{_domain(domain)}/grants", parse_model(m.Grant), body=body.payload())


def list_grants(
    domain: str, *, limit: int | None = None, cursor: str | None = None
) -> Call[m.Page[m.Grant]]:
    return Call(
        "GET",
        f"{_domain(domain)}/grants",
        parse_model(m.Page[m.Grant]),
        params={"limit": limit, "cursor": cursor},
        safe=True,
    )


def revoke_grant(domain: str, grant_id: str) -> Call[None]:
    return Call("DELETE", f"{_domain(domain)}/grants/{_seg(grant_id)}", parse_none, safe=True)


def list_audit(
    domain: str, *, limit: int | None = None, cursor: str | None = None
) -> Call[m.Page[m.AuditEntry]]:
    return Call(
        "GET",
        f"{_domain(domain)}/audit",
        parse_model(m.Page[m.AuditEntry]),
        params={"limit": limit, "cursor": cursor},
        safe=True,
    )
