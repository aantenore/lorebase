"""Webhook secrets, signatures, target checks, backoff and settings (no database)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

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
from lorebase_store.webhooks.delivery import (
    DEAD,
    DELIVERED,
    PENDING,
    Target,
    _attempt,
    _attempt_all,
    _Claimed,
    backoff,
    check_target,
    scheme_problem,
    target_problem,
)

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


async def test_every_resolved_address_is_checked() -> None:
    async def mixed(host: str) -> list[str]:
        return ["93.184.215.14", "127.0.0.1"]

    async def nothing(host: str) -> list[str]:
        return []

    url = "https://hooks.example.com/x"
    assert await target_problem(url, WebhookSettings(), mixed) == "resolves to a private address"
    assert (await check_target(url, WebhookSettings(), nothing)).problem is not None
    checked = await check_target(url, WebhookSettings(), _public)
    assert checked == Target(None, ("93.184.215.14",))


@pytest.mark.parametrize(
    "address",
    [
        "100.100.100.200",  # shared address space (100.64.0.0/10), a cloud metadata service
        "100.64.0.1",
        "192.0.0.8",  # IETF protocol assignments
        "198.18.0.1",  # benchmarking
        "::ffff:10.0.0.1",  # IPv4-mapped
        "64:ff9b::a00:5",  # NAT64 well-known prefix wrapping 10.0.0.5
        "64:ff9b:1::a00:5",  # NAT64 local-use prefix
        "2002:a00:1::",  # 6to4 wrapping 10.0.0.1
        "2001:0:4136:e378:8000:63bf:f5ff:fffe",  # Teredo, client 10.0.0.1
        "fec0::1",  # deprecated site-local
        "fc00::1",
        "224.0.1.1",  # multicast
    ],
)
async def test_non_global_addresses_are_refused(address: str) -> None:
    async def resolve(host: str) -> list[str]:
        return [address]

    url = "https://hooks.example.com/x"
    assert await target_problem(url, WebhookSettings(), resolve) == "resolves to a private address"


async def test_global_ipv6_addresses_are_accepted() -> None:
    async def resolve(host: str) -> list[str]:
        return ["2606:4700:4700::1111"]

    assert await target_problem("https://hooks.example.com/x", WebhookSettings(), resolve) is None


async def _public(host: str) -> list[str]:
    return ["93.184.215.14"]


class _Rebinding:
    """A DNS answer that is public at first and private afterwards."""

    def __init__(self) -> None:
        self.lookups = 0

    async def __call__(self, host: str) -> list[str]:
        self.lookups += 1
        return ["93.184.215.14"] if self.lookups == 1 else ["10.0.0.5"]


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int:
        self.calls.append((url, tuple(addresses)))
        return 204


async def test_sender_is_pinned_to_the_checked_addresses_and_rebinding_is_refused() -> None:
    claimed = _Claimed("acme", 1, "https://hooks.example.com/x", b"k" * 32, "e", {}, 0)
    resolve, send = _Rebinding(), _Recorder()

    def clock() -> datetime:
        return datetime(2026, 10, 21, tzinfo=UTC)

    first = await _attempt(claimed, WebhookSettings(), send, clock, resolve)
    assert first.status == DELIVERED
    # one lookup per attempt, and the sender gets the addresses that lookup checked
    assert resolve.lookups == 1
    assert send.calls == [("https://hooks.example.com/x", ("93.184.215.14",))]

    second = await _attempt(claimed, WebhookSettings(), send, clock, resolve)
    assert second.status == DEAD
    assert second.error == "target refused: resolves to a private address"
    assert len(send.calls) == 1


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
        ({"lease_s": 70, "pass_timeout_s": 60, "timeout_s": 10}, "pass_timeout_s"),
        ({"connect_timeout_s": 20, "timeout_s": 10}, "connect_timeout_s"),
        ({"concurrency": 0}, "webhooks.concurrency"),
        ({"allowed_schemes": []}, "webhooks.allowed_schemes"),
    ],
)
def test_invalid_values_name_the_key(values: dict[str, object], needle: str) -> None:
    with pytest.raises(ConfigError) as caught:
        webhook_settings(Settings(webhooks=values))
    assert needle in str(caught.value)


# A receiver that is gone or silent: every attempt ends within `timeout_s`, and a pass within
# `pass_timeout_s` plus one attempt, whatever the lookup or the sender do.

FAST = WebhookSettings(timeout_s=0.2, connect_timeout_s=0.1, pass_timeout_s=0.5, lease_s=5)


def fixed_clock() -> datetime:
    return datetime(2026, 10, 21, tzinfo=UTC)


async def public(host: str) -> list[str]:
    return ["93.184.215.14"]


async def hanging_lookup(host: str) -> list[str]:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


class _Hanging:
    """Never answers for `silent` hosts; answers 204 to the others."""

    def __init__(self, silent: frozenset[str] = frozenset({"gone.example.com"})) -> None:
        self.silent = silent
        self.sent: list[str] = []

    async def __call__(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout_s: float,
        addresses: Sequence[str],
    ) -> int:
        if urlsplit(url).hostname in self.silent:
            await asyncio.Event().wait()
        self.sent.append(url)
        return 204


def claimed(n: int, host: str = "gone.example.com", attempts: int = 0) -> _Claimed:
    return _Claimed("acme", n, f"https://{host}/hook", b"k" * 32, "e", {}, attempts)


async def test_a_silent_receiver_fails_the_attempt_at_the_timeout() -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = await _attempt(claimed(1), FAST, _Hanging(), fixed_clock, public)
    assert loop.time() - started < 1
    assert (outcome.status, outcome.timed_out) == (PENDING, True)
    assert outcome.error == "timed out after 0.2s"

    last = await _attempt(
        claimed(1, attempts=FAST.max_attempts - 1), FAST, _Hanging(), fixed_clock, public
    )
    assert (last.status, last.timed_out) == (DEAD, True)  # attempts used up: dead letter


async def test_a_hanging_lookup_counts_in_the_attempt_timeout() -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    outcome = await _attempt(claimed(1), FAST, _Hanging(), fixed_clock, hanging_lookup)
    assert loop.time() - started < 1
    assert (outcome.status, outcome.timed_out) == (PENDING, True)


async def test_a_silent_receiver_does_not_hold_the_others_of_a_pass() -> None:
    send = _Hanging()
    deliveries = [claimed(1), claimed(2, "ok.example.com"), claimed(3, "ok.example.com")]
    settings = FAST.model_copy(update={"concurrency": 2})
    outcomes, left = await _attempt_all(deliveries, settings, send, fixed_clock, public)
    assert left == []
    assert {o.claimed.id: o.status for o in outcomes} == {1: PENDING, 2: DELIVERED, 3: DELIVERED}
    assert len(send.sent) == 2


async def test_a_pass_starts_no_attempt_after_its_deadline() -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    deliveries = [claimed(n) for n in range(1, 11)]
    settings = FAST.model_copy(update={"concurrency": 1})
    outcomes, left = await _attempt_all(deliveries, settings, _Hanging(), fixed_clock, public)
    # 0.5 s of pass with 0.2 s attempts: three start, the rest go back to the queue untried
    assert [o.claimed.id for o in outcomes] == [1, 2, 3]
    assert [c.id for c in left] == list(range(4, 11))
    assert loop.time() - started < settings.pass_timeout_s + settings.timeout_s + 0.5
