"""Sinks of audit checkpoints: files, HTTP with the target checks, S3 signatures, settings."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from pydantic import ValidationError

from lorebase_scribe.checkpoints import FileSink, HttpSink, S3Sink, SinkError, sigv4_headers
from lorebase_scribe.settings import (
    AuditCheckpointSettings,
    CheckpointHttpSettings,
    CheckpointS3Settings,
)

PUBLIC = "93.184.216.34"


async def public(host: str) -> Sequence[str]:
    return [PUBLIC]


async def private(host: str) -> Sequence[str]:
    return ["10.0.0.7"]


def recording(status: int = 201) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, headers={"Location": "https://elsewhere.example/"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


def test_signature_version_4_matches_the_published_example() -> None:
    # "Example: PUT Object", Signature Version 4 header-based authentication (Amazon S3 API).
    headers = sigv4_headers(
        "PUT",
        httpx.URL("https://examplebucket.s3.amazonaws.com/test%24file.text"),
        region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        body=b"Welcome to Amazon S3.",
        now=datetime(2013, 5, 24, tzinfo=UTC),
        signed={
            "Date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
        },
    )
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=date;host;x-amz-content-sha256;x-amz-date;x-amz-storage-class, "
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
    )


def contents(path: str) -> bytes:
    return Path(path).read_bytes()


def names(root: Path) -> list[str]:
    return [p.name for p in root.rglob("*")]


async def test_file_sink_never_replaces_or_escapes(tmp_path: Path) -> None:
    sink = FileSink(str(tmp_path))
    location = await sink.put("lorebase-audit/actions/1.json", b"{}")
    assert contents(location) == b"{}"
    with pytest.raises(SinkError, match="exists"):
        await sink.put("lorebase-audit/actions/1.json", b"{changed}")
    assert contents(location) == b"{}"
    with pytest.raises(SinkError, match="leaves"):
        await sink.put("../outside.json", b"{}")
    assert names(tmp_path) == ["lorebase-audit", "actions", "1.json"]


async def test_http_sink_pins_the_checked_address() -> None:
    client, seen = recording()
    settings = CheckpointHttpSettings(
        url="https://audit.example.com/in/{name}",
        headers={"Authorization": "Bearer t0ken"},
    )
    async with client:
        url = await HttpSink(client, settings, resolve=public).put("p/actions/1.json", b"{}")
    assert url == "https://audit.example.com/in/p/actions/1.json"
    [request] = seen
    assert request.method == "PUT"
    assert request.url.host == PUBLIC
    assert request.headers["host"] == "audit.example.com"
    assert request.headers["authorization"] == "Bearer t0ken"
    assert request.extensions["sni_hostname"] == "audit.example.com"


@pytest.mark.parametrize(
    ("settings", "resolve", "status", "problem"),
    [
        ({"url": "https://audit.example.com/{name}"}, private, 201, "private address"),
        ({"url": "http://audit.example.com/{name}"}, public, 201, "scheme"),
        ({"url": "https://audit.example.com/{name}"}, public, 302, "answered 302"),
    ],
)
async def test_http_sink_refusals(
    settings: dict[str, Any], resolve: Any, status: int, problem: str
) -> None:
    client, seen = recording(status)
    async with client:
        sink = HttpSink(client, CheckpointHttpSettings(**settings), resolve=resolve)
        with pytest.raises(SinkError, match=problem):
            await sink.put("x.json", b"{}")
    assert len(seen) == (1 if status != 201 else 0)  # redirects are not followed


async def test_s3_sink_signs_a_put_to_the_bucket() -> None:
    client, seen = recording(200)
    settings = CheckpointS3Settings(
        endpoint="https://s3.eu-west-1.amazonaws.com",
        bucket="audit-anchors",
        region="eu-west-1",
        access_key_id="AKIDEXAMPLE",
        secret_access_key="secret-example",
    )
    async with client:
        sink = S3Sink(
            client, settings, resolve=public, clock=lambda: datetime(2026, 10, 25, tzinfo=UTC)
        )
        url = await sink.put("lorebase-audit/reads/20261025T000000000000Z.json", b"{}")
    assert url == (
        "https://s3.eu-west-1.amazonaws.com/audit-anchors/"
        "lorebase-audit/reads/20261025T000000000000Z.json"
    )
    [request] = seen
    assert request.headers["host"] == "s3.eu-west-1.amazonaws.com"
    assert request.headers["x-amz-date"] == "20261025T000000Z"
    assert request.headers["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261025/eu-west-1/s3/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, Signature="
    )


def test_enabled_checkpoints_need_a_key_and_a_sink() -> None:
    with pytest.raises(ValidationError, match="signing_key, directory"):
        AuditCheckpointSettings(enabled=True)
    with pytest.raises(ValidationError, match="not a PEM private key"):
        AuditCheckpointSettings(enabled=True, directory="/x", signing_key=NOT_A_KEY)
    pem = (
        Ed25519PrivateKey.generate()
        .private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        .decode()
    )
    with pytest.raises(ValidationError, match=r"s3\.endpoint"):
        AuditCheckpointSettings(enabled=True, signing_key=pem, sink="s3")
    with pytest.raises(ValidationError, match="relative"):
        AuditCheckpointSettings(prefix="../up/")
    assert AuditCheckpointSettings(enabled=True, signing_key=pem, directory="/x").enabled


NOT_A_KEY = "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"
