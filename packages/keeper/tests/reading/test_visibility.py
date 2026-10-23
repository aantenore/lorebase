"""Kind scopes with invisible Kinds (design 13.3): what a narrowed caller is told."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from lorebase_core.model import Schema
from lorebase_core.model.yaml_schema import load_schema
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.errors import ProblemError
from lorebase_keeper.reading.points import KindScope
from lorebase_keeper.reading.visibility import projection, visible
from lorebase_keeper.services.reads import ReadGuard

SCHEMA = Path(__file__).resolve().parents[4] / "examples" / "supply" / "schema.yaml"


@pytest.fixture(scope="module")
def supply() -> Schema:
    return load_schema(SCHEMA.read_text())


def scope(*kinds: str, invisible: bool) -> KindScope:
    guard = ReadGuard(
        authorizer=cast(Any, object()),
        principal=cast(Any, object()),
        permissions=(Permission.DATA_READ,),
        domain_id="acme",
        max_depth=10,
    )
    return KindScope(guard, frozenset(kinds), invisible=invisible)


def test_invisible_kinds_are_unknown_not_forbidden() -> None:
    hidden = scope("Part", invisible=True)
    hidden.precheck(["Supplier"])  # resolution reports it, like an unknown name
    with pytest.raises(ProblemError) as raised:
        hidden.require(["Supplier"])
    assert raised.value.status_code == 404
    assert raised.value.problem_detail == "Kind Supplier not found."
    assert "kind" not in raised.value.extensions


def test_without_invisibility_other_kinds_are_forbidden() -> None:
    refused = scope("Part", invisible=False)
    for check in (refused.precheck, refused.require):
        with pytest.raises(ProblemError) as raised:
            check(["Supplier"])
        assert raised.value.status_code == 403
        assert raised.value.extensions["kind"] == "Supplier"


def test_cached_responses_are_kept_apart_by_kinds_and_mode() -> None:
    variants = {
        scope("Part", invisible=True).variant,
        scope("Part", invisible=False).variant,
        scope("Part", "Plant", invisible=True).variant,
        KindScope(scope("Part", invisible=True).guard).variant,
    }
    assert len(variants) == 4


def test_the_schema_seen_is_projected_only_when_kinds_are_invisible(supply: Schema) -> None:
    assert scope("Part", invisible=False).visible(supply).schema is supply
    assert KindScope(scope("Part", invisible=True).guard).visible(supply).schema is supply
    seen = scope("Part", invisible=True).visible(supply)
    assert set(seen.schema.kinds) == {k.id for k in supply.kinds.values() if k.api_name == "Part"}
    supplier = supply.kind_by_api_name("Supplier")
    assert supplier is not None
    assert supplier.id in seen.hidden


def test_projections_are_remembered_per_schema_and_kinds(supply: Schema) -> None:
    kinds = frozenset({"Part", "Plant"})
    first = projection(supply, kinds)
    assert projection(supply, kinds) is first
    assert visible(supply, kinds) is first.schema
    copy = supply.model_copy()
    assert projection(copy, kinds) is not first
    assert projection(supply, frozenset({"Part"})).schema is not first.schema
