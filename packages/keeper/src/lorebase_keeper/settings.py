"""The `keeper` settings section, registered with `lorebase_platform`.

Every value has a code default for local runs; deployments override them through the YAML file
(`keeper:`) or environment variables (`LOREBASE__KEEPER__PORT`, ...). See
`config/lorebase.example.yaml`.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from lorebase_keeper.auth.config import AuthSettings, is_loopback
from lorebase_keeper.client_address import normalize_networks
from lorebase_keeper.ratelimit.config import QuotaSettings, RateLimitSettings
from lorebase_keeper.services.config import (
    AgentWriteSettings,
    BranchSettings,
    DocumentSettings,
    DomainSettings,
    OperationSettings,
    ProposalSettings,
    ReconciliationSettings,
    SchemaChangeSettings,
    SchemaInferenceSettings,
    SchemaSuggestionSettings,
    StewardSettings,
)
from lorebase_keeper.tracing.config import TraceSettings
from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "keeper"


REQUIRED_CORS_REQUEST_HEADERS = (
    "Authorization",
    "Content-Type",
    "Idempotency-Key",
    "If-None-Match",
)
"""Request headers the clients send, always allowed cross-origin (plus the request id, session
CSRF and agent trace headers Keeper is configured with)."""

REQUIRED_CORS_RESPONSE_HEADERS = ("ETag", "Lorebase-Revision")
"""Response headers always readable cross-origin (plus the request id and rate limit headers):
the web app revalidates reads with the ETag and follows the Revision a read was served from."""


class CorsSettings(SectionModel):
    """Cross-origin access for browser clients. Disabled while `allow_origins` is empty."""

    allow_origins: tuple[str, ...] = Field(
        default=(), description="Allowed origins, e.g. https://lorebase.example.com. Empty: off."
    )
    allow_methods: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
    allow_headers: tuple[str, ...] = Field(
        default=(),
        description="Further request headers to allow. Always allowed besides them: "
        "Authorization, Content-Type, Idempotency-Key, If-None-Match, the request id header, "
        "the session CSRF header and the agent trace headers.",
    )
    expose_headers: tuple[str, ...] = Field(
        default=REQUIRED_CORS_RESPONSE_HEADERS,
        description="Response headers readable by cross-origin scripts. ETag and "
        "Lorebase-Revision (conditional reads), the request id and the rate limit headers are "
        "always exposed.",
    )
    allow_credentials: bool = False
    max_age_s: int = Field(default=600, ge=0, description="Preflight cache lifetime.")

    @model_validator(mode="after")
    def _no_wildcard_with_credentials(self) -> Self:
        if self.allow_credentials and "*" in self.allow_origins:
            raise ValueError("allow_origins cannot contain '*' when allow_credentials is true")
        return self


class HstsSettings(SectionModel):
    """`Strict-Transport-Security`, which only makes sense when the connection is TLS."""

    enabled: Literal["auto", "always", "never"] = Field(
        default="auto",
        description="auto: send it when the request arrived over https (directly, or through a "
        "trusted proxy that sets X-Forwarded-Proto: https); always; never.",
    )
    max_age_s: int = Field(default=31_536_000, ge=0, description="max-age directive.")
    include_subdomains: bool = True
    preload: bool = Field(
        default=False,
        description="Add the preload directive; needs include_subdomains and a max_age_s of at "
        "least one year.",
    )

    @model_validator(mode="after")
    def _preload_requirements(self) -> Self:
        if self.preload and (not self.include_subdomains or self.max_age_s < 31_536_000):
            raise ValueError("preload needs include_subdomains and max_age_s of at least 31536000")
        return self

    @property
    def header_value(self) -> str:
        parts = [f"max-age={self.max_age_s}"]
        if self.include_subdomains:
            parts.append("includeSubDomains")
        if self.preload:
            parts.append("preload")
        return "; ".join(parts)


class SecurityHeadersSettings(SectionModel):
    """Response headers that limit what a browser does with Keeper's responses. A header a
    route sets itself is never replaced."""

    enabled: bool = Field(default=True, description="Master switch for all of these headers.")
    hsts: HstsSettings = Field(default_factory=HstsSettings)
    frame_options: Literal["DENY", "SAMEORIGIN", "off"] = Field(
        default="DENY",
        description="X-Frame-Options, and the matching CSP frame-ancestors ('none' or 'self'). "
        "off sends neither.",
    )
    referrer_policy: str = Field(
        default="no-referrer", description="Referrer-Policy value. Empty: not sent."
    )
    content_type_nosniff: bool = Field(
        default=True, description="Send X-Content-Type-Options: nosniff."
    )


class LimitSettings(SectionModel):
    """Request size limits, rate limits and daily quotas."""

    max_request_body_bytes: int = Field(
        default=1_048_576, ge=1, description="Larger request bodies are rejected with 413."
    )
    rate: RateLimitSettings = Field(default_factory=RateLimitSettings)
    quota: QuotaSettings = Field(default_factory=QuotaSettings)


class PaginationSettings(SectionModel):
    """Cursor pagination bounds shared by list endpoints."""

    default_limit: int = Field(default=50, ge=1)
    max_limit: int = Field(default=500, ge=1)

    @model_validator(mode="after")
    def _default_within_max(self) -> Self:
        if self.default_limit > self.max_limit:
            raise ValueError("default_limit cannot exceed max_limit")
        return self


class ReadCacheSettings(SectionModel):
    """In-process cache of GET read responses, keyed by the Revision they were served from.

    Entries never go stale (a new commit means a new Revision, hence a new key); they leave
    by size, count or time to live.
    """

    enabled: bool = True
    max_entries: int = Field(default=1024, ge=1, description="Responses kept.")
    max_bytes: int = Field(
        default=32 * 1024 * 1024, ge=1, description="Total size of kept response bodies."
    )
    ttl_s: float = Field(default=300, gt=0, description="Lifetime of an entry in seconds.")
    max_body_bytes: int = Field(
        default=1024 * 1024, ge=1, description="Larger responses are not cached."
    )
    time_travel_max_entries: int = Field(
        default=128,
        ge=0,
        description="Entries for reads served from the tree at another Revision; once reached, "
        "such reads are not cached. 0 never caches them.",
    )


class SensitivitySettings(SectionModel):
    """How reads show values of sensitive Attributes (design 10.2, 13.5).

    A caller sees the values of an Attribute tagged `pii`, `confidential` or `restricted` only
    with the matching clearance (`pii.read`, `confidential.read`, `restricted.read`) on its
    Kind; other callers get `masked_value`, or no value at all with `omit`.
    """

    omit: bool = Field(
        default=False,
        description="Leave masked values out of Records instead of showing masked_value. "
        "Values shown outside Records (history, lineage, diffs, Conflicts) are always masked.",
    )
    masked_value: str = Field(
        default="***", description="What a value is replaced by when it is masked."
    )
    erased_value: str = Field(
        default="[erased]",
        description="What cleared callers see for a vault value that was erased.",
    )


class ReadsSettings(SectionModel):
    """Read responses: authorization of Revisions named by id or tag, response cache, and the
    database serving them (design 15.3)."""

    cache: ReadCacheSettings = Field(default_factory=ReadCacheSettings)
    sensitivity: SensitivitySettings = Field(default_factory=SensitivitySettings)
    reachability_max_depth: int = Field(
        default=1000,
        ge=1,
        description="Parent hops followed from branch heads when checking that a Revision or "
        "tag named by a caller with branch-scoped grants is reachable from a branch the caller "
        "may read. A Revision further back than this is refused (403).",
    )
    prefer_replica: bool = Field(
        default=False,
        description="Serve reads from the read replicas named by `database.read_dsn` when one "
        "is fresh enough; otherwise, or when false, from the primary. Without replicas "
        "configured it has no effect.",
    )


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
    trusted_proxies: tuple[str, ...] = Field(
        default=(),
        description="Networks of the reverse proxies in front of Keeper, e.g. 10.0.0.0/8. Only "
        "when the socket peer is in one of them is X-Forwarded-For read (right to left, "
        "skipping trusted hops) to find the client address used by API key allowlists and "
        "failed authentication limits. Empty: the socket peer is the client.",
    )
    cors: CorsSettings = Field(default_factory=CorsSettings)
    security_headers: SecurityHeadersSettings = Field(default_factory=SecurityHeadersSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    pagination: PaginationSettings = Field(default_factory=PaginationSettings)
    reads: ReadsSettings = Field(default_factory=ReadsSettings)
    readiness: ReadinessSettings = Field(default_factory=ReadinessSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    openapi: OpenApiSettings = Field(default_factory=OpenApiSettings)
    domains: DomainSettings = Field(default_factory=DomainSettings)
    branches: BranchSettings = Field(default_factory=BranchSettings)
    schema_changes: SchemaChangeSettings = Field(default_factory=SchemaChangeSettings)
    schema_inference: SchemaInferenceSettings = Field(default_factory=SchemaInferenceSettings)
    schema_suggestions: SchemaSuggestionSettings = Field(default_factory=SchemaSuggestionSettings)
    operations: OperationSettings = Field(default_factory=OperationSettings)
    proposals: ProposalSettings = Field(default_factory=ProposalSettings)
    agent_writes: AgentWriteSettings = Field(default_factory=AgentWriteSettings)
    reconciliation: ReconciliationSettings = Field(default_factory=ReconciliationSettings)
    documents: DocumentSettings = Field(default_factory=DocumentSettings)
    stewards: StewardSettings = Field(default_factory=StewardSettings)
    trace: TraceSettings = Field(default_factory=TraceSettings)

    @field_validator("trusted_proxies")
    @classmethod
    def _proxy_networks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return normalize_networks(value)

    @field_validator("api_prefix")
    @classmethod
    def _prefix_shape(cls, value: str) -> str:
        if not value.startswith("/") or (len(value) > 1 and value.endswith("/")):
            raise ValueError("must start with '/' and not end with '/'")
        return "" if value == "/" else value

    @model_validator(mode="after")
    def _no_anonymous_access_off_loopback(self) -> Self:
        if self.auth.mode == "none" and not self.auth.allow_insecure and not is_loopback(self.host):
            raise ValueError(
                f"auth.mode 'none' accepts every request as an administrator and host "
                f"{self.host!r} is not a loopback address: choose another auth.mode, bind to "
                "127.0.0.1, or set auth.allow_insecure to true"
            )
        return self


register_section(SECTION, KeeperSettings)


def keeper_settings(settings: Settings) -> KeeperSettings:
    """The validated `keeper` section of loaded settings."""
    return settings.section(SECTION, KeeperSettings)
