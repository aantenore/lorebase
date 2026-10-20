"""The `keeper` settings section, registered with `lorebase_platform`.

Every value has a code default for local runs; deployments override them through the YAML file
(`keeper:`) or environment variables (`LOREBASE__KEEPER__PORT`, ...). See
`config/lorebase.example.yaml`.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, field_validator, model_validator

from lorebase_keeper.auth.config import AuthSettings
from lorebase_keeper.services.config import (
    BranchSettings,
    DomainSettings,
    OperationSettings,
    ReadSettings,
    SchemaChangeSettings,
)
from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "keeper"


class CorsSettings(SectionModel):
    """Cross-origin access for browser clients. Disabled while `allow_origins` is empty."""

    allow_origins: tuple[str, ...] = Field(
        default=(), description="Allowed origins, e.g. https://lorebase.example.com. Empty: off."
    )
    allow_methods: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
    allow_headers: tuple[str, ...] = ("Authorization", "Content-Type", "Idempotency-Key")
    allow_credentials: bool = False
    max_age_s: int = Field(default=600, ge=0, description="Preflight cache lifetime.")

    @model_validator(mode="after")
    def _no_wildcard_with_credentials(self) -> Self:
        if self.allow_credentials and "*" in self.allow_origins:
            raise ValueError("allow_origins cannot contain '*' when allow_credentials is true")
        return self


class LimitSettings(SectionModel):
    """Request size limits."""

    max_request_body_bytes: int = Field(
        default=1_048_576, ge=1, description="Larger request bodies are rejected with 413."
    )


class PaginationSettings(SectionModel):
    """Cursor pagination bounds shared by list endpoints."""

    default_limit: int = Field(default=50, ge=1)
    max_limit: int = Field(default=500, ge=1)

    @model_validator(mode="after")
    def _default_within_max(self) -> Self:
        if self.default_limit > self.max_limit:
            raise ValueError("default_limit cannot exceed max_limit")
        return self


class ReadinessSettings(SectionModel):
    """How `/readyz` finds and probes the database."""

    timeout_s: float = Field(default=2.0, gt=0, description="Time budget of each check.")
    database_section: str = Field(
        default="database",
        min_length=1,
        description="Settings section holding the database connection. When it is absent "
        "the database check reports not_configured.",
    )
    database_dsn_key: str = Field(
        default="dsn", min_length=1, description="Key of the connection string in that section."
    )


class OpenApiSettings(SectionModel):
    """OpenAPI document and interactive docs, served under the API prefix."""

    title: str = "Lorebase Keeper"
    description: str = (
        "REST API of Lorebase: versioned ontology and knowledge graph. Errors use RFC 9457 "
        "problem details."
    )
    docs_enabled: bool = Field(default=True, description="Serve /docs and the OpenAPI document.")


class KeeperSettings(SectionModel):
    """Settings of the Keeper service."""

    host: str = Field(default="127.0.0.1", min_length=1, description="Bind address.")
    port: int = Field(default=8080, ge=1, le=65535)
    api_prefix: str = Field(default="/api/v1", description="Version prefix of every API route.")
    request_id_header: str = Field(
        default="X-Request-ID", min_length=1, description="Header carrying the request id."
    )
    problem_type_base: str = Field(
        default="urn:lorebase:problem:",
        min_length=1,
        description="Prefix of the stable `type` URI of problem details; the error code is "
        "appended.",
    )
    cors: CorsSettings = Field(default_factory=CorsSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    pagination: PaginationSettings = Field(default_factory=PaginationSettings)
    readiness: ReadinessSettings = Field(default_factory=ReadinessSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    openapi: OpenApiSettings = Field(default_factory=OpenApiSettings)
    domains: DomainSettings = Field(default_factory=DomainSettings)
    branches: BranchSettings = Field(default_factory=BranchSettings)
    schema_changes: SchemaChangeSettings = Field(default_factory=SchemaChangeSettings)
    reads: ReadSettings = Field(default_factory=ReadSettings)
    operations: OperationSettings = Field(default_factory=OperationSettings)

    @field_validator("api_prefix")
    @classmethod
    def _prefix_shape(cls, value: str) -> str:
        if not value.startswith("/") or (len(value) > 1 and value.endswith("/")):
            raise ValueError("must start with '/' and not end with '/'")
        return "" if value == "/" else value


register_section(SECTION, KeeperSettings)


def keeper_settings(settings: Settings) -> KeeperSettings:
    """The validated `keeper` section of loaded settings."""
    return settings.section(SECTION, KeeperSettings)
