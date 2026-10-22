"""Client configuration: constructor arguments, then environment variables, then defaults.

| Argument | Environment variable | Default |
|----------|----------------------|---------|
| `base_url` | `LOREBASE_URL` | required |
| `api_key` | `LOREBASE_API_KEY` | none |
| `token` | `LOREBASE_TOKEN` | none |
| `timeout` | `LOREBASE_TIMEOUT` (seconds) | `DEFAULT_TIMEOUT_S` |
| `api_prefix` | `LOREBASE_API_PREFIX` | `DEFAULT_API_PREFIX` |
| `session_id`, `tool`, `trace` | none | none (agent trace headers) |

An API key (`lbk_...`) and an OIDC access token both travel as `Authorization: Bearer`; when
both are set the API key wins. `token` may be a callable returning the current token, called
for every request, so that a refreshing token source plugs in.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from lorebase_client.errors import LorebaseConfigError

ENV_URL = "LOREBASE_URL"
ENV_API_KEY = "LOREBASE_API_KEY"
ENV_TOKEN = "LOREBASE_TOKEN"
ENV_TIMEOUT = "LOREBASE_TIMEOUT"
ENV_API_PREFIX = "LOREBASE_API_PREFIX"

DEFAULT_API_PREFIX = "/api/v1"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 0.5
DEFAULT_BACKOFF_MAX_S = 30.0
DEFAULT_CACHE_ENTRIES = 256
RETRY_STATUSES = frozenset({429, 503})

SESSION_HEADER = "X-Lorebase-Session"
TOOL_HEADER = "X-Lorebase-Tool"
TRACE_HEADER = "X-Lorebase-Trace"
"""Default names of the agent trace headers (Keeper can be configured with others; pass those
through `headers=`)."""
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")

TokenSource = str | Callable[[], str]
TraceMode = Literal["on", "debug"]


def check_session_id(session_id: str) -> str:
    """The session id, or `LorebaseConfigError` when it is not 1-128 of `A-Za-z0-9._:-`."""
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        raise LorebaseConfigError("session_id must be 1-128 characters of A-Za-z0-9._:-")
    return session_id


@dataclass(frozen=True)
class RetryPolicy:
    """Retries on 429, 503 and connection failures, with exponential backoff.

    The wait before attempt `n` (from 0) is `min(backoff_max_s, backoff_base_s * 2**n)`, or the
    `Retry-After` of the response when there is one (capped at `backoff_max_s`). A random
    share up to `jitter` of the computed wait is subtracted so that clients do not retry in step.
    """

    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S
    jitter: float = 0.25
    statuses: frozenset[int] = RETRY_STATUSES


@dataclass(frozen=True)
class ClientConfig:
    """Resolved settings of a client."""

    base_url: str
    api_prefix: str = DEFAULT_API_PREFIX
    api_key: str | None = None
    token: TokenSource | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    auto_idempotency_keys: bool = False
    cache_entries: int | None = None
    """Entries of the ETag cache; `None` leaves caching off."""
    headers: Mapping[str, str] = field(default_factory=dict)
    session_id: str | None = None
    """Agent trace session sent as `X-Lorebase-Session` with every request."""
    tool: str | None = None
    """Tool name sent as `X-Lorebase-Tool`."""
    trace: TraceMode | None = None
    """`X-Lorebase-Trace` value: `on` opts requests in, `debug` adds query plans."""

    @property
    def root(self) -> str:
        """URL every request path is appended to: server root plus API prefix."""
        return self.base_url.rstrip("/") + self.api_prefix

    def bearer(self) -> str | None:
        """The credential to send, or `None` for anonymous requests."""
        if self.api_key:
            return self.api_key
        if callable(self.token):
            return self.token() or None
        return self.token or None


def resolve_config(
    base_url: str | None = None,
    *,
    api_key: str | None = None,
    token: TokenSource | None = None,
    timeout: float | None = None,
    api_prefix: str | None = None,
    retry: RetryPolicy | None = None,
    auto_idempotency_keys: bool = False,
    cache: bool | int = False,
    headers: Mapping[str, str] | None = None,
    session_id: str | None = None,
    tool: str | None = None,
    trace: TraceMode | None = None,
    env: Mapping[str, str] | None = None,
) -> ClientConfig:
    """Merge arguments with the environment (`env`, default `os.environ`).

    `cache` turns the ETag cache on (`True`: `DEFAULT_CACHE_ENTRIES` entries; an integer:
    that many).
    """
    environment = os.environ if env is None else env
    url = base_url or environment.get(ENV_URL)
    if not url:
        raise LorebaseConfigError(f"no server URL: pass base_url or set {ENV_URL}")
    timeout_s = timeout
    if timeout_s is None:
        raw = environment.get(ENV_TIMEOUT)
        try:
            timeout_s = float(raw) if raw else DEFAULT_TIMEOUT_S
        except ValueError:
            raise LorebaseConfigError(f"{ENV_TIMEOUT} must be a number of seconds") from None
    entries = None
    if cache is True:
        entries = DEFAULT_CACHE_ENTRIES
    elif cache:
        entries = int(cache)
    return ClientConfig(
        base_url=url,
        api_prefix=api_prefix or environment.get(ENV_API_PREFIX) or DEFAULT_API_PREFIX,
        api_key=api_key or environment.get(ENV_API_KEY) or None,
        token=token or environment.get(ENV_TOKEN) or None,
        timeout_s=timeout_s,
        retry=retry or RetryPolicy(),
        auto_idempotency_keys=auto_idempotency_keys,
        cache_entries=entries,
        headers=dict(headers or {}),
        session_id=check_session_id(session_id) if session_id else None,
        tool=tool or None,
        trace=trace,
    )
