"""The `vault` settings section: keys of the personal data vault (design 13.5)."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "vault"
MIN_KEY_LENGTH = 32


class VaultSettings(SectionModel):
    """Keys of the vault. Keeper, Scribe and every process that commits need the same values.

    Without keys, commits that store a value of a vault Attribute are refused
    (`value.vault_unavailable`) and reads show vaulted values masked.
    """

    token_key: SecretValue | None = Field(
        default=None,
        description="Key (at least 32 characters) of the vault tokens that Record objects hold "
        "and of the value fingerprints that erasure by value matches, e.g. "
        "secret://env/LOREBASE_VAULT_TOKEN_KEY. Never change it once values are stored: the "
        "same value would get another token, which reads as a change of every vaulted value.",
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
        keys = [self.token_key, self.encryption_key, *self.previous_keys.values()]
        if any(key is not None and len(key.get_secret_value()) < MIN_KEY_LENGTH for key in keys):
            raise ValueError(f"vault keys need at least {MIN_KEY_LENGTH} characters")
        if (self.token_key is None) != (self.encryption_key is None):
            raise ValueError("vault.token_key and vault.encryption_key are set together")
        if self.previous_keys and self.encryption_key is None:
            raise ValueError("vault.previous_keys need an encryption_key")
        if self.key_id in self.previous_keys:
            raise ValueError(f"vault.key_id {self.key_id!r} is also in previous_keys")
        return self

    @property
    def configured(self) -> bool:
        return self.encryption_key is not None


register_section(SECTION, VaultSettings)


def vault_settings(settings: Settings) -> VaultSettings:
    """The validated `vault` section."""
    return settings.section(SECTION, VaultSettings)
