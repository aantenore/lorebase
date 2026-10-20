"""Rendering of context packs and explanations: markdown snapshots and the JSON shape.

Snapshots live next to this file in `snapshots/`; set LOREBASE_UPDATE_SNAPSHOTS=1 to rewrite
them after an intended change, then review the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from lorebase_core.model import Schema
from lorebase_keeper.agents.budget import Draft, fit
from lorebase_keeper.agents.explain import ExplainOut, explain_markdown
from lorebase_keeper.agents.markdown import markdown_measure, markdown_sections, pack_markdown
from lorebase_keeper.agents.pack import (
    ContextPackOut,
    budget_json,
    dumps,
    json_measure,
    json_sections,
)

SNAPSHOTS = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("LOREBASE_UPDATE_SNAPSHOTS") == "1"
REVISION = "c" * 64
CPT = 4.0


def check(name: str, rendered: str) -> None:
    path = SNAPSHOTS / name
    if UPDATE or not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(rendered)
        if not UPDATE:
            pytest.fail(f"snapshot {path.name} was missing and has been written; rerun")
    assert rendered == path.read_text()


def markdown(draft: Draft, schema: Schema, budget_tokens: int) -> str:
    result = fit(
        draft,
        budget_tokens=budget_tokens,
        chars_per_token=CPT,
        measure=markdown_measure(schema),
    )
    budget = budget_json(
        result, draft, budget_tokens=budget_tokens, chars_per_token=CPT, capped=False
    )
    return pack_markdown(
        markdown_sections(result.draft, schema),
        domain="supply",
        branch="main",
        revision=REVISION,
        budget=budget,
        since="a" * 64,
    )


def test_full_pack_markdown(draft: Draft, schema: Schema) -> None:
    rendered = markdown(draft, schema, 4000)
    check("context_pack.md", rendered)
    assert rendered == markdown(draft, schema, 4000)


def test_trimmed_pack_markdown(draft: Draft, schema: Schema) -> None:
    check("context_pack_trimmed.md", markdown(draft, schema, 700))


def test_markdown_escapes_table_cells(draft: Draft, schema: Schema) -> None:
    tricky = Draft(
        seeds=draft.seeds,
        neighbors=(
            draft.neighbors[0].__class__(
                ref="Part/P-1", kind="Part", title="a | b\nc", depth=1, values={"x": "1|2"}
            ),
        ),
    )
    table = markdown_sections(tricky, schema)["neighbors"]
    assert "| Part/P-1 | a \\| b c | 1 | x=1\\|2 |" in table


def test_json_pack_matches_the_documented_shape(draft: Draft, schema: Schema) -> None:
    result = fit(draft, budget_tokens=4000, chars_per_token=CPT, measure=json_measure(schema))
    document = {
        "domain": "supply",
        "branch": "main",
        "revision": REVISION,
        "etag": f'"{REVISION}.0123456789abcdef"',
        **json_sections(result.draft, schema),
        "budget": budget_json(result, draft, budget_tokens=4000, chars_per_token=CPT, capped=False),
    }
    parsed = ContextPackOut.model_validate(json.loads(dumps(document)))
    assert [s.ref for s in parsed.seeds] == ["Supplier/S-1001"]
    assert parsed.schema_ is not None
    assert [k.name for k in parsed.schema_.kinds] == ["Contract", "Part", "Plant", "Supplier"]
    assert [r.name for r in parsed.schema_.relations] == [
        "stocked_at",
        "supplied_by",
        "with_supplier",
    ]
    risk = next(a for a in parsed.schema_.kinds[3].attributes if a.name == "risk_score")
    assert risk.type == "integer"
    tier = next(a for a in parsed.schema_.kinds[3].attributes if a.name == "tier")
    assert tier.type == "enum(1|2|3)"
    assert list(parsed.links) == ["stocked_at", "supplied_by", "with_supplier"]


def test_explain_markdown_with_proposals_and_truncated_links() -> None:
    origin: dict[str, Any] = {
        "revision": "d" * 64,
        "at": "2026-10-02T10:00:00Z",
        "actor": "usr_reviewer",
        "origin_type": "merge_resolution",
        "origin": {"type": "merge_resolution", "proposal_id": "prp_1"},
        "message": "Merge prp_1",
    }
    document: dict[str, Any] = {
        "domain": "supply",
        "branch": "main",
        "revision": REVISION,
        "record": {"ref": "Part/P-10001", "kind": "Part", "key": "P-10001", "values": {}},
        "created": None,
        "attributes": [{"name": "unit_cost", "value": "3.20", **origin}],
        "links": [
            {"relation": "supplied_by", "direction": "forward", "ref": "Supplier/S-1005", **origin}
        ],
        "links_truncated": True,
        "history": {"changes": 1},
        "proposals": [
            {"id": "prp_1", "title": "Fix | costs", "status": "merged", "merged_revision": "d" * 64}
        ],
    }
    ExplainOut.model_validate(document)
    check("explain_part.md", explain_markdown(document))
