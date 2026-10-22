"""The `vault` settings section: keys of the personal data vault (design 13.5)."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "vault"
MIN_KEY_LENGTH = 32


class ErasedValues(StrEnum):
    """What a write of a value erased before does (`vault.erased_values`)."""

    ERASE = "erase"
    """Store it erased: the Record holds an erased reference, as after the erasure."""
    REFUSE = "refuse"
    """Refuse the commit (`value.vault_erased`)."""
    ALLOW = "allow"
    """Store it again under a new token, readable until erased again."""


class VaultSettings(SectionModel):
    """Keys of the vault. Keeper, Scribe and every process that commits need the same values.

    Without keys, commits that store a value of a vault Attribute are refused
    (`value.vault_unavailable`) and reads show vaulted values masked.
    """

    token_key: SecretValue | None = Field(
        default=None,
        description="Key (at least 32 characters) of the vault tokens that Record objects hold "
        "and of the value fingerprints that erasure by value matches, e.g. "
        "secret://env/LOREBASE_VAULT_TOKEN_KEY. To rotate it, give the new key a new "
        "token_key_id and move the old one to previous_token_keys: tokens already stored keep "
        "working, new values get tokens under the new key, and `lorebase admin vault rotate` "
        "moves the fingerprints to it.",
    )
    token_key_id: str = Field(
        default="1",
        min_length=1,
        max_length=64,
        description="Id of token_key, stored with each fingerprint made under it.",
    )
    previous_token_keys: dict[str, SecretValue] = Field(
        default_factory=dict,
        description="Earlier token keys by id: a value written again finds the token it had "
        "under them, and erasure by value matches fingerprints made under them. Remove one "
        "when `lorebase admin vault status` shows no fingerprint and no erasure record under "
        "its id.",
    )
    digest_key: SecretValue | None = Field(
        default=None,
        description="Key (at least 32 characters) of the keyed digests Lorebase stores of "
        "inputs that may hold personal data (Operation parameters, the Record keys of erasure "
        "audit entries). Default: derived from token_key when the vault is configured, else "
        "the random installation secret revision 0027 stored in the database. Set it to keep "
        "the digests unconfirmable for whoever reads the database.",
    )
    erased_values: ErasedValues = Field(
        default=ErasedValues.ERASE,
        description="A later write of an erased value (a Source sync, say): erase stores it "
        "erased, refuse fails the commit, allow stores it again until erased again.",
    )
    tombstone_origins: tuple[str, ...] = Field(
        default=("source_sync", "extraction", "import"),
        description="Commit origins whose writes are checked against the fingerprints of erased "
        "values (erased_values). Writes of people and agents are not: they choose the value, so "
        "the outcome would tell them whether a guessed value was erased.",
    )
    lock_timeout_ms: int = Field(
        default=10_000,
        ge=0,
        description="How long a commit storing vault values waits for an erasure in the same "
        "Domain to finish (0: no limit).",
    )
    rekey_batch: int = Field(
        default=500,
        ge=1,
        le=10_000,
        description="Vault rows re-encrypted per transaction by the rotation job.",
    )
    rekey_rows_per_job: int = Field(
        default=50_000,
        ge=1,
        description="Rows one rotation job handles before it queues its continuation.",
    )
    encryption_key: SecretValue | None = Field(
        default=None,
        description="Key (at least 32 characters) that encrypts new vault values (AES-256-GCM), "
        "e.g. secret://env/LOREBASE_VAULT_KEY.",
    )
    key_id: str = Field(
        default="1",
        min_length=1,
        max_length=64,
        description="Id stored with each value encrypted under encryption_key; give the key a "
        "new id when it changes and move the old one to previous_keys.",
    )
    previous_keys: dict[str, SecretValue] = Field(
        default_factory=dict,
        description="Earlier encryption keys by id: values stored under them stay readable. "
        "Remove one only when no vault row has its id.",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        keys = [
            self.token_key,
            self.encryption_key,
            self.digest_key,
            *self.previous_keys.values(),
            *self.previous_token_keys.values(),
        ]
        if any(key is not None and len(key.get_secret_value()) < MIN_KEY_LENGTH for key in keys):
            raise ValueError(f"vault keys need at least {MIN_KEY_LENGTH} characters")
        if (self.token_key is None) != (self.encryption_key is None):
            raise ValueError("vault.token_key and vault.encryption_key are set together")
        if self.previous_keys and self.encryption_key is None:
            raise ValueError("vault.previous_keys need an encryption_key")
        if self.key_id in self.previous_keys:
            raise ValueError(f"vault.key_id {self.key_id!r} is also in previous_keys")
        if self.previous_token_keys and self.token_key is None:
            raise ValueError("vault.previous_token_keys need a token_key")
        if self.token_key_id in self.previous_token_keys:
            raise ValueError(
                f"vault.token_key_id {self.token_key_id!r} is also in previous_token_keys"
            )
        return self

    @property
    def configured(self) -> bool:
        return self.encryption_key is not None


register_section(SECTION, VaultSettings)


def vault_settings(settings: Settings) -> VaultSettings:
    """The validated `vault` section."""
    return settings.section(SECTION, VaultSettings)
