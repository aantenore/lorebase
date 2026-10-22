"""One builder per API endpoint, returning a `Call` that describes the request.

The sync and async clients expose these through resource objects; nothing here does I/O.
Paths are relative to the API prefix. List builders take `limit` and `cursor`; GET reads that
carry an ETag are marked `cacheable`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from lorebase_client import models as m
from lorebase_client.core import (
    Call,
    parse_json,
    parse_model,
    parse_none,
    parse_text,
)

_ACCEPTED = 202


_DOT_SEGMENTS = frozenset({"", ".", ".."})


def _seg(value: str) -> str:
    """One path segment, percent-encoded. Empty and dot segments are refused: an HTTP client
    or proxy would resolve them and send the request to another route."""
    if value in _DOT_SEGMENTS:
        raise ValueError(f"invalid path segment {value!r}")
    return quote(value, safe="")


def _branch(name: str) -> str:
    """Branch names are hierarchical: slashes stay in the path, but no part may be empty or a
    dot segment."""
    if any(part in _DOT_SEGMENTS for part in name.split("/")):
        raise ValueError(f"invalid branch name {name!r}")
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


def rebase_branch_tags(
    domain: str, branch: str, body: m.RebaseTagsRequest
) -> Call[m.RebaseTagsResult]:
    """Give a branch the sensitivity tags of the default branch (or, with `rows_only`, queue
    the rewrite of its stale titles and search vectors)."""
    return Call(
        "POST",
        f"{_domain(domain)}/branches/{_branch(branch)}/rebase-tags",
        parse_model(m.RebaseTagsResult),
        body=body.payload(),
    )


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


def infer_schema_postgres(domain: str, body: Mapping[str, Any]) -> Call[dict[str, Any]]:
    """Propose a schema from Postgres tables: `body` is `{"postgres": {...}, "options": {...}}`.

    The answer carries the proposal as YAML in `yaml`; nothing is written.
    """
    return Call(
        "POST",
        f"{_domain(domain)}/schema:infer",
        parse_json,
        params={"format": "json"},
        body=dict(body),
        safe=True,
    )


def infer_schema_files(
    domain: str,
    files: Mapping[str, bytes],
    *,
    options: Mapping[str, Any] | None = None,
    records_path: str | None = None,
) -> Call[dict[str, Any]]:
    """Propose a schema from data files (file name to content); the name is the table.
    `records_path` (dotted keys) locates the records array of JSON documents that hold it under
    a key."""
    form = {} if not options else {"options": json.dumps(options, separators=(",", ":"))}
    if records_path is not None:
        form["records_path"] = records_path
    return Call(
        "POST",
        f"{_domain(domain)}/schema:infer",
        parse_json,
        params={"format": "json"},
        files=tuple(("files", name, data) for name, data in files.items()),
        form=form,
        safe=True,
    )


def preview_files(
    domain: str,
    files: Mapping[str, bytes],
    *,
    rows: int | None = None,
    records_path: str | None = None,
) -> Call[dict[str, Any]]:
    """The first rows of each data file (file name to content) as Keeper reads them; nothing
    is stored. `rows` per file is capped by Keeper's settings."""
    form = {} if records_path is None else {"records_path": records_path}
    return Call(
        "POST",
        f"{_domain(domain)}/schema:preview",
        parse_json,
        params={"rows": rows},
        files=tuple(("files", name, data) for name, data in files.items()),
        form=form,
        safe=True,
    )


def preview_derivation(domain: str, body: Mapping[str, Any]) -> Call[dict[str, Any]]:
    """Evaluate a candidate Derivation on sample Records without saving it: `body` holds the
    change set (`yaml`, or `patch`/`remove`), `derivation` and optionally `branch`, `limit`,
    `keys`. Record-read permissions and masking apply; nothing is written."""
    return Call(
        "POST", f"{_domain(domain)}/derivations:preview", parse_json, body=dict(body), safe=True
    )


def preview_source(domain: str, body: Mapping[str, Any]) -> Call[dict[str, Any]]:
    """Evaluate the expression mappings of a candidate Source on the given `rows`: `body` holds
    the change set, `source` and `rows`. Nothing is read from the Source or written."""
    return Call(
        "POST", f"{_domain(domain)}/sources:preview", parse_json, body=dict(body), safe=True
    )


def list_schema_suggestions(
    domain: str,
    *,
    status: Sequence[m.ItemDecisionValue] | None = None,
    type: Sequence[m.SuggestionType] | None = None,
    all: bool | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.Page[m.SchemaSuggestion]]:
    """Schema suggestions from extraction, the most supported first; `all` lists pending ones
    below the configured support too."""
    return Call(
        "GET",
        f"{_domain(domain)}/schema/suggestions",
        parse_model(m.Page[m.SchemaSuggestion]),
        params={
            "status": list(status or ()),
            "type": list(type or ()),
            "all": all,
            "limit": limit,
            "cursor": cursor,
        },
        safe=True,
    )


def decide_schema_suggestions(domain: str, body: m.DecideSuggestions) -> Call[m.SuggestionsDecided]:
    """Accepting opens a Proposal on a new branch: not safe to repeat blindly."""
    return Call(
        "POST",
        f"{_domain(domain)}/schema/suggestions:decide",
        parse_model(m.SuggestionsDecided),
        body=body.payload(),
    )


def merge_schema_suggestions(domain: str) -> Call[m.SuggestionsMerged]:
    """Fold stored variants of Attribute suggestions; repeating it changes nothing."""
    return Call(
        "POST",
        f"{_domain(domain)}/schema/suggestions:merge",
        parse_model(m.SuggestionsMerged),
        safe=True,
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


def _execution_or_proposal(response: httpx.Response) -> m.Execution | m.AgentWrite:
    """202 answers an Operation that needed review: the Proposal it opened."""
    if response.status_code == _ACCEPTED:
        return m.AgentWrite.model_validate_json(response.content)
    return m.Execution.model_validate_json(response.content)


def execute_operation_as_agent(
    domain: str,
    operation: str,
    body: m.ExecuteRequest,
    *,
    idempotency_key: str | None = None,
) -> Call[m.Execution | m.AgentWrite]:
    """Like `execute_operation`, for requests that may come back as a Proposal."""
    return Call(
        "POST",
        f"{_domain(domain)}/operations/{_seg(operation)}/execute",
        _execution_or_proposal,
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


def record_history(
    domain: str,
    kind: str,
    key: str,
    *,
    branch: str | None = None,
    values: bool = False,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.RecordHistory]:
    return Call(
        "GET",
        f"{_domain(domain)}/kinds/{_seg(kind)}/records/{_seg(key)}/history",
        parse_model(m.RecordHistory),
        params={"branch": branch, "values": values or None, "limit": limit, "cursor": cursor},
        safe=True,
        cacheable=True,
    )


def record_lineage(
    domain: str, kind: str, key: str, *, branch: str | None = None
) -> Call[m.RecordLineage]:
    return Call(
        "GET",
        f"{_domain(domain)}/kinds/{_seg(kind)}/records/{_seg(key)}/lineage",
        parse_model(m.RecordLineage),
        params={"branch": branch},
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


def erase_vault(domain: str, body: m.EraseRequest) -> Call[m.EraseResult]:
    return Call(
        "POST", f"{_domain(domain)}/vault/erase", parse_model(m.EraseResult), body=body.payload()
    )


def vault_keys() -> Call[m.VaultKeys]:
    return Call("GET", "/vault/keys", parse_model(m.VaultKeys), safe=True)


def rotate_vault() -> Call[m.VaultRotation]:
    return Call("POST", "/vault/rotate", parse_model(m.VaultRotation))


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


def verify_audit(domain: str, *, after_seq: int | None = None) -> Call[m.AuditVerification]:
    return Call(
        "GET",
        f"{_domain(domain)}/audit/verify",
        parse_model(m.AuditVerification),
        params={"after_seq": after_seq},
        safe=True,
    )


# Proposals --------------------------------------------------------------------------------


def create_proposal(
    domain: str, body: m.ProposalCreate, *, idempotency_key: str | None = None
) -> Call[m.ProposalDetail]:
    return Call(
        "POST",
        f"{_domain(domain)}/proposals",
        parse_model(m.ProposalDetail),
        body=body.payload(),
        accepts_key=True,
        idempotency_key=idempotency_key,
    )


def list_proposals(
    domain: str,
    *,
    status: m.ProposalStatus | None = None,
    origin: m.ProposalOrigin | None = None,
    source: str | None = None,
    target: str | None = None,
    author: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.Page[m.Proposal]]:
    return Call(
        "GET",
        f"{_domain(domain)}/proposals",
        parse_model(m.Page[m.Proposal]),
        params={
            "status": status,
            "origin": origin,
            "source": source,
            "target": target,
            "author": author,
            "limit": limit,
            "cursor": cursor,
        },
        safe=True,
    )


def get_proposal(domain: str, proposal_id: str) -> Call[m.ProposalDetail]:
    return Call(
        "GET",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}",
        parse_model(m.ProposalDetail),
        safe=True,
    )


def update_proposal(
    domain: str, proposal_id: str, body: m.ProposalUpdate
) -> Call[m.ProposalDetail]:
    return Call(
        "PATCH",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}",
        parse_model(m.ProposalDetail),
        body=body.payload(),
        safe=True,
    )


def close_proposal(domain: str, proposal_id: str) -> Call[m.ProposalDetail]:
    return Call(
        "POST",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}/close",
        parse_model(m.ProposalDetail),
    )


def review_proposal(domain: str, proposal_id: str, body: m.ReviewIn) -> Call[m.ReviewResult]:
    return Call(
        "POST",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}/reviews",
        parse_model(m.ReviewResult),
        body=body.payload(),
    )


def merge_proposal(
    domain: str,
    proposal_id: str,
    body: m.MergeIn | None = None,
    *,
    idempotency_key: str | None = None,
) -> Call[m.MergeResult]:
    return Call(
        "POST",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}/merge",
        parse_model(m.MergeResult),
        body=None if body is None else body.payload(),
        accepts_key=True,
        idempotency_key=idempotency_key,
    )


def list_conflicts(
    domain: str,
    proposal_id: str,
    *,
    kind: str | None = None,
    status: m.ConflictStatus | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.Conflicts]:
    return Call(
        "GET",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}/conflicts",
        parse_model(m.Conflicts),
        params={"kind": kind, "status": status, "limit": limit, "cursor": cursor},
        safe=True,
    )


def resolve_conflict(
    domain: str, proposal_id: str, conflict_id: str, body: m.ResolutionIn
) -> Call[m.Resolution]:
    return Call(
        "POST",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}/conflicts/{_seg(conflict_id)}/resolution",
        parse_model(m.Resolution),
        body=body.payload(),
        safe=True,
    )


def resolve_conflicts(domain: str, proposal_id: str, body: m.ResolveManyIn) -> Call[m.Conflicts]:
    return Call(
        "POST",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}/conflicts:resolve",
        parse_model(m.Conflicts),
        body=body.payload(),
        safe=True,
    )


def list_proposal_items(
    domain: str,
    proposal_id: str,
    *,
    status: Sequence[m.ItemDecisionValue] | None = None,
    kind: Sequence[str] | None = None,
    type: Sequence[m.ItemType] | None = None,
    min_confidence: float | None = None,
    max_confidence: float | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.Page[m.ProposalItem]]:
    """Items of an extraction Proposal; a filter given several values matches any of them."""
    return Call(
        "GET",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}/items",
        parse_model(m.Page[m.ProposalItem]),
        params={
            "status": list(status or ()),
            "kind": list(kind or ()),
            "type": list(type or ()),
            "min_confidence": min_confidence,
            "max_confidence": max_confidence,
            "limit": limit,
            "cursor": cursor,
        },
        safe=True,
    )


def decide_proposal_items(
    domain: str, proposal_id: str, body: m.DecideItems
) -> Call[m.ItemsDecided]:
    """Safe to repeat: the same decisions leave the same state."""
    return Call(
        "POST",
        f"{_domain(domain)}/proposals/{_seg(proposal_id)}/items:decide",
        parse_model(m.ItemsDecided),
        body=body.payload(),
        safe=True,
    )


# Agents -----------------------------------------------------------------------------------


def context_pack(domain: str, body: m.ContextRequest) -> Call[m.ContextPack]:
    request = body.model_copy(update={"format": "json"})
    return Call(
        "POST",
        f"{_domain(domain)}/context",
        parse_model(m.ContextPack),
        body=request.payload(),
        safe=True,
    )


def context_pack_markdown(domain: str, body: m.ContextRequest) -> Call[str]:
    request = body.model_copy(update={"format": "markdown"})
    return Call(
        "POST",
        f"{_domain(domain)}/context",
        parse_text,
        body=request.payload(),
        accept="text/markdown",
        safe=True,
    )


def explain_record(
    domain: str, kind: str, key: str, *, branch: str | None = None
) -> Call[m.Explain]:
    return Call(
        "GET",
        f"{_domain(domain)}/kinds/{_seg(kind)}/records/{_seg(key)}/explain",
        parse_model(m.Explain),
        params={"branch": branch, "format": "json"},
        safe=True,
        cacheable=True,
    )


def explain_record_markdown(
    domain: str, kind: str, key: str, *, branch: str | None = None
) -> Call[str]:
    return Call(
        "GET",
        f"{_domain(domain)}/kinds/{_seg(kind)}/records/{_seg(key)}/explain",
        parse_text,
        params={"branch": branch, "format": "markdown"},
        accept="text/markdown",
        safe=True,
        cacheable=True,
    )


def domain_health(domain: str, *, branch: str | None = None) -> Call[m.DomainHealth]:
    return Call(
        "GET",
        f"{_domain(domain)}/health",
        parse_model(m.DomainHealth),
        params={"branch": branch, "format": "json"},
        safe=True,
        cacheable=True,
    )


def domain_health_markdown(domain: str, *, branch: str | None = None) -> Call[str]:
    return Call(
        "GET",
        f"{_domain(domain)}/health",
        parse_text,
        params={"branch": branch, "format": "markdown"},
        accept="text/markdown",
        safe=True,
        cacheable=True,
    )


def kind_health(domain: str, kind: str, *, branch: str | None = None) -> Call[m.KindHealthReport]:
    return Call(
        "GET",
        f"{_domain(domain)}/kinds/{_seg(kind)}/health",
        parse_model(m.KindHealthReport),
        params={"branch": branch, "format": "json"},
        safe=True,
        cacheable=True,
    )


def kind_health_markdown(domain: str, kind: str, *, branch: str | None = None) -> Call[str]:
    return Call(
        "GET",
        f"{_domain(domain)}/kinds/{_seg(kind)}/health",
        parse_text,
        params={"branch": branch, "format": "markdown"},
        accept="text/markdown",
        safe=True,
        cacheable=True,
    )


def branch_changes(
    domain: str,
    branch: str,
    *,
    since: str | None = None,
    kinds: Sequence[str] | None = None,
    limit: int | None = None,
) -> Call[m.ChangesFeed]:
    return Call(
        "GET",
        f"{_domain(domain)}/branches/{_branch(branch)}/changes",
        parse_model(m.ChangesFeed),
        params={"since": since, "kinds": list(kinds) if kinds else None, "limit": limit},
        safe=True,
        cacheable=True,
    )


def branch_events(
    domain: str,
    branch: str,
    *,
    kinds: Sequence[str] | None = None,
    last_event_id: str | None = None,
) -> Call[str]:
    """The Server-Sent Events stream of a branch, as text.

    The answer is read until the server closes the stream, so the call returns only then (or
    fails at the client timeout); use `last_event_id` to resume after the last event seen.
    """
    return Call(
        "GET",
        f"{_domain(domain)}/branches/{_branch(branch)}/events",
        parse_text,
        params={"kinds": list(kinds) if kinds else None, "last_event_id": last_event_id},
        accept="text/event-stream",
        safe=True,
    )


# Webhooks ---------------------------------------------------------------------------------


def create_webhook(domain: str, body: m.CreateWebhook) -> Call[m.WebhookCreated]:
    return Call(
        "POST", f"{_domain(domain)}/webhooks", parse_model(m.WebhookCreated), body=body.payload()
    )


def list_webhooks(domain: str) -> Call[m.WebhookList]:
    return Call("GET", f"{_domain(domain)}/webhooks", parse_model(m.WebhookList), safe=True)


def update_webhook(domain: str, webhook_id: str, body: m.UpdateWebhook) -> Call[m.Webhook]:
    """Idempotent: the body is the full new set of clearances."""
    return Call(
        "PATCH",
        f"{_domain(domain)}/webhooks/{_seg(webhook_id)}",
        parse_model(m.Webhook),
        body=body.payload(),
        safe=True,
    )


def delete_webhook(domain: str, webhook_id: str) -> Call[None]:
    return Call("DELETE", f"{_domain(domain)}/webhooks/{_seg(webhook_id)}", parse_none, safe=True)


# Sources ----------------------------------------------------------------------------------


def sync_source(domain: str, source: str, body: m.SyncRequest) -> Call[m.SyncQueued]:
    """Not marked safe: a retry may queue a second job once the first one has finished."""
    return Call(
        "POST",
        f"{_domain(domain)}/sources/{_seg(source)}/sync",
        parse_model(m.SyncQueued),
        body=body.payload(),
    )


def list_source_runs(
    domain: str, source: str, *, limit: int | None = None, cursor: str | None = None
) -> Call[m.Page[m.SourceRun]]:
    return Call(
        "GET",
        f"{_domain(domain)}/sources/{_seg(source)}/runs",
        parse_model(m.Page[m.SourceRun]),
        params={"limit": limit, "cursor": cursor},
        safe=True,
    )


# Documents --------------------------------------------------------------------------------


def upload_document(
    domain: str,
    file: str | os.PathLike[str] | bytes,
    *,
    filename: str | None = None,
    extract: bool | None = None,
    tiers: Sequence[int] | None = None,
) -> Call[m.DocumentUploaded]:
    """Upload a document: a path (read here, named after the file) or bytes with a `filename`.

    Not marked safe: a retry may queue a second extraction job.
    """
    if isinstance(file, bytes):
        if not filename:
            raise ValueError("a filename is required to upload bytes")
        name, content = filename, file
    else:
        path = Path(file)
        name, content = filename or path.name, path.read_bytes()
    form: dict[str, str] = {}
    if extract is not None:
        form["extract"] = "true" if extract else "false"
    if tiers is not None:
        form["tiers"] = json.dumps(list(tiers), separators=(",", ":"))
    return Call(
        "POST",
        f"{_domain(domain)}/documents",
        parse_model(m.DocumentUploaded),
        files=(("file", name, content),),
        form=form,
    )


def upload_document_url(domain: str, body: m.DocumentFromUrl) -> Call[m.DocumentUploaded]:
    """Have the server fetch the document from an allowlisted URL. Not marked safe."""
    return Call(
        "POST",
        f"{_domain(domain)}/documents",
        parse_model(m.DocumentUploaded),
        body=body.payload(),
    )


def list_documents(
    domain: str, *, limit: int | None = None, cursor: str | None = None
) -> Call[m.Page[m.Document]]:
    return Call(
        "GET",
        f"{_domain(domain)}/documents",
        parse_model(m.Page[m.Document]),
        params={"limit": limit, "cursor": cursor},
        safe=True,
    )


def get_document(domain: str, document_id: str) -> Call[m.DocumentDetail]:
    return Call(
        "GET",
        f"{_domain(domain)}/documents/{_seg(document_id)}",
        parse_model(m.DocumentDetail),
        safe=True,
    )


def get_document_text(domain: str, document_id: str) -> Call[m.DocumentText]:
    return Call(
        "GET",
        f"{_domain(domain)}/documents/{_seg(document_id)}/text",
        parse_model(m.DocumentText),
        safe=True,
    )


# Agent trace ------------------------------------------------------------------------------


def list_trace_sessions(
    *,
    actor: str | None = None,
    domain_id: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Call[m.TraceSessionPage]:
    return Call(
        "GET",
        "/trace/sessions",
        parse_model(m.TraceSessionPage),
        params={"actor": actor, "domain_id": domain_id, "limit": limit, "cursor": cursor},
        safe=True,
    )


def get_trace_session(session_id: str, *, actor: str | None = None) -> Call[m.TraceTimeline]:
    return Call(
        "GET",
        f"/trace/sessions/{_seg(session_id)}",
        parse_model(m.TraceTimeline),
        params={"actor": actor},
        safe=True,
    )


def add_trace_note(session_id: str, body: m.NoteRequest) -> Call[m.NoteAccepted]:
    return Call(
        "POST",
        f"/trace/sessions/{_seg(session_id)}/notes",
        parse_model(m.NoteAccepted),
        body=body.payload(),
    )
