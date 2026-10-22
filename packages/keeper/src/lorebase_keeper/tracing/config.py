"""The `keeper.trace` settings: agent trace (docs/agent-trace.md)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from lorebase_platform.settings import SectionModel

SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
"""Shape of a session id: what the session header and the session routes accept."""


class TraceSettings(SectionModel):
    """Agent trace: per request of a traced agent session, the query as structured data,
    result counts, the refs (Kind and key) a response touched, latency and cache use, written
    off the request path to `agent_trace_steps`. Never values of Records.

    A request is traced when tracing is enabled and either it opts in (`opt_in_header`, when
    `allow_header_opt_in`), its principal or API key is listed, or `trace_all` is set and it
    is not traffic of the web app (unless `include_web`). Its session is the one it names
    (`session_header`), or an inferred one (`inferred_session_idle_s`).
    """

    enabled: bool = Field(
        default=False, description="Record agent traces. Needs the `database` section."
    )
    session_header: str = Field(
        default="X-Lorebase-Session",
        min_length=1,
        description="Header naming the agent session of a request (1 to 128 characters of "
        "A-Z a-z 0-9 . _ : -). Without it a traced request joins an inferred session; with a "
        "malformed one it is not traced.",
    )
    tool_header: str = Field(
        default="X-Lorebase-Tool",
        min_length=1,
        description="Header naming the agent tool that made the request (MCP tool name).",
    )
    opt_in_header: str = Field(
        default="X-Lorebase-Trace",
        min_length=1,
        description="Header opting a request in: `on`, or `debug` to add query plans "
        "(callers holding trace.explain).",
    )
    allow_header_opt_in: bool = Field(
        default=True, description="Trace requests that opt in with `opt_in_header`."
    )
    trace_all: bool = Field(
        default=False,
        description="Trace every authenticated request, except the web app's own (see "
        "`include_web`).",
    )
    client_header: str = Field(
        default="X-Lorebase-Client",
        min_length=1,
        description="Header naming the client that sent a request; the web app sends `web_client`.",
    )
    web_client: str = Field(
        default="web",
        min_length=1,
        description="Value of `client_header` that marks a request of the web app.",
    )
    include_web: bool = Field(
        default=False,
        description="Let `trace_all` trace the web app's own requests: those marked with "
        "`client_header`, and those authenticated by a browser session cookie that name no "
        "agent session. Off: the screens' traffic is not agent activity.",
    )
    principals: tuple[str, ...] = Field(
        default=(), description="Principal ids whose requests are traced."
    )
    api_keys: tuple[str, ...] = Field(
        default=(), description="API key ids whose requests are traced."
    )
    capture_literals: bool = Field(
        default=False,
        description="Keep every value compared by filters (`attr`/`derived` predicates), on "
        "sensitive Attributes too; overrides `capture_filter_values`.",
    )
    capture_filter_values: Literal["none", "non_sensitive"] = Field(
        default="non_sensitive",
        description="Values compared by filters that are kept: `non_sensitive` keeps those on "
        "fields the read finds not sensitive under the effective tags (through Traits and "
        "Derivations; `key` and `title` by their Attributes), bounded by `max_text_chars` and "
        "`max_filter_value_items`; `none` replaces every one by its type.",
    )
    max_filter_value_items: int = Field(
        default=20,
        ge=1,
        description="Items kept of a list compared by a filter (`in`); more are cut.",
    )
    capture_search_text: bool = Field(
        default=True,
        description="Keep search text (`q`, `search`, `match`); otherwise it is replaced by "
        "its length.",
    )
    max_query_bytes: int = Field(
        default=16_384,
        ge=256,
        description="Largest request body read to record the query; larger queries are "
        "recorded as truncated.",
    )
    max_text_chars: int = Field(
        default=500, ge=1, description="Longest string kept in a recorded query."
    )
    max_response_bytes: int = Field(
        default=1_048_576,
        ge=1024,
        description="Largest response inspected for counts and refs; larger ones are "
        "recorded without them.",
    )
    max_refs: int = Field(
        default=500, ge=0, description="Record and Link refs kept per step; more are cut."
    )
    max_note_chars: int = Field(default=4000, ge=1, description="Longest note an agent attaches.")
    inferred_session_idle_s: float = Field(
        default=300.0,
        gt=0,
        description="A traced request without a session id joins the inferred session of its "
        "principal and client (API key or web session, user agent) while that one saw a "
        "request within this many seconds; otherwise it starts a new one.",
    )
    max_steps_per_session: int = Field(
        default=2000,
        ge=1,
        description="Steps kept per session and principal; later ones are not written.",
    )
    retention_days: float = Field(
        default=7.0, gt=0, description="Age after which steps are deleted."
    )
    prune_interval_s: float = Field(
        default=300.0, gt=0, description="Time between two deletions of expired steps."
    )
    prune_batch: int = Field(default=10_000, ge=1, description="Steps deleted per pass at most.")
    queue_size: int = Field(
        default=10_000,
        ge=1,
        description="Steps waiting to be written; past it new steps are dropped and counted "
        "in `lorebase.trace.steps.dropped`.",
    )
    batch_size: int = Field(default=200, ge=1, description="Steps written per transaction.")
    flush_interval_s: float = Field(
        default=1.0, gt=0, description="Longest wait before queued steps are written."
    )
    write_timeout_s: float = Field(
        default=5.0, gt=0, description="Time budget of one batch; a failed batch is dropped."
    )

    @field_validator("principals", "api_keys")
    @classmethod
    def _no_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("entries cannot be blank")
        return value
