"""The `herald` settings section, registered with `lorebase_platform`.

Every value has a code default for local runs; deployments override them through the YAML file
(`herald:`), `LOREBASE__HERALD__...` variables, or the short aliases below, which MCP host
configurations usually set:

| Variable | Key |
|----------|-----|
| `LOREBASE_URL` | `herald.keeper.base_url` |
| `LOREBASE_API_KEY` | `herald.keeper.api_key` (literal or `secret://` reference) |
| `LOREBASE_DOMAIN` | `herald.domain` |
| `LOREBASE_BRANCH` | `herald.branch` |
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from lorebase_platform.secrets import SecretValue
from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "herald"
ENV_ALIASES = {
    "LOREBASE_URL": "keeper.base_url",
    "LOREBASE_API_KEY": "keeper.api_key",
    "LOREBASE_DOMAIN": "domain",
    "LOREBASE_BRANCH": "branch",
}

Transport = Literal["stdio", "streamable_http"]
OutputFormat = Literal["text", "json"]


class KeeperConnection(SectionModel):
    """How Herald reaches Keeper's REST API."""

    base_url: str = Field(
        default="http://127.0.0.1:8080",
        min_length=1,
        description="Keeper origin, e.g. https://lorebase.example.com (alias LOREBASE_URL).",
    )
    api_prefix: str = Field(default="/api/v1", description="Path prefix of the REST API.")
    api_key: SecretValue | None = Field(
        default=None,
        description="API key of the agent's service principal, used by the stdio transport "
        "only (alias LOREBASE_API_KEY). Streamable HTTP forwards each caller's own key.",
    )
    timeout_s: float = Field(default=30, gt=0, description="Timeout of one Keeper request.")
    max_retries: int = Field(
        default=2,
        ge=0,
        description="Times a request is sent again when Keeper answers 429 or 503, or could not "
        "be reached; 0 turns retries off.",
    )
    retry_max_wait_s: float = Field(
        default=5, gt=0, description="Longest pause before a retry; Retry-After is capped to it."
    )
    verify_tls: bool = Field(default=True, description="Verify Keeper's TLS certificate.")
    trust_env: bool = Field(
        default=False,
        description="Honor HTTP_PROXY, HTTPS_PROXY and NO_PROXY (and other httpx environment "
        "settings) when reaching Keeper; false ignores them and connects directly. Proxies "
        "are not used with verify_tls false.",
    )
    user_agent: str = Field(default="lorebase-herald", min_length=1)

    @field_validator("base_url", "api_prefix")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


class HttpSettings(SectionModel):
    """The Streamable HTTP transport."""

    host: str = Field(default="127.0.0.1", min_length=1, description="Bind address.")
    port: int = Field(default=8090, ge=1, le=65535)
    path: str = Field(default="/mcp", min_length=1, description="Endpoint path.")
    stateless: bool = Field(
        default=True, description="No server-side MCP sessions; any replica serves any call."
    )
    json_response: bool = Field(
        default=True, description="Answer with JSON bodies instead of SSE streams."
    )
    allowed_hosts: tuple[str, ...] = Field(
        default=(),
        description="Host header values accepted (DNS rebinding protection), e.g. "
        "herald.example.com. Empty: the SDK default for the bind address.",
    )
    allowed_origins: tuple[str, ...] = Field(
        default=(), description="Origin header values accepted with `allowed_hosts`."
    )
    credential_header: str = Field(
        default="Authorization",
        min_length=1,
        description="Request header carrying the caller's credential (`Bearer <api key>`), "
        "forwarded to Keeper unchanged.",
    )
    allow_anonymous: bool = Field(
        default=False,
        description="Forward calls that carry no credential to Keeper without one instead of "
        "refusing them. Only for a local stack whose Keeper runs with auth mode none: Keeper "
        "still authenticates every call, so under any other mode they fail as unauthorized. "
        "Calls that carry a credential forward it as always.",
    )


class OutputSettings(SectionModel):
    """Size and shape of tool outputs."""

    default_format: OutputFormat = Field(
        default="text", description="text (compact, token-lean) or json (stable shapes)."
    )
    max_chars: int = Field(
        default=12_000, ge=500, description="Longest tool output; longer ones are cut."
    )
    max_items: int = Field(default=50, ge=1, description="Items listed per list in an output.")
    max_value_chars: int = Field(
        default=240, ge=16, description="Longest single value rendered in text output."
    )
    max_issues: int = Field(default=20, ge=1, description="Issues listed in an error.")


class ContextSettings(SectionModel):
    """The context pack of `lorebase_context`."""

    token_budget: int = Field(default=3_000, ge=200, description="Default budget in tokens.")
    max_token_budget: int = Field(default=20_000, ge=200, description="Largest budget allowed.")
    depth: int = Field(default=1, ge=1, description="Neighborhood depth around the seeds.")
    max_seeds: int = Field(default=5, ge=1, description="Seed Records per pack.")
    search_seeds: int = Field(
        default=3, ge=1, description="Seeds taken from search when only a question is given."
    )

    @model_validator(mode="after")
    def _budget_within_max(self) -> Self:
        if self.token_budget > self.max_token_budget:
            raise ValueError("token_budget cannot exceed max_token_budget")
        return self


class NeighborhoodSettings(SectionModel):
    """Bounds of `lorebase_neighborhood` (Keeper enforces its own caps as well)."""

    default_depth: int = Field(default=1, ge=1)
    max_depth: int = Field(default=3, ge=1)
    default_max_nodes: int = Field(default=50, ge=1)
    max_nodes: int = Field(default=500, ge=1)
    max_edges: int = Field(default=1_000, ge=1)
    max_seeds: int = Field(default=20, ge=1)


class ChangesSettings(SectionModel):
    """Bounds of `lorebase_changes`."""

    page_size: int = Field(default=50, ge=1, description="Log entries fetched per request.")
    max_pages: int = Field(default=10, ge=1, description="Pages read looking for `since`.")


class TraceSettings(SectionModel):
    """Agent trace headers on every Keeper call (Keeper's `keeper.trace` records them)."""

    enabled: bool = Field(
        default=True,
        description="Send the session, tool and traceparent headers; false sends none of them.",
    )
    session_header: str = Field(
        default="X-Lorebase-Session",
        min_length=1,
        description="Header carrying the session id, to Keeper and, when a caller sends it, "
        "to Herald (the name configured on Keeper).",
    )
    tool_header: str = Field(
        default="X-Lorebase-Tool", min_length=1, description="Header carrying the tool name."
    )
    trace_header: str = Field(
        default="X-Lorebase-Trace",
        min_length=1,
        description="Header opting a request in to tracing (`on`, or `debug` for query plans).",
    )
    opt_in: bool = Field(
        default=False,
        description="Send `<trace_header>: on` on every call (true), or only when "
        "the caller's request carries it.",
    )
    session_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9._:-]{1,128}$",
        description="Session id used when the caller's request names none, in place of the MCP "
        "session id. Without any, no session header is sent and Keeper infers sessions.",
    )


class HeraldSettings(SectionModel):
    """The MCP server (design 11.2)."""

    transport: Transport = Field(default="stdio", description="stdio or streamable_http.")
    server_name: str = Field(default="lorebase", min_length=1, description="MCP server name.")
    domain: str | None = Field(
        default=None,
        description="Domain used when a call names none (alias LOREBASE_DOMAIN).",
    )
    branch: str | None = Field(
        default=None,
        description="Branch used when a call names none; null: the Domain's default branch "
        "(alias LOREBASE_BRANCH).",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="WARNING", description="Log level of the MCP SDK."
    )
    keeper: KeeperConnection = Field(default_factory=KeeperConnection)
    http: HttpSettings = Field(default_factory=HttpSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    neighborhood: NeighborhoodSettings = Field(default_factory=NeighborhoodSettings)
    changes: ChangesSettings = Field(default_factory=ChangesSettings)
    trace: TraceSettings = Field(default_factory=TraceSettings)


register_section(SECTION, HeraldSettings, env_aliases=ENV_ALIASES)


def herald_settings(settings: Settings) -> HeraldSettings:
    """The validated `herald` section of loaded settings."""
    return settings.section(SECTION, HeraldSettings)
