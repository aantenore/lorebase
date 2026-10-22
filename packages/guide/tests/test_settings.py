"""Browser origins: an empty `allowed_origins` falls back to `default_origins`."""

from __future__ import annotations

import logging

import pytest

from lorebase_guide.app import create_app
from lorebase_guide.auth import AuthRefusedError, check_origin
from lorebase_guide.settings import GuideSettings, HttpSettings

from .support import inference_settings

WEB = ["http://127.0.0.1:8090", "http://localhost:8090/"]


def test_an_empty_list_falls_back_to_the_default_origins() -> None:
    http = HttpSettings.model_validate({"allowed_origins": [], "default_origins": WEB})
    assert http.origins == ("http://127.0.0.1:8090", "http://localhost:8090")
    assert http.origins_fell_back
    check_origin({"origin": "http://localhost:8090"}, http)
    with pytest.raises(AuthRefusedError):
        check_origin({"origin": "https://elsewhere.example"}, http)


def test_listed_origins_replace_the_defaults() -> None:
    http = HttpSettings.model_validate(
        {"allowed_origins": ["http://localhost:5173"], "default_origins": WEB}
    )
    assert http.origins == ("http://localhost:5173",)
    assert not http.origins_fell_back


def test_unset_origins_use_the_defaults_without_a_warning() -> None:
    http = HttpSettings.model_validate({"default_origins": WEB})
    assert http.origins == ("http://127.0.0.1:8090", "http://localhost:8090")
    assert not http.origins_fell_back
    assert HttpSettings().origins == ()


def test_the_fallback_is_logged_at_start(caplog: pytest.LogCaptureFixture) -> None:
    settings = GuideSettings.model_validate(
        {"http": {"allowed_origins": [], "default_origins": WEB}}
    )
    with caplog.at_level(logging.WARNING, logger="lorebase.guide"):
        create_app(settings, inference_settings())
    assert any("allowed_origins is an empty list" in r.getMessage() for r in caplog.records)
