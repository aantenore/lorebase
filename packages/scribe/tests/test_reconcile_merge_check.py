"""The operator's `reconciliation` merge check reaches the merges Scribe runs (design 10.3,
10.6): Source syncs hand `scribe.reconciliation.merge_check` to the merge service, whose Domain
setting `reconciliation_check` still wins."""

from __future__ import annotations

from typing import Any, ClassVar, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.merge import MergePolicy
from lorebase_core.model.source import MergePolicy as SourcePolicy
from lorebase_core.reconcile import ReconcileOptions
from lorebase_scribe.settings import SourceSettings
from lorebase_scribe.sources import sync
from lorebase_store.merging import MergeRequest, reconciliation_check_mode
from lorebase_store.repositories import ProposalRow
from lorebase_store.runtime import StoreRuntime


class FakeMergeService:
    """Records the request instead of merging."""

    requests: ClassVar[list[MergeRequest]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def recompute(self, request: MergeRequest) -> Any:
        FakeMergeService.requests.append(request)
        return None

    merge = recompute


def test_merge_check_is_an_operator_option() -> None:
    assert ReconcileOptions().merge_check == "warn"
    assert ReconcileOptions(merge_check="block").merge_check == "block"
    with pytest.raises(ValidationError):
        ReconcileOptions(merge_check="strict")


def test_the_domain_setting_wins_over_the_installation_mode() -> None:
    assert reconciliation_check_mode(None, "block") == "block"
    assert reconciliation_check_mode({"reconciliation_check": "off"}, "block") == "off"
    assert reconciliation_check_mode({"reconciliation_check": "nonsense"}, "block") == "block"
    assert reconciliation_check_mode({}, None) == "warn"


@pytest.mark.parametrize(("options", "expected"), [(None, None), ("block", "block")])
async def test_source_sync_merges_with_the_operator_mode(
    monkeypatch: pytest.MonkeyPatch, options: str | None, expected: str | None
) -> None:
    monkeypatch.setattr(sync, "MergeService", FakeMergeService)
    FakeMergeService.requests = []
    runner = sync.SourceSync(
        cast(async_sessionmaker[AsyncSession], None),
        cast(StoreRuntime, None),
        SourceSettings(),
        principal="svc_scribe",
        reconciliation=None if options is None else ReconcileOptions(merge_check=options),
    )
    report = sync.SyncReport(
        run_id="run_1",
        domain="supply",
        source="src_1",
        source_name="erp",
        branch="source/erp",
        target="main",
        dry_run=False,
    )
    proposal = cast(ProposalRow, object())
    await runner._merge(
        cast(AsyncSession, None),
        "supply",
        proposal,
        MergePolicy(),
        SourcePolicy.PROPOSAL,
        report,
    )
    [request] = FakeMergeService.requests
    assert request.reconciliation_check == expected
