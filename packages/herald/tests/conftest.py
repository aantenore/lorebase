from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import pytest
from mcp import Client

from lorebase_herald.keeper_http import KeeperHttp
from lorebase_herald.server import create_server
from lorebase_herald.settings import ENV_ALIASES, HeraldSettings
from lorebase_platform import secrets, settings

from .fake_keeper import FakeKeeper

AGENT_KEY = "lbk_agentprefix_agentsecret"

Connect = Callable[..., AbstractAsyncContextManager[Client]]


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient configuration."""
    for name in list(os.environ):
        if (
            name.startswith(settings.ENV_PREFIX)
            or name == settings.CONFIG_ENV_VAR
            or name in ENV_ALIASES
        ):
            monkeypatch.delenv(name)
    monkeypatch.setattr(secrets, "_known_values", set())


@pytest.fixture
def fake() -> FakeKeeper:
    return FakeKeeper()


def herald_settings(**overrides: Any) -> HeraldSettings:
    values: dict[str, Any] = {
        "domain": "supply",
        "keeper": {"base_url": "http://keeper.test", "api_key": AGENT_KEY},
    }
    values.update(overrides)
    return HeraldSettings.model_validate(values)


@pytest.fixture
def connect(fake: FakeKeeper) -> Connect:
    """An MCP client session on a Herald server backed by the fake Keeper."""

    @asynccontextmanager
    async def open_client(**overrides: Any) -> AsyncIterator[Client]:
        configured = herald_settings(**overrides)
        keeper = KeeperHttp(configured.keeper, transport=fake.transport())
        try:
            async with Client(create_server(configured, keeper)) as client:
                yield client
        finally:
            await keeper.aclose()

    return open_client
