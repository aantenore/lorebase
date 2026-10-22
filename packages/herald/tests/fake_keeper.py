"""A fake Keeper on `httpx.MockTransport`: canned JSON per route, every request recorded."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

import httpx

API = "/api/v1/domains/supply"
REVISION = "ab" * 32
PARENT = "cd" * 32
OLDER = "ef" * 32

Answer = dict[str, Any] | httpx.Response | Callable[[httpx.Request], httpx.Response]


def problem(status: int, code: str, detail: str | None, **extensions: Any) -> httpx.Response:
    body: dict[str, Any] = {
        "type": f"https://lorebase.dev/problems/{code}",
        "title": code,
        "status": status,
        "code": code,
    }
    if detail is not None:
        body["detail"] = detail
    body.update(extensions)
    return httpx.Response(status, json=body, headers={"content-type": "application/problem+json"})


@dataclass
class FakeKeeper:
    """Routes keyed by `METHOD /decoded/path`; unknown routes answer 404 problems without a
    `detail`, as Keeper does for a route it does not have."""

    routes: dict[str, Answer] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def on(self, method: str, path: str, answer: Answer) -> None:
        self.routes[f"{method} {path}"] = answer

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.routes.get(f"{request.method} {unquote(request.url.path)}")
        if answer is None:
            return problem(404, "not-found", None)
        if callable(answer):
            return answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def last(self) -> httpx.Request:
        return self.requests[-1]

    def last_body(self) -> Any:
        return json.loads(self.last().content)


SUPPLIER = "knd_supplier"
PART = "knd_part"


def _attribute(attribute_id: str, name: str, type_: dict[str, Any], **extra: Any) -> Any:
    return {
        "id": attribute_id,
        "api_name": name,
        "description": extra.pop("description", f"The {name}."),
        "type": type_,
        "required": extra.pop("required", False),
        **extra,
    }


SCHEMA = {
    "domain": "supply",
    "branch": "main",
    "revision": REVISION,
    "schema": {
        "domain": {"id": "supply", "default_branch": "main"},
        "kinds": {
            SUPPLIER: {
                "id": SUPPLIER,
                "api_name": "Supplier",
                "description": "A company that supplies parts.",
                "primary_key": ["att_code"],
                "title_attribute": "att_name",
                "status": "active",
                "operations_builtin": ["create", "update", "delete"],
                "traits": [],
                "attributes": {
                    "att_code": _attribute(
                        "att_code", "supplier_code", {"name": "string"}, required=True
                    ),
                    "att_name": _attribute(
                        "att_name", "legal_name", {"name": "string"}, required=True
                    ),
                    "att_tier": _attribute(
                        "att_tier", "tier", {"name": "enum", "enum_values": ["1", "2", "3"]}
                    ),
                },
            },
            PART: {
                "id": PART,
                "api_name": "PurchaseOrder",
                "description": "An order.",
                "primary_key": ["att_po"],
                "status": "active",
                "operations_builtin": ["create"],
                "attributes": {
                    "att_po": _attribute("att_po", "po_number", {"name": "string"}, required=True),
                    "att_cost": _attribute(
                        "att_cost", "total", {"name": "decimal", "precision": 12, "scale": 2}
                    ),
                    "att_supplier": _attribute(
                        "att_supplier", "supplier", {"name": "ref", "kind_ref": SUPPLIER}
                    ),
                },
            },
        },
        "relations": {
            "rel_issued": {
                "id": "rel_issued",
                "api_name": "issued_to",
                "inverse_api_name": "purchase_orders",
                "from_kind": PART,
                "to_kind": SUPPLIER,
                "cardinality": "many_to_one",
                "backing": "explicit",
                "description": "The supplier an order is issued to.",
                "attributes": {},
            }
        },
        "traits": {},
        "operations": {
            "opr_flag": {
                "id": "opr_flag",
                "api_name": "flag_supplier_risk",
                "description": "Flag a supplier as a risk.",
                "parameters": [
                    {
                        "name": "supplier",
                        "type": {"name": "ref", "kind_ref": SUPPLIER},
                        "required": True,
                        "description": "The supplier to flag.",
                    }
                ],
                "review_policy": {"mode": "expression", "expression": "true"},
            }
        },
        "derivations": {},
        "sources": {},
    },
}


def served(**members: Any) -> dict[str, Any]:
    """A read answer as Keeper serves it: the Revision it was read at and where from."""
    return {"revision": REVISION, "served_from": "materialized", **members}


def record(kind: str, key: str, title: str, **values: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "key": key,
        "title": title,
        "values": values,
        "updated_revision": REVISION,
        "updated_at": "2026-10-01T08:00:03Z",
    }


SUPPLIER_RECORD = {
    "revision": REVISION,
    "served_from": "materialized",
    "record": {
        **record("Supplier", "S-1001", "Kavsen Alloys", supplier_code="S-1001", tier="1"),
        "links": [
            {"relation": "purchase_orders", "direction": "inverse", "count": 2},
            {"relation": "risk_flags", "direction": "inverse", "count": 0},
        ],
        "lineage": {
            "supplier_code": {
                "provenance_id": "pvn_1",
                "origin_type": "import",
                "actor": "usr_loader",
                "at": "2026-10-01T08:00:03Z",
            }
        },
    },
}


CONTEXT_PACK = {
    "domain": "supply",
    "branch": "main",
    "revision": REVISION,
    "etag": "e1",
    "seeds": [{"ref": "Supplier/S-1001"}],
    "neighbors": [],
    "links": {},
    "budget": {"tokens": 3000},
}
MARKDOWN_PACK = "# Context: Supplier/S-1001\nsupply/main at revision " + REVISION + "\n"


def markdown_pack(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, text=MARKDOWN_PACK, headers={"content-type": "text/markdown; charset=utf-8"}
    )


EXPLAINED = {
    "domain": "supply",
    "branch": "main",
    "revision": REVISION,
    "record": {
        "ref": "Supplier/S-1001",
        "kind": "Supplier",
        "key": "S-1001",
        "title": "Kavsen Alloys",
        "values": {"supplier_code": "S-1001", "tier": "1"},
        "updated_revision": REVISION,
    },
    "created": {
        "revision": PARENT,
        "at": "2026-10-01T08:00:03Z",
        "actor": "usr_loader",
        "origin_type": "import",
        "message": "Import the supply example data",
    },
    "attributes": [
        {
            "name": "supplier_code",
            "value": "S-1001",
            "revision": PARENT,
            "at": "2026-10-01T08:00:03Z",
            "actor": "usr_loader",
            "origin_type": "import",
        },
        {"name": "tier", "value": "1"},
    ],
    "links": [
        {
            "relation": "purchase_orders",
            "direction": "inverse",
            "ref": "PurchaseOrder/PO-1",
            "title": "PO-1",
            "revision": PARENT,
            "at": "2026-10-01T08:00:03Z",
            "actor": "usr_loader",
            "origin_type": "import",
        }
    ],
    "links_truncated": False,
    "history": {"changes": 2},
    "proposals": [
        {"id": "prp_1", "title": "Raise risk", "status": "merged", "merged_revision": REVISION}
    ],
}


PROVENANCE = {
    "id": "prv_1",
    "origin_type": "import",
    "origin": {},
    "actor": "usr_loader",
    "at": "2026-10-01T08:00:03Z",
}
CHANGE = {
    "revision": PARENT,
    "generation": 1,
    "at": "2026-10-01T08:00:03Z",
    "author": "usr_loader",
    "message": "Import the supply example data",
    "origin": {"type": "import"},
    "change": "created",
    "attributes": [
        {"name": "tier", "before": None, "after": "2", "provenance": PROVENANCE},
        {"name": "supplier_code", "after": "S-1001"},
    ],
}
HISTORY = {
    "revision": REVISION,
    "served_from": "tree",
    "branch": "main",
    "kind": "Supplier",
    "key": "S-1001",
    "items": [{**CHANGE, "revision": REVISION, "change": "updated"}, CHANGE],
    "next_cursor": "h2",
}
LINEAGE = {
    "revision": REVISION,
    "served_from": "tree",
    "branch": "main",
    "kind": "Supplier",
    "key": "S-1001",
    "created": CHANGE,
    "changes": 2,
    "attributes": [
        {
            "name": "tier",
            "value": "1",
            "provenance": {**PROVENANCE, "origin_type": "extraction"},
            "revision": REVISION,
            "author": "usr_loader",
            "extraction": {
                "job_id": "job_1",
                "document_id": "doc_1",
                "span_start": 10,
                "span_end": 24,
                "model_id": "m-1",
                "prompt_version": "v1",
            },
            "proposal": {
                "id": "prp_1",
                "title": "Raise risk",
                "status": "merged",
                "merged_revision": REVISION,
            },
        },
        {"name": "supplier_code", "value": "S-1001"},
    ],
    "sources": ["erp"],
    "proposals": [
        {"id": "prp_1", "title": "Raise risk", "status": "merged", "merged_revision": REVISION}
    ],
}


def branch_entry(name: str, head: str, *, default: bool, protected: bool) -> dict[str, Any]:
    return {
        "name": name,
        "head": head,
        "default": default,
        "protected": protected,
        "materialization": "materialized",
        "version": 1,
        "created_at": "2026-10-01T08:00:00Z",
        "created_by": "usr_loader",
    }


def revision_log(request: httpx.Request) -> httpx.Response:
    entries = [
        {
            "id": rev,
            "parents": [],
            "root": rev,
            "generation": 1,
            "created_at": "2026-10-02T00:00:00Z",
            "author": "usr_editor",
            "origin": {"type": "operation"},
            "message": f"change {rev[:2]}",
            "stats": {"records": 1},
        }
        for rev in (REVISION, PARENT, OLDER)
    ]
    if request.url.params.get("cursor") == "page2":
        return httpx.Response(200, json={"branch": "main", "head": REVISION, "items": entries[2:]})
    return httpx.Response(
        200,
        json={"branch": "main", "head": REVISION, "items": entries[:2], "next_cursor": "page2"},
    )


EXECUTION = {
    "status": "dry_run",
    "operation": "update_supplier",
    "branch": "main",
    "invocation_id": "inv_1",
    "parameters_hash": "h",
    "head": REVISION,
    "revision": None,
    "revisions": [],
    "edits": [{"op": "upsert", "kind": "Supplier", "key": "S-1"}],
    "edit_count": 1,
    "edits_truncated": False,
    "violations": [],
    "violation_count": 0,
    "warnings": [],
    "review_required": False,
    "replayed": False,
}
