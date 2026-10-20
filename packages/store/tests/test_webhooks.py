"""Webhook secrets, signatures, target checks, backoff and settings (no database)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from lorebase_platform.settings import CONFIG_ENV_VAR, ConfigError, Settings, load_settings
from lorebase_store.webhooks import (
    WebhookSettings,
    new_secret,
    signature,
    signing_key,
    verify,
    webhook_settings,
)
from lorebase_store.webhooks.delivery import backoff, scheme_problem, target_problem

EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "lorebase.example.yaml"
NOW = 1_800_000_000


def test_secrets_are_random_and_stored_as_digest() -> None:
    first, second = new_secret(32), new_secret(32)
    assert first != second
    assert first.startswith("whsec_")
    assert len(signing_key(first)) == 32
    assert signing_key(first) == signing_key(first)


def test_signature_round_trip_and_tampering() -> None:
    secret = new_secret(32)
    body = b'{"type":"revision.committed"}'
    header = signature(signing_key(secret), NOW, body)
    assert header.startswith(f"t={NOW},v1=")
    assert verify(secret, header, body, now=NOW + 10, tolerance_s=300)
    assert not verify(secret, header, body + b" ", now=NOW, tolerance_s=300)
    assert not verify(new_secret(32), header, body, now=NOW, tolerance_s=300)
    assert not verify(secret, header, body, now=NOW + 301, tolerance_s=300)
    assert not verify(secret, "v1=abc", body, now=NOW, tolerance_s=300)


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://hooks.example.com/lorebase", True),
        ("http://hooks.example.com/lorebase", False),
        ("https://127.0.0.1/x", False),
        ("https://10.1.2.3/x", False),
        ("https://[::1]/x", False),
        ("https://169.254.169.254/latest", False),
        ("https://localhost/x", False),
        ("https:///x", False),
    ],
)
def test_target_form_checks(url: str, allowed: bool) -> None:
    assert (scheme_problem(url, WebhookSettings()) is None) is allowed


def test_private_targets_can_be_allowed() -> None:
    settings = WebhookSettings(allowed_schemes=("http", "https"), allow_private_targets=True)
    assert scheme_problem("http://127.0.0.1:8080/hook", settings) is None


async def test_names_resolving_to_private_addresses_are_refused() -> None:
    async def private(host: str) -> list[str]:
        return ["10.0.0.5"]

    async def public(host: str) -> list[str]:
        return ["93.184.215.14"]

    settings = WebhookSettings()
    url = "https://hooks.example.com/x"
    assert await target_problem(url, settings, private) == "resolves to a private address"
    assert await target_problem(url, settings, public) is None


def test_backoff_grows_and_is_capped() -> None:
    settings = WebhookSettings(backoff_base_s=10, backoff_factor=3, backoff_max_s=100)
    assert [backoff(n, settings) for n in (1, 2, 3, 4)] == [
        timedelta(seconds=10),
        timedelta(seconds=30),
        timedelta(seconds=90),
        timedelta(seconds=100),
    ]


def test_example_file_matches_code_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(EXAMPLE))
    assert webhook_settings(load_settings()) == WebhookSettings()


@pytest.mark.parametrize(
    ("values", "needle"),
    [
        ({"backoff_base_s": 10, "backoff_max_s": 5}, "webhooks"),
        ({"lease_s": 5, "timeout_s": 10}, "webhooks"),
        ({"allowed_schemes": []}, "webhooks.allowed_schemes"),
    ],
)
def test_invalid_values_name_the_key(values: dict[str, object], needle: str) -> None:
    with pytest.raises(ConfigError) as caught:
        webhook_settings(Settings(webhooks=values))
    assert needle in str(caught.value)
