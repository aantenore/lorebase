"""The `webhooks` settings section: targets Keeper accepts and how the worker delivers."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "webhooks"


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
    signature_header: str = Field(default="Lorebase-Signature", min_length=1)
    event_header: str = Field(default="Lorebase-Event", min_length=1)
    delivery_header: str = Field(default="Lorebase-Delivery", min_length=1)
    timeout_s: float = Field(default=10.0, gt=0, description="Time budget of one request.")
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
        "result was never recorded.",
    )
    batch: int = Field(default=100, ge=1, description="Events and deliveries per worker pass.")
    changed_keys_max: int = Field(
        default=100, ge=0, description="Changed Record keys listed per event payload."
    )
    retention_s: float = Field(
        default=7 * 24 * 3600.0,
        gt=0,
        description="Finished deliveries and handed-off outbox events older than this are "
        "deleted by the worker. The change feed cannot resume from deleted events.",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.backoff_base_s > self.backoff_max_s:
            raise ValueError("backoff_base_s cannot exceed backoff_max_s")
        if self.lease_s <= self.timeout_s:
            raise ValueError("lease_s must exceed timeout_s")
        return self


register_section(SECTION, WebhookSettings)


def webhook_settings(settings: Settings) -> WebhookSettings:
    """The validated `webhooks` section."""
    return settings.section(SECTION, WebhookSettings)
