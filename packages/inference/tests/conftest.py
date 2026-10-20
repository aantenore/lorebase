from __future__ import annotations

import os

import pytest

from lorebase_inference.settings import ENV_ALIASES
from lorebase_platform import settings as platform_settings


@pytest.fixture(autouse=True)
def clean_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without ambient LOREBASE__*, LOREBASE_CONFIG or LLM_* variables."""
    for name in list(os.environ):
        if (
            name.startswith(platform_settings.ENV_PREFIX)
            or name == platform_settings.CONFIG_ENV_VAR
            or name in ENV_ALIASES
        ):
            monkeypatch.delenv(name)
