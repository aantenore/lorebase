"""Publication of signed audit checkpoints outside the database (`audit_checkpoint` jobs).

Each job reads the head of every chain of the configured streams, signs one checkpoint per
stream (`lorebase_store.auth.checkpoints`) and writes it to the sink of
`scribe.audit_checkpoints`:

- `file`: a new file under `directory` (never replacing one), written to a temporary name,
  synced and linked into place;
- `http`: a PUT or POST of the document to `http.url`;
- `s3`: a PUT to an S3-compatible bucket, signed with Signature Version 4.

HTTP targets get the checks of webhook deliveries: allowed schemes, every resolved address
global unicast unless `allow_private_targets`, connections pinned to the checked addresses
with the host name kept for TLS, no redirects, no proxy from the environment (the shared
client is built with `trust_env=False`), and the exchange bounded as a whole.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_scribe.settings import (
    AuditCheckpointSettings,
    CheckpointHttpSettings,
    CheckpointS3Settings,
)
from lorebase_scribe.webhooks import pin
from lorebase_store.auth import AuditRepository, AuditStream
from lorebase_store.auth.checkpoints import Checkpoint, load_private_key, signed_document
from lorebase_store.webhooks.delivery import Resolver, TargetPolicy, check_target, resolve_host

MEDIA_TYPE = "application/json"


class CheckpointSink(Protocol):
    """Stores one checkpoint document under its name; returns where it went."""

    async def put(self, name: str, document: bytes) -> str: ...


class SinkError(RuntimeError):
    """A checkpoint could not be stored."""


class FileSink:
    """Checkpoints as files under a directory; an existing file is never replaced."""

    def __init__(self, directory: str) -> None:
        self._root = Path(directory)

    def _write(self, name: str, document: bytes) -> str:
        root = self._root.resolve()
        target = (root / name).resolve()
        if root not in target.parents:
            raise SinkError(f"checkpoint name {name!r} leaves the directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, target)  # fails rather than replacing an existing checkpoint
        except FileExistsError:
            raise SinkError(f"checkpoint {name} exists already") from None
        finally:
            temporary.unlink(missing_ok=True)
        return str(target)

    async def put(self, name: str, document: bytes) -> str:
        return await asyncio.to_thread(self._write, name, document)


async def send_checked(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    body: bytes,
    headers: Mapping[str, str],
    policy: TargetPolicy,
    timeout_s: float,
    resolve: Resolver = resolve_host,
) -> int:
    """Send to `url` after the target checks, connecting only to the checked addresses;
    the status code. `SinkError` when the target is refused or answers other than 2xx."""
    target = await check_target(url, policy, resolve)
    if target.problem is not None:
        raise SinkError(f"checkpoint target refused: {target.problem}")
    parsed = httpx.URL(url)
    attempts = [pin(parsed, address) for address in target.addresses] or [(parsed, {}, {})]
    failure: Exception | None = None
    async with asyncio.timeout(timeout_s):
        for pinned, host, extensions in attempts:
            try:
                # The response body is never read: a target that streams cannot hold the job.
                async with client.stream(
                    method,
                    pinned,
                    content=body,
                    headers={**headers, **host},
                    timeout=httpx.Timeout(timeout_s),
                    follow_redirects=False,
                    extensions=extensions,
                ) as response:
                    status = response.status_code
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                failure = error  # the next checked address, never a new lookup
                continue
            if not 200 <= status < 300:
                raise SinkError(f"checkpoint target answered {status}")
            return status
    raise SinkError(f"cannot connect to the checkpoint target: {failure}")


class HttpSink:
    """Checkpoints sent to an HTTP endpoint (`http.url`, `{name}` replaced)."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: CheckpointHttpSettings,
        *,
        resolve: Resolver = resolve_host,
    ) -> None:
        if settings.url is None:
            raise ValueError("the http sink needs http.url")
        self._client = client
        self._settings = settings
        self._url = settings.url
        self._resolve = resolve

    async def put(self, name: str, document: bytes) -> str:
        url = self._url.replace("{name}", quote(name, safe="/-_.~"))
        headers = {k: v.get_secret_value() for k, v in self._settings.headers.items()}
        await send_checked(
            self._client,
            self._settings.method,
            url,
            body=document,
            headers={"Content-Type": MEDIA_TYPE, **headers},
            policy=self._settings,
            timeout_s=self._settings.timeout_s,
            resolve=self._resolve,
        )
        return url


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(
    method: str,
    url: httpx.URL,
    *,
    region: str,
    access_key: str,
    secret_key: str,
    body: bytes,
    now: datetime,
    session_token: str | None = None,
    signed: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Headers of an S3 request signed with AWS Signature Version 4 (header form, single
    chunk, no query string). `signed` are further headers to sign and send. The path of
    `url` must already be percent-encoded as S3 expects (each segment once)."""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    headers = {
        "host": url.netloc.decode("ascii"),
        "x-amz-content-sha256": hashlib.sha256(body).hexdigest(),
        "x-amz-date": stamp,
        **{k.lower(): v.strip() for k, v in (signed or {}).items()},
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    names = sorted(headers)
    canonical = "\n".join(
        [
            method,
            url.raw_path.decode("ascii").split("?", 1)[0] or "/",
            "",
            "".join(f"{name}:{headers[name]}\n" for name in names),
            ";".join(names),
            headers["x-amz-content-sha256"],
        ]
    )
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), day)
    for part in (region, "s3", "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    del headers["host"]  # sent by the client (kept on pinned connections by `pin`)
    return headers


class S3Sink:
    """Checkpoints PUT into an S3-compatible bucket."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: CheckpointS3Settings,
        *,
        resolve: Resolver = resolve_host,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if settings.endpoint is None or settings.bucket is None:
            raise ValueError("the s3 sink needs s3.endpoint and s3.bucket")
        if settings.access_key_id is None or settings.secret_access_key is None:
            raise ValueError("the s3 sink needs s3.access_key_id and s3.secret_access_key")
        self._client = client
        self._settings = settings
        self._endpoint = httpx.URL(settings.endpoint)
        self._bucket = settings.bucket
        self._access_key = settings.access_key_id
        self._secret_key = settings.secret_access_key
        self._resolve = resolve
        self._clock = clock

    def url(self, name: str) -> httpx.URL:
        """URL of an object."""
        key = "/".join(quote(part, safe="-_.~") for part in name.split("/"))
        base = self._endpoint.raw_path.decode("ascii").rstrip("/")
        if self._settings.path_style:
            return self._endpoint.copy_with(raw_path=f"{base}/{self._bucket}/{key}".encode())
        host = f"{self._bucket}.{self._endpoint.host}"
        return self._endpoint.copy_with(host=host, raw_path=f"{base}/{key}".encode())

    async def put(self, name: str, document: bytes) -> str:
        settings = self._settings
        url = self.url(name)
        headers = sigv4_headers(
            "PUT",
            url,
            region=settings.region,
            access_key=self._access_key.get_secret_value(),
            secret_key=self._secret_key.get_secret_value(),
            session_token=(
                settings.session_token.get_secret_value() if settings.session_token else None
            ),
            body=document,
            now=self._clock(),
            signed={"content-type": MEDIA_TYPE},
        )
        await send_checked(
            self._client,
            "PUT",
            str(url),
            body=document,
            headers=headers,
            policy=settings,
            timeout_s=settings.timeout_s,
            resolve=self._resolve,
        )
        return str(url)


def build_sink(settings: AuditCheckpointSettings, client: httpx.AsyncClient) -> CheckpointSink:
    """The sink the settings name."""
    if settings.sink == "http":
        return HttpSink(client, settings.http)
    if settings.sink == "s3":
        return S3Sink(client, settings.s3)
    if settings.directory is None:
        raise ValueError("the file sink needs a directory")
    return FileSink(settings.directory)


@dataclass(frozen=True)
class Published:
    """One checkpoint written."""

    stream: str
    chains: int
    location: str

    def as_dict(self) -> dict[str, Any]:
        return {"stream": self.stream, "chains": self.chains, "location": self.location}


async def publish_checkpoints(
    sessions: async_sessionmaker[AsyncSession],
    settings: AuditCheckpointSettings,
    sink: CheckpointSink,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[Published]:
    """Sign and store one checkpoint per configured stream."""
    if settings.signing_key is None:
        raise ValueError("scribe.audit_checkpoints.signing_key is not set")
    key = load_private_key(settings.signing_key.get_secret_value())
    published = []
    for name in settings.streams:
        stream = AuditStream(name)
        async with sessions() as session, session.begin():
            heads = await AuditRepository(session, stream).heads()
        checkpoint = Checkpoint(stream, clock(), settings.key_id, tuple(heads))
        location = await sink.put(
            checkpoint.name(settings.prefix), signed_document(checkpoint, key)
        )
        published.append(Published(stream.value, len(heads), location))
    return published
