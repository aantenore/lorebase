"""Guarded access to Source locations: local files under allowed roots, pinned HTTP requests.

- Local files: the path is resolved (symbolic links included) and must lie under a resolved
  root; the file is then opened from the root one component at a time without following
  symbolic links, so a link swapped in after the check is refused instead of followed, and it
  must be a regular file.
- HTTP: the URL needs an allowed scheme and host; the host must resolve to global unicast
  addresses only (unless private targets are allowed), checked as for webhooks, and requests
  connect to those checked addresses, never to a new lookup (one lookup per host and client).
  Redirects are never followed and response bodies are read in bounded steps.

Messages name the settings section (``scribe.sources.csv``...) and never the resolved value of
a connection, the host it resolves to or a header value.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import urlsplit

import httpx

from lorebase_core.model import Source
from lorebase_platform.secrets import SecretResolutionError, resolve_secret
from lorebase_scribe.sources.mapping import SourceDefinitionError
from lorebase_scribe.webhooks import pin
from lorebase_store.webhooks import WebhookSettings
from lorebase_store.webhooks.delivery import Resolver, check_target

READ_CHUNK = 1024 * 1024
"""Bytes read from a local file or a response per step; totals are bounded by settings."""


class SourceReadError(RuntimeError):
    """The Source could not be read (network, file system or database); a retry may work."""


def resolve_location(source: Source) -> str:
    """The value behind the Source's connection reference."""
    try:
        return resolve_secret(source.connection)
    except SecretResolutionError as error:
        raise SourceDefinitionError(f"connection: {error}") from None


# Local files ----------------------------------------------------------------------------------


def local_path(location: str, kind: str) -> Path:
    """The path of a local location: a plain path or a ``file:`` URL."""
    parts = urlsplit(location)
    if parts.scheme == "file":
        return Path(parts.path)
    if parts.scheme:
        raise SourceDefinitionError(f"connection: unsupported {kind} location scheme")
    return Path(location)


def _open_beneath(root: Path, relative: PurePath) -> int:
    """A descriptor of `root / relative`, opened without following any symbolic link.

    Each directory below `root` is opened relative to its parent with `O_NOFOLLOW`, and so is
    the file itself (non-blocking, so a FIFO cannot hold the worker): a component replaced by
    a symbolic link after the path was checked fails with ELOOP or ENOTDIR.
    """
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(
            relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    finally:
        os.close(directory)


def _beneath_root(path: Path, roots: Sequence[str], section: str) -> tuple[Path, PurePath]:
    """The allowed root `path` resolves under, and the resolved path relative to it.

    Symbolic links are resolved here, so a link inside a root that points inside a root keeps
    working; the file is then opened along the resolved path, which has no links, without
    following any (`_open_beneath`)."""
    try:
        resolved = path.resolve()
        resolved_roots = [Path(root).resolve() for root in roots]
    except (OSError, RuntimeError):
        raise SourceReadError(f"{path.name}: the path cannot be resolved") from None
    for root in resolved_roots:
        if resolved != root and resolved.is_relative_to(root):
            return root, resolved.relative_to(root)
    raise SourceDefinitionError(f"connection: the path is outside {section}.allowed_roots")


def open_local(path: Path, roots: Sequence[str], section: str) -> int:
    """A read-only descriptor of the regular file `path` under one of `roots`."""
    root, relative = _beneath_root(path, roots, section)
    try:
        descriptor = _open_beneath(root, relative)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SourceDefinitionError(
                "connection: the path meets a symbolic link or a non-directory when opened "
                f"under {section}.allowed_roots"
            ) from None
        raise SourceReadError(f"{path.name}: {error.strerror}") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SourceDefinitionError(f"{path.name}: not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_bounded(descriptor: int, name: str, limit: int, setting: str) -> bytes:
    """At most `limit` bytes of the file behind `descriptor` (`setting` names the limit)."""
    size = os.fstat(descriptor).st_size
    if size > limit:
        raise SourceDefinitionError(
            f"{name}: {size} bytes, more than the {limit} allowed by {setting}"
        )
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(descriptor, min(READ_CHUNK, limit + 1 - size)):
        size += len(chunk)
        if size > limit:  # the file grew after fstat
            raise SourceDefinitionError(f"{name}: more than the {limit} bytes allowed by {setting}")
        chunks.append(chunk)
    return b"".join(chunks)


# HTTP -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpRules:
    """What a section of settings allows of URLs and responses."""

    section: str
    allowed_schemes: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    allow_private_targets: bool
    max_bytes: int


@dataclass(frozen=True)
class Fetched:
    """A response: its status, headers and, for a 200, its body."""

    status: int
    headers: httpx.Headers
    body: bytes = b""


@dataclass
class PinnedClient:
    """GET requests to checked URLs over an `httpx.AsyncClient` built with `trust_env=False`
    and `follow_redirects=False` (owned by the caller)."""

    client: httpx.AsyncClient
    rules: HttpRules
    resolve: Resolver
    _addresses: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def check(self, url: httpx.URL, what: str) -> None:
        """Refuse a URL whose scheme or host is not allowed (`what` starts the message)."""
        if url.scheme not in self.rules.allowed_schemes:
            raise SourceDefinitionError(
                f"{what}: the URL scheme is not in {self.rules.section}.allowed_schemes"
            )
        if url.host not in self.rules.allowed_hosts:
            raise SourceDefinitionError(
                f"{what}: the URL host is not in {self.rules.section}.allowed_hosts"
            )

    async def addresses(self, url: httpx.URL, what: str) -> tuple[str, ...]:
        """The checked addresses of the URL host, looked up once per client.

        Empty when private targets are allowed: the client then resolves the host itself."""
        if url.host in self._addresses:
            return self._addresses[url.host]
        checks = WebhookSettings(
            allowed_schemes=self.rules.allowed_schemes,
            allow_private_targets=self.rules.allow_private_targets,
        )
        target = await check_target(str(url), checks, self.resolve)
        if target.problem is not None:
            if target.problem.startswith("cannot resolve"):
                raise SourceReadError(f"{what}: the URL host cannot be resolved")
            # `check_target` problems never contain the host name or the URL.
            raise SourceDefinitionError(
                f"{what}: the URL host {target.problem}, refused unless "
                f"{self.rules.section}.allow_private_targets"
            )
        self._addresses[url.host] = target.addresses
        return target.addresses

    async def get(self, url: httpx.URL, headers: Mapping[str, str], what: str) -> Fetched:
        """GET `url` from its checked addresses; the body is read only for a 200."""
        self.check(url, what)
        addresses = await self.addresses(url, what)
        if not addresses:
            return await self._get(url, dict(headers), {})
        failure: httpx.TransportError | None = None
        for address in addresses:
            pinned, host, extensions = pin(url, address)
            try:
                return await self._get(pinned, {**headers, **host}, extensions)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                failure = error  # the next checked address, never a new lookup
        assert failure is not None
        raise failure

    async def _get(
        self, target: httpx.URL, headers: dict[str, str], extensions: dict[str, Any]
    ) -> Fetched:
        limit = self.rules.max_bytes
        chunks: list[bytes] = []
        size = 0
        async with self.client.stream(
            "GET", target, headers=headers, extensions=extensions
        ) as response:
            if response.status_code != 200:
                return Fetched(response.status_code, response.headers)
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > limit:
                    raise SourceDefinitionError(
                        f"download larger than the {limit} bytes allowed by "
                        f"{self.rules.section}.max_bytes"
                    )
                chunks.append(chunk)
            return Fetched(200, response.headers, b"".join(chunks))
