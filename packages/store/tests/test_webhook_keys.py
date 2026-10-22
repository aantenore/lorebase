"""Sealing webhook signing keys (`lorebase_store.webhooks.keys`) and the shared keyring."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ValidationError

from lorebase_store.keyring import Keyring
from lorebase_store.webhooks import SigningKeys, StoredKey, WebhookSettings, signing_key

KEY = signing_key("whsec_test")
CURRENT, PREVIOUS, OTHER = "c" * 32, "p" * 32, "o" * 32


def test_without_an_encryption_key_the_key_is_stored_usable() -> None:
    keys = SigningKeys(None)
    stored = keys.store(KEY)
    assert stored == StoredKey(secret_hash=KEY, sealed_key=None)
    opened = keys.open(stored)
    assert opened is not None
    assert (opened.key, opened.stale) == (KEY, False)


def test_with_an_encryption_key_the_key_is_sealed() -> None:
    keys = SigningKeys(CURRENT)
    stored = keys.store(KEY)
    assert stored.secret_hash is None
    assert stored.sealed_key is not None
    assert KEY not in stored.sealed_key
    assert keys.store(KEY).sealed_key != stored.sealed_key  # a new nonce each time
    opened = keys.open(stored)
    assert opened is not None
    assert (opened.key, opened.stale) == (KEY, False)


def test_usable_and_previously_sealed_keys_are_stale() -> None:
    keys = SigningKeys(CURRENT, [PREVIOUS])
    usable = keys.open(StoredKey(KEY, None))
    assert usable is not None
    assert usable.stale
    old = SigningKeys(PREVIOUS).store(KEY)
    opened = keys.open(old)
    assert opened is not None
    assert (opened.key, opened.stale) == (KEY, True)


def test_unknown_or_altered_seals_do_not_open() -> None:
    sealed = SigningKeys(OTHER).store(KEY)
    assert SigningKeys(CURRENT, [PREVIOUS]).open(sealed) is None
    assert SigningKeys(None).open(sealed) is None
    assert sealed.sealed_key is not None
    altered = StoredKey(None, sealed.sealed_key[:-1] + bytes([sealed.sealed_key[-1] ^ 1]))
    assert SigningKeys(OTHER).open(altered) is None


def test_settings_need_long_keys_and_a_current_one() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        WebhookSettings.model_validate({"encryption_key": "short"})
    with pytest.raises(ValidationError, match="need an encryption_key"):
        WebhookSettings.model_validate({"previous_encryption_keys": [PREVIOUS]})


def test_settings_resolve_secret_references(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE_TEST_WEBHOOK_KEY", CURRENT)
    settings = WebhookSettings.model_validate(
        {"encryption_key": "secret://env/LOREBASE_TEST_WEBHOOK_KEY"}
    )
    keys = SigningKeys.from_settings(settings)
    assert keys.sealing
    assert SigningKeys(CURRENT).open(keys.store(KEY)) is not None


def test_keyring_namespaces_derive_the_keys_sessions_used_before() -> None:
    """Keeper's session keys must not change with the keyring's move to the store."""
    secret, purpose, message = "s" * 32, "csrf", b"message"
    derived = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"lorebase/keeper/" + purpose.encode()
    ).derive(secret.encode())
    expected = hmac.new(derived, message, hashlib.sha256).digest()
    assert Keyring(secret, namespace="keeper").mac(purpose, message) == expected
    assert Keyring(secret, namespace="webhooks").mac(purpose, message) != expected
