"""Traits as targets and lenses (design 8.8): resolved against the caller's grants."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from lorebase_core.model import Lens, Schema
from lorebase_core.model.ids import IdPrefix, new_id
from lorebase_keeper.agents.context import PackSettings, _follow, effective
from lorebase_keeper.agents.pack import ContextRequest
from lorebase_keeper.agents.settings import AgentSettings
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.errors import ProblemError
from lorebase_keeper.reading.lenses import resolve_lens, trait_kinds
from lorebase_keeper.reading.points import KindScope, ReadPoint, ServedFrom
from lorebase_keeper.reading.requests import SearchRequest
from lorebase_keeper.reading.search import _target
from lorebase_keeper.services.reads import ReadGuard
from lorebase_store.layout import BranchLayout


def _ids(schema: Schema, section: str, *names: str) -> tuple[str, ...]:
    found = {e.api_name: e.id for e in getattr(schema, section).values()}
    return tuple(found[n] for n in names)


@pytest.fixture
def lensed(schema: Schema) -> Schema:
    def lens(name: str, **members: Any) -> Lens:
        return Lens(id=new_id(IdPrefix.LENS), api_name=name, **members)

    lenses = [
        lens(
            "supplier_risk",
            kinds=_ids(schema, "kinds", "Supplier", "Part"),
            relations=_ids(schema, "relations", "supplied_by"),
            depth=1,
            budget_tokens=900,
            include_lineage=False,
        ),
        lens("located", traits=_ids(schema, "traits", "Located")),
        lens("plants", kinds=_ids(schema, "kinds", "Plant")),
        lens("too_deep", depth=9, budget_tokens=10**9),
        lens("open"),
    ]
    return schema.model_copy(update={"lenses": {item.id: item for item in lenses}})


def point_for(schema: Schema, *kinds: str, invisible: bool = False) -> ReadPoint:
    guard = ReadGuard(
        authorizer=cast(Any, None),
        principal=cast(Any, None),
        permissions=(Permission.DATA_READ,),
        domain_id="acme",
        max_depth=10,
    )
    scope = KindScope(guard, frozenset(kinds) if kinds else None, invisible=invisible)
    return ReadPoint(
        domain=cast(Any, None),
        branch="main",
        revision=cast(Any, None),
        schema=scope.visible(schema).schema,
        served_from=ServedFrom.MATERIALIZED,
        scope=scope,
        layout=BranchLayout("acme", "main"),
    )


def runtime(depth: int = 2) -> Any:
    traverse = SimpleNamespace(max_depth=depth)
    return SimpleNamespace(
        read=SimpleNamespace(traverse=traverse),
        store_settings=SimpleNamespace(traverse=traverse),
    )


def names(kinds: Any) -> list[str]:
    return sorted(k.api_name for k in kinds)


def codes(raised: pytest.ExceptionInfo[ProblemError]) -> list[str]:
    return [e["code"] for e in raised.value.extensions["errors"]]


# Traits as targets ----------------------------------------------------------------------------


def test_a_trait_stands_for_the_kinds_the_caller_may_read(lensed: Schema) -> None:
    assert names(trait_kinds(point_for(lensed), ["Located"], "/body/traits")) == [
        "Plant",
        "Supplier",
    ]
    narrowed = point_for(lensed, "Supplier", "Part")
    assert names(trait_kinds(narrowed, ["Located"], "/body/traits")) == ["Supplier"]


def test_a_trait_the_caller_cannot_see_is_unknown_like_any_other_name(lensed: Schema) -> None:
    with pytest.raises(ProblemError) as missing:
        trait_kinds(point_for(lensed, "Part", invisible=True), ["Located"], "/body/traits")
    with pytest.raises(ProblemError) as nonsense:
        trait_kinds(point_for(lensed, "Part", invisible=True), ["Nope"], "/body/traits")
    assert missing.value.extensions == {
        "errors": [
            {
                "pointer": "/body/traits/0",
                "message": "unknown Trait 'Located'",
                "code": "unknown_trait",
            }
        ]
    }
    assert codes(missing) == codes(nonsense) == ["unknown_trait"]


def test_search_targets_the_union_of_kinds_and_traits(lensed: Schema) -> None:
    point = point_for(lensed)
    target = _target(point, SearchRequest(q="x", kinds=["Part"], traits=["Located"]))
    assert names(target.kinds) == ["Part", "Plant", "Supplier"]
    only = _target(point, SearchRequest(q="x", traits=["Located"]))
    assert names(only.kinds) == ["Plant", "Supplier"]
    single = _target(point, SearchRequest(q="x", kinds=["Part"]))
    assert single.name == "Part"


def test_search_traits_stop_at_the_grants(lensed: Schema) -> None:
    target = _target(point_for(lensed, "Plant", "Part"), SearchRequest(q="x", traits=["Located"]))
    assert names(target.kinds) == ["Plant"]


# Lenses ---------------------------------------------------------------------------------------


def test_a_lens_limits_search_unless_the_request_names_kinds(lensed: Schema) -> None:
    point = point_for(lensed)
    assert names(_target(point, SearchRequest(q="x", lens="supplier_risk")).kinds) == [
        "Part",
        "Supplier",
    ]
    assert names(_target(point, SearchRequest(q="x", lens="located")).kinds) == [
        "Plant",
        "Supplier",
    ]
    by_request = _target(point, SearchRequest(q="x", lens="supplier_risk", kinds=["Plant"]))
    assert names(by_request.kinds) == ["Plant"]
    by_trait = _target(point, SearchRequest(q="x", lens="supplier_risk", traits=["Located"]))
    assert names(by_trait.kinds) == ["Plant", "Supplier"]
    everything = _target(point, SearchRequest(q="x", lens="open"))
    assert len(everything.kinds) == len(lensed.kinds)


def test_a_lens_never_widens_the_grants(lensed: Schema) -> None:
    point = point_for(lensed, "Supplier")
    assert names(_target(point, SearchRequest(q="x", lens="supplier_risk")).kinds) == ["Supplier"]
    assert names(_target(point, SearchRequest(q="x", lens="located")).kinds) == ["Supplier"]
    with pytest.raises(ProblemError) as raised:
        _target(point, SearchRequest(q="x", lens="plants"))
    assert codes(raised) == ["lens_unknown"]


def test_a_lens_is_unknown_when_the_schema_has_none_or_the_projection_dropped_it(
    lensed: Schema,
) -> None:
    with pytest.raises(ProblemError) as raised:
        resolve_lens(point_for(lensed), "nothing", "/body/lens")
    assert codes(raised) == ["lens_unknown"]
    assert raised.value.extensions["errors"][0]["pointer"] == "/body/lens"
    invisible = point_for(lensed, "Part", invisible=True)
    with pytest.raises(ProblemError) as dropped:
        resolve_lens(invisible, "plants", "/body/lens")
    assert codes(dropped) == ["lens_unknown"]
    seen = resolve_lens(invisible, "supplier_risk", "/body/lens")
    assert names(seen.kinds or ()) == ["Part"]
    assert resolve_lens(invisible, "open", "/body/lens").kinds is None


# Context packs --------------------------------------------------------------------------------


def test_request_members_win_over_the_lens_which_wins_over_the_settings(lensed: Schema) -> None:
    point, settings = point_for(lensed), AgentSettings().context
    lens = effective(ContextRequest(search="x", lens="supplier_risk"), settings, runtime(), point)
    assert (lens.budget_tokens, lens.depth) == (900, 1)
    assert (lens.include_schema, lens.include_recent_changes, lens.include_lineage) == (
        True,
        True,
        False,
    )
    assert not lens.capped
    assert lens.relations == _ids(lensed, "relations", "supplied_by")
    assert names(lensed.kinds[k] for k in lens.kinds or ()) == ["Part", "Supplier"]

    asked = ContextRequest(
        search="x",
        lens="supplier_risk",
        budget_tokens=500,
        depth=2,
        include_lineage=True,
        traits=["Located"],
    )
    own = effective(asked, settings, runtime(), point)
    assert (own.budget_tokens, own.depth, own.include_lineage) == (500, 2, True)
    assert names(lensed.kinds[k] for k in own.kinds or ()) == ["Plant", "Supplier"]

    plain = effective(ContextRequest(search="x"), settings, runtime(), point)
    assert plain == PackSettings(settings.default_budget_tokens, settings.default_depth)


def test_a_lens_over_the_caps_is_served_at_the_caps(lensed: Schema) -> None:
    settings = AgentSettings().context
    capped = effective(
        ContextRequest(search="x", lens="too_deep"), settings, runtime(2), point_for(lensed)
    )
    assert capped.capped
    assert (capped.budget_tokens, capped.depth) == (settings.max_budget_tokens, 2)
    with pytest.raises(ProblemError) as raised:
        effective(
            ContextRequest(search="x", lens="too_deep", depth=9),
            settings,
            runtime(2),
            point_for(lensed),
        )
    assert codes(raised) == ["limit_exceeded"]


def test_an_unknown_lens_is_refused_before_anything_is_read(lensed: Schema) -> None:
    with pytest.raises(ProblemError) as raised:
        effective(
            ContextRequest(search="x", lens="nothing"),
            AgentSettings().context,
            runtime(),
            point_for(lensed),
        )
    assert codes(raised) == ["lens_unknown"]


def test_neighbors_stop_at_the_kinds_of_the_lens(lensed: Schema) -> None:
    point = point_for(lensed)
    everything = _follow(point, None)
    assert set(everything) == set(lensed.relations)
    settings = AgentSettings().context
    bounds = effective(ContextRequest(search="x", lens="supplier_risk"), settings, runtime(), point)
    followed = _follow(point, None, bounds)
    supplied_by = _ids(lensed, "relations", "supplied_by")[0]
    assert set(followed) == {supplied_by}
    assert followed[supplied_by].name == "BOTH"
    plants = effective(ContextRequest(search="x", lens="plants"), settings, runtime(), point)
    reached = _follow(point, None, plants)
    stocked_at = _ids(lensed, "relations", "stocked_at")[0]
    assert reached[stocked_at].name == "OUT"  # Part -> Plant: Plants are reached from Parts
    assert supplied_by not in reached
    named = _follow(point, ["supplied_by"], plants)
    assert named == {}
