from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from lorebase_keeper.app import create_app
from lorebase_platform import secrets, settings
from lorebase_platform.settings import Settings

AppFactory = Callable[..., FastAPI]
ClientFactory = Callable[[FastAPI], AbstractAsyncContextManager[httpx.AsyncClient]]


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient configuration."""
    for name in list(os.environ):
        if name.startswith(settings.ENV_PREFIX) or name == settings.CONFIG_ENV_VAR:
            monkeypatch.delenv(name)
    monkeypatch.setattr(secrets, "_known_values", set())


@pytest.fixture
def make_app() -> AppFactory:
    """Build an application from `keeper` and other settings sections given as dicts."""

    def build(keeper: dict[str, Any] | None = None, **kwargs: Any) -> FastAPI:
        sections: dict[str, Any] = {"keeper": keeper or {}}
        sections.update(kwargs.pop("sections", {}))
        return create_app(Settings(**sections), **kwargs)

    return build


@pytest.fixture
def app(make_app: AppFactory) -> FastAPI:
    return make_app()


@asynccontextmanager
async def open_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://keeper.test") as http:
        yield http


@pytest.fixture
def client_for() -> ClientFactory:
    """Open an HTTP client bound to the given application."""
    return open_client


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with open_client(app) as http:
        yield http
