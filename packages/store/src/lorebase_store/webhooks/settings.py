"""The `webhooks` settings section: targets Keeper accepts and how the worker delivers."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "webhooks"
MIN_ENCRYPTION_KEY_LENGTH = 32


class WebhookSettings(SectionModel):
    """Webhook management (Keeper) and delivery (`deliver_pending`)."""

    allowed_schemes: tuple[str, ...] = Field(
        default=("https",), min_length=1, description="URL schemes a webhook may use."
    )
    allow_private_targets: bool = Field(
        default=False,
        description="Allow loopback, private and link-local target addresses. Off by default so "
        "Domain administrators cannot reach internal services.",
    )
    max_per_domain: int = Field(default=20, ge=1, description="Webhooks of one Domain.")
    secret_bytes: int = Field(default=32, ge=16, description="Random bytes of a new secret.")
    encryption_key: SecretValue | None = Field(
        default=None,
        description="Key (at least 32 characters) that encrypts webhook signing keys in the "
        "database, e.g. secret://env/LOREBASE_WEBHOOK_KEY; Keeper and Scribe need the same "
        "value. Unset: Keeper refuses to create webhooks unless allow_unsealed_keys is true. "
        "Once set, the delivery worker encrypts the keys stored usable before.",
    )
    allow_unsealed_keys: bool = Field(
        default=False,
        description="For local development only: without encryption_key, store the signing "
        "keys of new webhooks usable instead of refusing to create them.",
    )
    previous_encryption_keys: tuple[SecretValue, ...] = Field(
        default=(),
        description="Earlier encryption keys, tried when the current one does not open a "
        "signing key; the worker encrypts such keys again under encryption_key. Remove one "
        "once no webhook uses it.",
    )
    signature_header: str = Field(default="Lorebase-Signature", min_length=1)
    event_header: str = Field(default="Lorebase-Event", min_length=1)
    delivery_header: str = Field(default="Lorebase-Delivery", min_length=1)
    idempotency_header: str = Field(
        default="Idempotency-Key",
        min_length=1,
        description="Header carrying `evt_<sequence>`, the same on every attempt of an event: "
        "deliveries are at least once, receivers drop the repeats by it.",
    )
    masked_value: str = Field(
        default="***",
        description="What a sensitive value is replaced by for a webhook not cleared for it.",
    )
    erased_value: str = Field(
        default="[erased]",
        description="What a cleared webhook receives for a vault value erased before sending.",
    )
    timeout_s: float = Field(
        default=10.0,
        gt=0,
        description="Time budget of one attempt as a whole: host lookup, connection to each "
        "checked address, sending and the answer's status. Past it the attempt fails and is "
        "retried like any other failure.",
    )
    connect_timeout_s: float = Field(
        default=5.0,
        gt=0,
        description="Time to open a connection to one address; the next checked address is "
        "tried after it, within timeout_s.",
    )
    max_attempts: int = Field(
        default=8, ge=1, description="Attempts before a delivery is marked dead."
    )
    backoff_base_s: float = Field(default=30.0, gt=0, description="Delay after the first failure.")
    backoff_factor: float = Field(default=2.0, ge=1, description="Growth of each next delay.")
    backoff_max_s: float = Field(default=3600.0, gt=0, description="Longest delay.")
    lease_s: float = Field(
        default=120.0,
        gt=0,
        description="A claimed delivery is retried by another worker after this long if its "
        "result was never recorded; a pass running longer than this is abandoned.",
    )
    batch: int = Field(default=100, ge=1, description="Events and deliveries per worker pass.")
    concurrency: int = Field(
        default=8,
        ge=1,
        description="Attempts of one pass in flight at the same time, so a target that does "
        "not answer holds one of them, not the whole pass.",
    )
    pass_timeout_s: float = Field(
        default=60.0,
        gt=0,
        description="A pass starts no attempt after this long; the deliveries it claimed and did "
        "not attempt are released to the next pass without counting an attempt.",
    )
    changed_keys_max: int = Field(
        default=100, ge=0, description="Changed Record keys listed per event payload."
    )
    retention_s: float = Field(
        default=7 * 24 * 3600.0,
        gt=0,
        description="Finished deliveries older than this are deleted by the worker (outbox "
        "events follow store.outbox.retention_s).",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.backoff_base_s > self.backoff_max_s:
            raise ValueError("backoff_base_s cannot exceed backoff_max_s")
        if self.connect_timeout_s > self.timeout_s:
            raise ValueError("connect_timeout_s cannot exceed timeout_s")
        if self.lease_s <= self.pass_timeout_s + self.timeout_s:
            raise ValueError("lease_s must exceed pass_timeout_s + timeout_s")
        keys = [self.encryption_key, *self.previous_encryption_keys]
        if any(
            key is not None and len(key.get_secret_value()) < MIN_ENCRYPTION_KEY_LENGTH
            for key in keys
        ):
            raise ValueError(
                f"webhook encryption keys need at least {MIN_ENCRYPTION_KEY_LENGTH} characters"
            )
        if self.previous_encryption_keys and self.encryption_key is None:
            raise ValueError("previous_encryption_keys need an encryption_key")
        return self


register_section(SECTION, WebhookSettings)


def webhook_settings(settings: Settings) -> WebhookSettings:
    """The validated `webhooks` section."""
    return settings.section(SECTION, WebhookSettings)
