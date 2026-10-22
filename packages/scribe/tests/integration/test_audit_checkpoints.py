"""`audit_checkpoint` jobs publish signed chain heads that reveal a truncated chain."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lorebase_scribe.jobs import AUDIT_CHECKPOINT_JOB, JobContext, build_handlers
from lorebase_scribe.scheduler import Scheduler
from lorebase_scribe.settings import AuditCheckpointSettings, SchedulerSettings, ScribeSettings
from lorebase_store.auth import AuditRepository, AuditStream, NewAuditEntry
from lorebase_store.auth.checkpoints import compare, parse_document
from lorebase_store.jobs import JobRow
from lorebase_store.runtime import StoreRuntime
from lorebase_store.webhooks import WebhookSettings

pytestmark = pytest.mark.integration

AT = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


def files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.json"))


def read(path: Path) -> bytes:
    return path.read_bytes()


def job() -> JobRow:
    fields = {f.name: None for f in dataclasses.fields(JobRow)}
    return JobRow(**{**fields, "id": 1, "type": AUDIT_CHECKPOINT_JOB, "payload": {}})


async def test_checkpoints_are_published_and_detect_truncation(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    runtime: StoreRuntime,
    tmp_path: Path,
) -> None:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    settings = AuditCheckpointSettings(enabled=True, signing_key=pem, directory=str(tmp_path))
    async with sessions() as session, session.begin():
        for n in range(3):
            await AuditRepository(session).append(
                NewAuditEntry(actor="usr_1", action=f"a{n}", domain_id="acme")
            )
        await AuditRepository(session, AuditStream.READS).append_many(
            [NewAuditEntry(actor="usr_1", action="read", domain_id="acme")]
        )
    async with httpx.AsyncClient(trust_env=False) as http:
        context = JobContext(
            engine=engine,
            sessions=sessions,
            runtime=runtime,
            settings=ScribeSettings(audit_checkpoints=settings),
            webhooks=WebhookSettings(),
            http=http,
            worker_id="test-worker",
            clock=lambda: AT,
        )
        handler = build_handlers(context, [AUDIT_CHECKPOINT_JOB])[AUDIT_CHECKPOINT_JOB]
        result: Any = await handler(job())
    assert [(p["stream"], p["chains"]) for p in result["published"]] == [
        ("actions", 1),
        ("reads", 1),
    ]
    written = files(tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in written] == [
        "lorebase-audit/actions/20261025T090000000000Z.json",
        "lorebase-audit/reads/20261025T090000000000Z.json",
    ]
    checkpoint = parse_document(read(written[0]), key.public_key())

    async with engine.begin() as connection:  # the owner cuts the newest entry
        await connection.execute(
            text("DELETE FROM audit_log WHERE seq = (SELECT max(seq) FROM audit_log)")
        )
    async with sessions() as session, session.begin():
        [finding] = await compare(AuditRepository(session), [checkpoint])
    assert (finding.domain_id, finding.problem) == ("acme", "missing")

    async with httpx.AsyncClient(trust_env=False) as http:
        again = JobContext(
            engine=engine,
            sessions=sessions,
            runtime=runtime,
            settings=ScribeSettings(audit_checkpoints=settings),
            webhooks=WebhookSettings(),
            http=http,
            worker_id="test-worker",
            clock=lambda: AT,
        )
        handler = build_handlers(again, [AUDIT_CHECKPOINT_JOB])[AUDIT_CHECKPOINT_JOB]
        with pytest.raises(Exception, match="exists already"):  # never replaced
            await handler(job())


async def test_the_scheduler_queues_checkpoints_only_when_enabled(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession], runtime: StoreRuntime
) -> None:
    settings = SchedulerSettings(webhook_interval_s=0, embeddings_interval_s=0, gc_schedule=None)
    queued: list[list[str]] = []
    for interval in (0.0, 3600.0):
        scheduler = Scheduler(
            engine,
            sessions,
            settings=settings,
            jobs=runtime.settings.jobs,
            writes=runtime.settings.writes,
            clock=lambda: AT,
            checkpoint_interval_s=interval,
        )
        try:
            await scheduler.tick()
        finally:
            await scheduler.release()
        async with sessions() as session:
            types = (await session.execute(text("SELECT type FROM jobs ORDER BY id"))).scalars()
            queued.append(list(types))
    assert queued == [["audit_verify"], ["audit_verify", "audit_checkpoint"]]
