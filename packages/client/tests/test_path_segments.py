"""Path segments built from caller values cannot leave the route they belong to."""

from __future__ import annotations

import pytest

from lorebase_client import endpoints as ep


@pytest.mark.parametrize("key", ["", ".", ".."])
def test_dot_and_empty_segments_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match="invalid path segment"):
        ep.get_record("supply", "Supplier", key)


@pytest.mark.parametrize("domain", ["", ".."])
def test_domain_segment_is_checked_too(domain: str) -> None:
    with pytest.raises(ValueError, match="invalid path segment"):
        ep.get_domain(domain)


@pytest.mark.parametrize("branch", ["main/../other", "a//b", "./main", ""])
def test_branch_names_cannot_hold_dot_or_empty_parts(branch: str) -> None:
    with pytest.raises(ValueError, match="invalid branch name"):
        ep.get_branch("supply", branch)


def test_ordinary_values_are_percent_encoded() -> None:
    call = ep.get_record("supply", "Supplier", "a/../b?x")
    assert call.path == "/domains/supply/kinds/Supplier/records/a%2F..%2Fb%3Fx"
    assert ep.get_branch("supply", "team/main").path.endswith("/branches/team/main")
