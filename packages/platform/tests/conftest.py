from __future__ import annotations

import os

import pytest

from lorebase_platform import secrets, settings


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient configuration and from other tests' registrations."""
    for name in list(os.environ):
        if name.startswith(settings.ENV_PREFIX) or name == settings.CONFIG_ENV_VAR:
            monkeypatch.delenv(name)
    monkeypatch.setattr(settings, "_SECTIONS", dict(settings._SECTIONS))
    monkeypatch.setattr(secrets, "_known_values", set())
