"""A small HTTP client for Keeper's REST API (design 11.1), used by every Herald tool.

Herald reaches Lorebase only through Keeper (AC-20), so authorization, masking, audit and rate
limits are enforced in one place. Every request carries the credential of the calling agent;
this module never falls back to another one.

This module is internal and deliberately thin: it will be replaced by the typed
`lorebase_client` package once that is published, keeping the method names used by the tools.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from lorebase_herald.settings import KeeperConnection

PROBLEM_MEDIA_TYPES = ("application/problem+json", "application/json")
QueryValue = str | int | bool | Sequence[str] | None


@dataclass
class KeeperError(Exception):
    """A problem detail (RFC 9457) answered by Keeper."""

    status: int
    code: str
    title: str
    detail: str | None = None
    extensions: dict[str, Any] = field(default_factory=dict)
    retry_after: str | None = None

    def __str__(self) -> str:
        return f"{self.code} ({self.status}): {self.detail or self.title}"


class KeeperUnavailableError(Exception):
    """Keeper could not be reached or did not answer in time."""


_STANDARD_MEMBERS = frozenset(
    {"type", "title", "status", "detail", "instance", "code", "request_id", "trace_id"}
)


def _problem(response: httpx.Response) -> KeeperError:
    body: Any = None
    media_type = response.headers.get("content-type", "").split(";")[0].strip()
    if media_type in PROBLEM_MEDIA_TYPES:
        try:
            body = response.json()
        except ValueError:
            body = None
    if not isinstance(body, dict):
        return KeeperError(
            status=response.status_code,
            code=f"http-{response.status_code}",
            title=response.reason_phrase or "Error",
            retry_after=response.headers.get("retry-after"),
        )
    return KeeperError(
        status=int(body.get("status") or response.status_code),
        code=str(body.get("code") or f"http-{response.status_code}"),
        title=str(body.get("title") or response.reason_phrase or "Error"),
        detail=body.get("detail") if isinstance(body.get("detail"), str) else None,
        extensions={k: v for k, v in body.items() if k not in _STANDARD_MEMBERS},
        retry_after=response.headers.get("retry-after"),
    )


def route_missing(error: KeeperError) -> bool:
    """Whether Keeper answered that the route itself does not exist (an older Keeper), as
    opposed to a missing resource, whose problem always names it in `detail`."""
    return error.status in (404, 405) and error.detail is None


def segment(value: str) -> str:
    """One URL path segment; `/` in branch names and keys is encoded."""
    return quote(value, safe="")


class KeeperHttp:
    """Async JSON client of Keeper. One instance serves every agent: the credential is a
    per-call argument, never state of the client."""

    def __init__(
        self,
        connection: KeeperConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._connection = connection
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._connection.base_url + self._connection.api_prefix,
                timeout=self._connection.timeout_s,
                verify=self._connection.verify_tls,
                transport=self._transport,
                headers={
                    "User-Agent": self._connection.user_agent,
                    "Accept": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def domain_path(domain: str, *parts: str) -> str:
        """`/domains/{domain}/{part}/...` with every dynamic part encoded by the caller."""
        return "/".join(("/domains", segment(domain), *parts))

    async def get(
        self,
        path: str,
        *,
        credential: str | None,
        params: Mapping[str, QueryValue] | None = None,
    ) -> Any:
        return await self._send("GET", path, credential, params=params)

    async def post(
        self,
        path: str,
        *,
        credential: str | None,
        body: Mapping[str, Any],
    ) -> Any:
        return await self._send("POST", path, credential, body=body)

    async def post_text(
        self,
        path: str,
        *,
        credential: str | None,
        body: Mapping[str, Any],
    ) -> str:
        """The answer body as text, for endpoints that render documents (markdown or JSON)."""
        return str(await self._send("POST", path, credential, body=body, text=True))

    async def _send(
        self,
        method: str,
        path: str,
        credential: str | None,
        *,
        params: Mapping[str, QueryValue] | None = None,
        body: Mapping[str, Any] | None = None,
        text: bool = False,
    ) -> Any:
        headers = {} if credential is None else {"Authorization": authorization(credential)}
        query = {k: _query_value(v) for k, v in (params or {}).items() if v is not None}
        try:
            response = await self._http().request(
                method, path, params=query, json=body, headers=headers
            )
        except httpx.TimeoutException:
            raise KeeperUnavailableError("Keeper did not answer in time") from None
        except httpx.TransportError as error:
            raise KeeperUnavailableError(
                f"Keeper is unreachable ({type(error).__name__})"
            ) from None
        if response.status_code >= 400:
            raise _problem(response)
        return response.text if text else response.json()


def authorization(credential: str) -> str:
    """The Authorization header value: a forwarded `Bearer ...` value, or a bare API key."""
    scheme, _, rest = credential.partition(" ")
    if rest and scheme.lower() == "bearer":
        return credential
    return f"Bearer {credential}"


def _query_value(value: QueryValue) -> str | list[str]:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str | int):
        return str(value)
    return [str(v) for v in value or ()]
