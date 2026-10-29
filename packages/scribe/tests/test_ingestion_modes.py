"""The tier cap of the `direct` ingestion mode and its installation bounds (design 9.2)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.pipeline import ExtractionReport, ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.models.enums import ItemDecision
from lorebase_store.repositories import ItemRow


def item(tier: int | None) -> ItemRow:
    return ItemRow(
        item_id=f"pit_{tier}",
        item_type="link",
        seq=0,
        subject={},
        partition="r:rel",
        element_key="a\x1fb",
        decision=ItemDecision.ACCEPTED,
        tier=tier,
    )


def report() -> ExtractionReport:
    return ExtractionReport(
        job_id="exj_1",
        domain="supply",
        documents=["doc_1"],
        branch="extract/exj_1",
        target="main",
        tiers=[0, 1],
        proposal_id="prp_1",
        mode="direct",
    )


def run() -> ExtractionRun:
    unused: Any = None  # the cap and the pending check come before anything is read
    return ExtractionRun(unused, unused, ExtractionSettings(), InferenceSettings(), principal="svc")


@pytest.mark.parametrize("tiers", [[0, 1], [2], [0, None]])
async def test_a_model_item_caps_direct_at_doubts_only(tiers: list[int | None]) -> None:
    found = report()
    await run()._direct(object(), [item(t) for t in tiers], found)
    assert (found.mode, found.mode_capped, found.direct_blocked, found.direct_merged) == (
        "doubts_only",
        "tier",
        "tier",
        False,
    )
    assert found.as_dict()["mode_capped"] == "tier"


async def test_pending_items_block_direct_before_any_merge() -> None:
    found = report()
    found.pending = 1
    await run()._direct(object(), [item(0)], found)
    assert (found.mode, found.mode_capped, found.direct_blocked) == (
        "direct",
        None,
        "pending_items",
    )


def test_direct_is_never_the_installation_mode() -> None:
    with pytest.raises(ValidationError, match="direct is refused"):
        ExtractionSettings(mode="direct")
    bounds = ExtractionSettings(mode="review_all", domain_may_loosen=()).installation()
    assert bounds.base.mode == "review_all"
    assert bounds.domain_may_loosen == frozenset()
    defaults = ExtractionSettings()
    assert (defaults.mode, defaults.domain_may_loosen, defaults.direct_merge_protected) == (
        "doubts_only",
        ("mode",),
        True,
    )
