"""What a trace step keeps of requests and responses: redaction, counts, refs, Proposals."""

from __future__ import annotations

import json

from lorebase_keeper.tracing import capture
from lorebase_keeper.tracing.config import TraceSettings
from lorebase_keeper.tracing.explain import summarize

SETTINGS = TraceSettings(enabled=True)


def body(document: object) -> bytes:
    return json.dumps(document).encode()


def test_filter_values_vectors_and_cursors_are_not_kept() -> None:
    query = capture.query_of(
        SETTINGS,
        params=[],
        body=body(
            {
                "kind": "Customer",
                "where": {
                    "and": [
                        {"attr": "email", "op": "eq", "value": "ann@example.com"},
                        {"attr": "country", "op": "in", "value": ["PL", "DE"]},
                        {"count": "orders", "op": "gte", "value": 2},
                        {"match": "late shipment"},
                    ]
                },
                "query_vector": [0.1, 0.2, 0.3],
                "cursor": "opaque",
            }
        ),
        body_complete=True,
    )
    assert query is not None
    sent = query["body"]
    terms = sent["where"]["and"]
    assert terms[0]["value"] == {"redacted": "string"}
    assert terms[1]["value"] == {"redacted": "list", "count": 2}
    assert terms[2]["value"] == 2  # a count of Links, not a value
    assert terms[3]["match"] == "late shipment"
    assert sent["query_vector"] == {"dims": 3}
    assert "cursor" not in sent
    assert "ann@example.com" not in json.dumps(query)


def test_literals_and_search_text_follow_the_settings() -> None:
    keep = TraceSettings(capture_literals=True, capture_search_text=False)
    query = capture.query_of(
        keep,
        params=[("q", "secret words"), ("where", '{"attr":"a","op":"eq","value":1}')],
        body=None,
        body_complete=True,
    )
    assert query == {
        "params": {
            "q": {"redacted": "text", "length": 12},
            "where": {"attr": "a", "op": "eq", "value": 1},
        }
    }


def test_truncated_and_oversized_queries() -> None:
    assert capture.query_of(SETTINGS, params=[], body=b"{}", body_complete=False) == {
        "body": {"truncated": True, "bytes": 2}
    }
    small = TraceSettings(max_query_bytes=256, max_text_chars=10_000)
    big = capture.query_of(small, params=[], body=body({"q": "x" * 1000}), body_complete=True)
    assert big == {"truncated": True}
    short = TraceSettings(max_text_chars=5)
    cut = capture.query_of(
        short, params=[("branch", "feature/long-name")], body=None, body_complete=True
    )
    assert cut == {"params": {"branch": "featu…"}}
    assert capture.branch_of({"body": {"branch": "main"}}) == "main"


def test_results_keep_counts_and_shape_only() -> None:
    result = capture.result_of(
        {
            "revision": "ab",
            "mode": "hybrid",
            "items": [{"kind": "Part", "key": "P-1", "title": "Bearing", "score": 0.4}],
            "corrections": [{"word": "aloys", "replacements": ["alloys"]}],
            "links": {"supplied_by": [{"from": "Part/P-1", "to": "Supplier/S-1"}]},
            "budget": {
                "limit_tokens": 100,
                "used_tokens": 90,
                "over_budget": False,
                "sections": {},
            },
            "next_cursor": None,
        },
        SETTINGS,
    )
    assert result == {
        "counts": {"items": 1, "links": 1},
        "mode": "hybrid",
        "corrections": [{"word": "aloys", "replacements": ["alloys"]}],
        "budget": {"limit_tokens": 100, "used_tokens": 90, "over_budget": False},
        "has_more": False,
    }
    assert capture.result_of([1, 2], SETTINGS) is None
    hidden = capture.result_of(
        {"corrections": [{"word": "x", "replacements": ["y"]}]},
        TraceSettings(capture_search_text=False),
    )
    assert hidden == {"counts": {}}


def test_refs_come_from_records_edges_and_packs_never_from_values() -> None:
    refs = capture.refs_of(
        {
            "start": {
                "kind": "Part",
                "key": "P-1",
                "values": {"owner": {"kind": "User", "key": "u"}},
            },
            "hops": [
                {
                    "relation": "supplied_by",
                    "records": [{"kind": "Supplier", "key": "S-1", "values": {}}],
                    "edges": [
                        {
                            "relation": "supplied_by",
                            "from": {"kind": "Part", "key": "P-1"},
                            "to": {"kind": "Supplier", "key": "S-1"},
                            "values": {"since": "2020"},
                        }
                    ],
                }
            ],
            "seeds": [{"ref": "Plant/W-1", "values": {}}],
            "links": {"located_in": [{"from": "Supplier/S-1", "to": "Plant/W-1"}]},
        },
        limit=50,
    )
    assert refs == {
        "records": [["Part", "P-1"], ["Supplier", "S-1"], ["Plant", "W-1"]],
        "links": [
            ["supplied_by", "Part", "P-1", "Supplier", "S-1"],
            ["located_in", "Supplier", "S-1", "Plant", "W-1"],
        ],
    }
    capped = capture.refs_of({"items": [{"kind": "K", "key": str(i)} for i in range(5)]}, limit=2)
    assert capped == {"records": [["K", "0"], ["K", "1"]], "links": [], "truncated": True}
    assert capture.refs_of({"items": []}, limit=5) is None


def test_proposals_named_by_write_responses() -> None:
    assert capture.proposal_of({"proposal": {"id": "prp_1"}}) == "prp_1"
    assert capture.proposal_of({"proposal_id": "prp_2"}) == "prp_2"
    assert capture.proposal_of({"id": "x"}) is None


def test_plan_summary() -> None:
    plan = [
        {
            "Plan": {
                "Node Type": "Limit",
                "Plan Rows": 5,
                "Total Cost": 12.5,
                "Plans": [
                    {
                        "Node Type": "Index Scan",
                        "Relation Name": "mat_records",
                        "Index Name": "mat_records_attr_x",
                    },
                    {"Node Type": "Seq Scan", "Relation Name": "mat_links"},
                ],
            }
        }
    ]
    summary = summarize("query", json.dumps(plan))
    assert summary == {
        "label": "query",
        "node": "Limit",
        "rows": 5,
        "cost": 12.5,
        "node_types": ["Limit", "Index Scan", "Seq Scan"],
        "indexes": ["mat_records_attr_x"],
        "scans": [
            {"node": "Index Scan", "relation": "mat_records", "index": "mat_records_attr_x"},
            {"node": "Seq Scan", "relation": "mat_links"},
        ],
    }
