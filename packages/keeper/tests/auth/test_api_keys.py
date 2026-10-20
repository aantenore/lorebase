from __future__ import annotations

import pytest

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings, AuthSettings
from lorebase_keeper.settings import keeper_settings
from lorebase_platform.settings import ConfigError, Settings

PEPPER = "test-pepper-0123456789-0123456789-abcdef"


def codec(**overrides: object) -> ApiKeyCodec:
    return ApiKeyCodec(ApiKeySettings(pepper=PEPPER, **overrides))  # type: ignore[arg-type]


def test_generated_key_has_the_documented_shape() -> None:
    generated = codec().generate()
    leading, prefix, secret = generated.token.split("_", 2)
    assert leading == "lbk"
    assert prefix == generated.prefix
    assert len(prefix) == 10
    assert len(secret) >= 40
    assert "_" not in secret


def test_only_a_hmac_of_the_secret_is_stored() -> None:
    subject = codec()
    generated = subject.generate()
    parsed = subject.parse(generated.token)
    assert parsed is not None
    assert len(generated.key_hash) == 32
    assert generated.key_hash == subject.hash_secret(parsed.secret)
    assert parsed.secret.encode() not in generated.key_hash
    assert parsed.secret not in generated.key_hash.hex()


def test_generated_keys_are_unique() -> None:
    subject = codec()
    assert len({subject.generate().token for _ in range(50)}) == 50


def test_parse_round_trip_and_match() -> None:
    subject = codec()
    generated = subject.generate()
    parsed = subject.parse(generated.token)
    assert parsed is not None
    assert parsed.prefix == generated.prefix
    assert subject.matches(parsed.secret, generated.key_hash)
    assert not subject.matches(parsed.secret + "x", generated.key_hash)


def test_pepper_changes_the_hash() -> None:
    generated = codec().generate()
    other = ApiKeyCodec(ApiKeySettings(pepper="another-pepper-0123456789-0123456789-xyz"))
    parsed = other.parse(generated.token)
    assert parsed is not None
    assert not other.matches(parsed.secret, generated.key_hash)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "lbk_",
        "lbk_short_secret",
        "lbk_abcdefghij",
        "lbk_abcdefghij_",
        "xyz_abcdefghij_secret",
        "lbk_abcdefghij_sec ret",
        "eyJhbGciOiJSUzI1NiJ9.e30.sig",
    ],
)
def test_malformed_keys_do_not_parse(token: str) -> None:
    assert codec().parse(token) is None


def test_looks_like_key_distinguishes_jwts() -> None:
    subject = codec()
    assert subject.looks_like_key(subject.generate().token)
    assert not subject.looks_like_key("eyJhbGciOiJSUzI1NiJ9.e30.sig")


def test_token_prefix_is_configurable() -> None:
    subject = codec(token_prefix="acme")
    assert subject.generate().token.startswith("acme_")
    assert subject.parse(codec().generate().token) is None


def test_codec_requires_a_pepper() -> None:
    with pytest.raises(ValueError, match="pepper"):
        ApiKeyCodec(ApiKeySettings())


@pytest.mark.parametrize("pepper", [None, "too-short"])
def test_api_key_mode_requires_a_strong_pepper(pepper: str | None) -> None:
    keeper = {"auth": {"mode": "api_key", "api_keys": {"pepper": pepper}}}
    with pytest.raises(ConfigError, match=r"api_keys\.pepper"):
        keeper_settings(Settings(keeper=keeper))


def test_pepper_can_be_a_secret_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_PEPPER", PEPPER)
    keeper = {"auth": {"mode": "api_key", "api_keys": {"pepper": "secret://env/TEST_PEPPER"}}}
    resolved = keeper_settings(Settings(keeper=keeper)).auth.api_keys.pepper
    assert resolved is not None
    assert resolved.get_secret_value() == PEPPER


def test_max_ttl_must_cover_the_default() -> None:
    with pytest.raises(ValueError, match="max_ttl_days"):
        ApiKeySettings(default_ttl_days=100, max_ttl_days=30)
    with pytest.raises(ValueError, match="max_ttl_days"):
        ApiKeySettings(default_ttl_days=None, max_ttl_days=30)


def test_mode_none_needs_no_secrets() -> None:
    assert AuthSettings().mode == "none"
    assert not AuthSettings().uses_api_keys
