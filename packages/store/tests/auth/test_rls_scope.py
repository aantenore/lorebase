from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lorebase_store.auth.scope import rls_scope
from lorebase_store.repositories import scope_to_domain


class RecordingSession:
    """Stands in for a session: records statements, runs none."""

    def __init__(self) -> None:
        self.executed: list[Any] = []

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.executed.append((statement, params))
        raise AssertionError("nothing may be sent for an invalid Domain id")


@pytest.mark.parametrize(
    ("domain_ids", "message"),
    [
        (["dom_a,dom_b"], "commas"),
        (["dom_a", "dom_b,dom_c"], "commas"),
        ([""], "empty"),
        (["dom_a", ""], "empty"),
    ],
)
async def test_rls_scope_refuses_ids_that_would_change_the_list(
    domain_ids: list[str], message: str
) -> None:
    session = RecordingSession()
    with pytest.raises(ValueError, match=message):
        async with rls_scope(cast(AsyncSession, session), domain_ids=domain_ids):
            pass
    assert session.executed == []


@pytest.mark.parametrize(("domain_id", "message"), [("dom_a,dom_b", "commas"), ("", "empty")])
async def test_scope_to_domain_applies_the_same_check(domain_id: str, message: str) -> None:
    session = RecordingSession()
    with pytest.raises(ValueError, match=message):
        await scope_to_domain(cast(AsyncSession, session), domain_id)
    assert session.executed == []
