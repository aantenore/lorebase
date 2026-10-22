"""The `guide` settings section, registered with `lorebase_platform`.

Guide holds no Keeper credential of its own: every read carries the caller's credential
(see `auth.py`). Model settings come from the `inference` section; the exploration agent uses
the chat model of the task named by `guide.agent.task`, so `inference.llm.tasks.<task>` can
point it at another model or endpoint than Scribe's extraction.
"""

from __future__ import annotations

import re
from typing import Self

from pydantic import Field, field_validator, model_validator

from lorebase_platform.settings import SectionModel, Settings, register_section

SECTION = "guide"
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_COOKIE_NAME = re.compile(r"^[A-Za-z0-9_.-]{0,128}$")


class KeeperConnection(SectionModel):
    """How Guide reaches Keeper's REST API."""

    base_url: str = Field(
        default="http://127.0.0.1:8080",
        min_length=1,
        description="Keeper origin as seen from Guide, e.g. http://keeper:8080.",
    )
    api_prefix: str = Field(default="/api/v1", description="Path prefix of the REST API.")
    timeout_s: float = Field(default=30, gt=0, description="Timeout of one Keeper request.")
    max_retries: int = Field(
        default=1, ge=0, description="Resends after 429, 503 or no connection; 0: none."
    )
    retry_max_wait_s: float = Field(
        default=3, gt=0, description="Longest pause before a retry; Retry-After is capped to it."
    )
    verify_tls: bool = Field(
        default=True,
        description="Verify Keeper's TLS certificate. Keeper is reached directly: proxy "
        "environment variables are ignored.",
    )
    user_agent: str = Field(default="lorebase-guide", min_length=1)

    @field_validator("base_url", "api_prefix")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")


class HttpSettings(SectionModel):
    """The HTTP service and how callers authenticate to it."""

    allowed_origins: tuple[str, ...] = Field(
        default=(),
        description="Browser origins allowed to call Guide (CORS), e.g. the web app's origin. "
        "A request whose Origin header is not listed is refused. Empty: `default_origins`.",
    )
    default_origins: tuple[str, ...] = Field(
        default=(),
        description="Origins used while `allowed_origins` is empty, e.g. the deployment's own "
        "web app origins (the compose file sets them). An explicit empty `allowed_origins` "
        "falls back to them with a warning instead of refusing every browser.",
    )
    credential_header: str = Field(
        default="Authorization",
        description="Header carrying the caller's credential (`Bearer <token or API key>`), "
        "forwarded to Keeper unchanged.",
    )
    session_cookie: str = Field(
        default="",
        description="Name of Keeper's session cookie, for a Guide served under the web app's "
        "origin (same-site proxy): the cookie and the CSRF header are forwarded to Keeper. "
        "Empty: cookies are ignored.",
    )
    csrf_header: str = Field(
        default="X-CSRF-Token",
        description="Header with the CSRF token of the session (keeper.auth.sessions."
        "csrf_header); required with the session cookie and checked against Keeper.",
    )
    allow_anonymous: bool = Field(
        default=False,
        description="Serve callers without a credential (their reads go to Keeper anonymous). "
        "Only for a Keeper with auth mode none, on a local stack.",
    )
    max_body_bytes: int = Field(default=16_384, ge=1024, description="Largest request body.")

    @field_validator("credential_header", "csrf_header")
    @classmethod
    def _header_name(cls, value: str) -> str:
        if not _HEADER_NAME.match(value):
            raise ValueError("must be a header name (letters, digits and dashes)")
        return value

    @field_validator("session_cookie")
    @classmethod
    def _cookie_name(cls, value: str) -> str:
        if not _COOKIE_NAME.match(value):
            raise ValueError("must be a cookie name")
        return value

    @field_validator("allowed_origins", "default_origins")
    @classmethod
    def _origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for origin in value:
            if origin == "*" or not origin.startswith(("http://", "https://")):
                raise ValueError("origins are explicit http(s)://host[:port] values")
        return tuple(origin.rstrip("/").lower() for origin in value)

    @property
    def origins(self) -> tuple[str, ...]:
        """The browser origins accepted: `allowed_origins`, or `default_origins` when empty."""
        return self.allowed_origins or self.default_origins

    @property
    def origins_fell_back(self) -> bool:
        """Whether `allowed_origins` was set to an empty list and `default_origins` apply."""
        return (
            "allowed_origins" in self.model_fields_set
            and not self.allowed_origins
            and bool(self.default_origins)
        )


class TraceSettings(SectionModel):
    """Agent trace headers and notes (docs/agent-trace.md)."""

    enabled: bool = Field(
        default=True,
        description="Send the session headers on every Keeper read of an exploration and "
        "post the model's reasoning as session notes.",
    )
    session_header: str = Field(default="X-Lorebase-Session", description="Session id header.")
    tool_header: str = Field(default="X-Lorebase-Tool", description="Tool name header.")
    opt_in_header: str = Field(
        default="X-Lorebase-Trace", description="Header opting the request in to tracing."
    )
    opt_in_value: str = Field(default="on", description="Value of `opt_in_header`; empty: unset.")
    notes: bool = Field(
        default=True,
        description="Post the model's reasoning as session notes. A note is a write to "
        "Keeper's trace store, made with the caller's credential; false keeps the headers only.",
    )
    notes_path: str = Field(
        default="/trace/sessions/{session}/notes",
        description="Path (under the API prefix) notes are posted to; `{session}` is replaced.",
    )
    max_note_chars: int = Field(default=2_000, ge=64, description="Longer notes are cut.")

    @field_validator("session_header", "tool_header", "opt_in_header")
    @classmethod
    def _header_name(cls, value: str) -> str:
        if not _HEADER_NAME.match(value):
            raise ValueError("must be a header name (letters, digits and dashes)")
        return value

    @field_validator("notes_path")
    @classmethod
    def _notes_path(cls, value: str) -> str:
        if not value.startswith("/") or "{session}" not in value or "//" in value:
            raise ValueError("must be an absolute path containing {session}")
        return value


class AgentSettings(SectionModel):
    """The read-only exploration agent and its budgets. A request may lower every budget, never
    raise it."""

    task: str = Field(
        default="explore",
        min_length=1,
        description="Task name of the chat model (inference.llm.tasks.<task> overrides it).",
    )
    max_steps: int = Field(default=8, ge=1, le=50, description="Tool calls per exploration.")
    max_tokens_per_call: int = Field(
        default=1_024, ge=64, description="Completion token cap of one model call."
    )
    max_total_tokens: int = Field(
        default=40_000, ge=500, description="Input plus output tokens of one exploration."
    )
    timeout_s: float = Field(default=120, gt=0, description="Wall-clock limit of one exploration.")
    max_question_chars: int = Field(default=2_000, ge=16, description="Longest question.")
    max_result_chars: int = Field(
        default=6_000, ge=500, description="Tool result text shown to the model; longer is cut."
    )
    max_value_chars: int = Field(
        default=300, ge=16, description="Longest single value inside a tool result."
    )
    max_items: int = Field(default=20, ge=1, le=200, description="Items per list in a result.")
    max_depth: int = Field(default=2, ge=1, le=5, description="Neighborhood depth cap.")
    max_nodes: int = Field(default=60, ge=1, description="Neighborhood node cap.")
    max_context_tokens: int = Field(
        default=3_000, ge=200, description="Token budget cap of a context pack tool call."
    )
    max_answer_chars: int = Field(default=4_000, ge=200, description="Longest final answer.")
    temperature: float | None = Field(
        default=None,
        ge=0,
        le=2,
        description="Sampling temperature of the agent's calls; null: the chat model's own "
        "(inference.llm.temperature, which may itself leave it out).",
    )


class LimitSettings(SectionModel):
    """Protection of the model budget against abuse; per process (one replica)."""

    max_concurrent: int = Field(default=4, ge=1, description="Explorations running at once.")
    max_concurrent_per_caller: int = Field(
        default=1, ge=1, description="Explorations of one credential running at once."
    )
    explorations_per_minute: int = Field(
        default=6, ge=1, description="Explorations one credential may start per minute."
    )
    embeds_per_minute: int = Field(
        default=120, ge=1, description="Embedding requests one credential may make per minute."
    )
    max_embed_chars: int = Field(default=2_000, ge=16, description="Longest text to embed.")
    max_concurrent_verifications: int = Field(
        default=16,
        ge=1,
        description="Credential checks against Keeper running at once; further requests are "
        "refused (429 busy) before any check.",
    )
    max_tracked_callers: int = Field(
        default=10_000,
        ge=10,
        description="Callers whose recent requests are counted; while the table is full of "
        "callers active in the last minute, new callers are refused (429 busy).",
    )


class GuideSettings(SectionModel):
    """Guide: exploration assistant and query embeddings, outside Keeper (docs/guide.md)."""

    host: str = Field(default="127.0.0.1", min_length=1, description="Bind address.")
    port: int = Field(default=8095, ge=1, le=65535)
    keeper: KeeperConnection = Field(default_factory=KeeperConnection)
    http: HttpSettings = Field(default_factory=HttpSettings)
    trace: TraceSettings = Field(default_factory=TraceSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    limits: LimitSettings = Field(default_factory=LimitSettings)
    readiness_timeout_s: float = Field(default=3, gt=0, description="Keeper probe of /readyz.")
    data_dir: str = Field(
        default="~/.local/state/lorebase/guide",
        min_length=1,
        description="Directory of Guide's own state files (`~` is expanded).",
    )
    mode_memory_file: str | None = Field(
        default="structured_output_modes.json",
        description="File (relative to data_dir unless absolute) remembering, per chat endpoint, "
        "the structured output mode it accepts, so a restart does not retry a refused mode; "
        "null keeps it in the process only.",
    )

    @model_validator(mode="after")
    def _per_caller_within_total(self) -> Self:
        if self.limits.max_concurrent_per_caller > self.limits.max_concurrent:
            raise ValueError("limits.max_concurrent_per_caller cannot exceed max_concurrent")
        return self


register_section(SECTION, GuideSettings)


def guide_settings(settings: Settings) -> GuideSettings:
    """The validated `guide` section of loaded settings."""
    return settings.section(SECTION, GuideSettings)
