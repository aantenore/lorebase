"""Shared fixtures of the Scribe tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lorebase_core.model import Schema
from lorebase_core.model.yaml_schema import load_schema
from lorebase_platform import secrets, settings
from lorebase_store.settings import ENV_ALIASES

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "supply"


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without ambient LOREBASE__*, LOREBASE_CONFIG or alias variables."""
    for name in list(os.environ):
        if (
            name.startswith(settings.ENV_PREFIX)
            or name == settings.CONFIG_ENV_VAR
            or name in ENV_ALIASES
        ):
            monkeypatch.delenv(name)
    monkeypatch.setattr(secrets, "_known_values", set())


@pytest.fixture(scope="session")
def supply_schema() -> Schema:
    """The `supply` example schema (examples/supply/schema.yaml)."""
    return load_schema((EXAMPLE / "schema.yaml").read_text())
