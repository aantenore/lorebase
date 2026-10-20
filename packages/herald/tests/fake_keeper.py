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


def record(kind: str, key: str, title: str, **values: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "key": key,
        "title": title,
        "values": values,
        "updated_revision": REVISION,
        "updated_at": "2026-10-01T08:00:03.000000Z",
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
