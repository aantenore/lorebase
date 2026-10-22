from __future__ import annotations

import os

import pytest

from lorebase_platform import secrets, settings

from .support import FakeKeeper


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient configuration."""
    for name in list(os.environ):
        if name.startswith(settings.ENV_PREFIX) or name == settings.CONFIG_ENV_VAR:
            monkeypatch.delenv(name)
    monkeypatch.setattr(secrets, "_known_values", set())


@pytest.fixture
def keeper() -> FakeKeeper:
    return FakeKeeper()
