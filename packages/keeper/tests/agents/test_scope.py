"""Agent reads keep to the Kinds a Kind-narrowed caller may read, before anything is fetched."""

from __future__ import annotations

from typing import Any, cast

import pytest

from lorebase_core.model import Schema
from lorebase_keeper.agents.context import seed_addresses
from lorebase_keeper.agents.pack import ContextRequest
from lorebase_keeper.agents.settings import AgentSettings
from lorebase_keeper.auth.permissions import Permission
from lorebase_keeper.errors import ProblemError
from lorebase_keeper.reading.points import KindScope, ReadPoint, ServedFrom
from lorebase_keeper.reading.runtime import ReadRuntime
from lorebase_keeper.routes import agents
from lorebase_keeper.services.reads import ReadGuard
from lorebase_store.layout import BranchLayout


class Untouched:
    """A session or runtime that fails the test when used."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"{name} used before the Kind scope was checked")


def narrowed(schema: Schema, *kinds: str) -> ReadPoint:
    guard = ReadGuard(
        authorizer=cast(Any, Untouched()),
        principal=cast(Any, Untouched()),
        permissions=(Permission.DATA_READ,),
        domain_id="acme",
        max_depth=10,
    )
    return ReadPoint(
        domain=cast(Any, Untouched()),
        branch="main",
        revision=cast(Any, Untouched()),
        schema=schema,
        served_from=ServedFrom.MATERIALIZED,
        scope=KindScope(guard, frozenset(kinds)),
        layout=BranchLayout("acme", "main"),
    )


def forbidden(raised: pytest.ExceptionInfo[ProblemError], kind: str) -> None:
    assert raised.value.status_code == 403
    assert raised.value.extensions["kind"] == kind


async def test_seed_refs_of_another_kind_are_refused(schema: Schema) -> None:
    point = narrowed(schema, "Supplier")
    settings = AgentSettings().context
    runtime = cast(ReadRuntime, Untouched())
    request = ContextRequest(refs=["Supplier/S-1001", "Part/P-10001"])
    with pytest.raises(ProblemError) as raised:
        await seed_addresses(cast(Any, Untouched()), runtime, point, request, settings)
    forbidden(raised, "Part")

    own = ContextRequest(refs=["Supplier/S-1001"])
    found = await seed_addresses(cast(Any, Untouched()), runtime, point, own, settings)
    assert [(kind.api_name, key) for kind, key in found] == [("Supplier", "S-1001")]


async def test_explain_of_another_kind_is_refused(
    schema: Schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    point = narrowed(schema, "Supplier")

    async def resolve(*args: Any, **kwargs: Any) -> ReadPoint:
        return point

    monkeypatch.setattr(agents, "resolve_point", resolve)
    with pytest.raises(ProblemError) as raised:
        await agents.explain(
            "acme",
            "Part",
            "P-10001",
            cast(Any, Untouched()),
            cast(Any, Untouched()),
            cast(Any, Untouched()),
            AgentSettings(),
            cast(Any, Untouched()),
        )
    forbidden(raised, "Part")
