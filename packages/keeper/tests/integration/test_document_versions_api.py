"""Document versions, retirement and purge over HTTP, against Postgres (design 9.2 "Document
versions and lifecycle")."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.pipeline import DocumentRetiredError, ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.db import create_engine
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import DOMAIN, Clock
from .test_documents_api import BASE, REVIEW, extract, seed, upload

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def keyed(path: Path, key: str, **fields: str) -> dict[str, Any]:
    return upload(path, source_key=key, **fields)


async def scalar(seeded: Seeded, sql: str, **params: Any) -> Any:
    async with owner_session(seeded.owner_url) as session, session.begin():
        return (await session.execute(text(sql), params)).scalar_one()


async def test_keyless_uploads_keep_content_addressing(seeded: Seeded, tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text("# Note\n\nKeyless.\n")
    async with keeper(seeded.app_url) as http:
        first = (await http.post(f"{BASE}/documents", **upload(note, extract="false"))).json()
        again = await http.post(f"{BASE}/documents", **upload(note, extract="false"))
    assert again.status_code == 200
    document = first["document"]
    assert (document["source_key"], document["version"], document["status"]) == (
        None,
        1,
        "active",
    )
    assert again.json()["document"]["id"] == document["id"]
    assert first["check_job_id"] is None


async def test_keyed_uploads_are_versions(seeded: Seeded, tmp_path: Path) -> None:
    v1, v2 = tmp_path / "a.md", tmp_path / "b.md"
    v1.write_text("# Terms\n\nPayment in 30 days.\n")
    v2.write_text("# Terms\n\nPayment in 60 days.\n")
    key = "  contracts/terms.md "
    async with keeper(seeded.app_url) as http:
        first = await http.post(f"{BASE}/documents", **keyed(v1, key, extract="false"))
        assert first.status_code == 201, first.text
        one = first.json()["document"]
        assert (one["source_key"], one["version"]) == ("contracts/terms.md", 1)
        same = await http.post(f"{BASE}/documents", **keyed(v1, key, extract="false"))
        assert same.status_code == 200
        assert same.json()["duplicate"] is True
        assert same.json()["document"]["id"] == one["id"]
        second = await http.post(f"{BASE}/documents", **keyed(v2, key, extract="false"))
        assert second.status_code == 201
        two = second.json()["document"]
        assert (two["version"], two["replaces"], two["status"]) == (2, one["id"], "active")
        check = second.json()["check_job_id"]
        assert isinstance(check, int)
        # The same bytes under another key are another document sharing the blob.
        other = await http.post(f"{BASE}/documents", **keyed(v1, "elsewhere", extract="false"))
        assert other.status_code == 201
        assert other.json()["document"]["sha256"] == one["sha256"]
        # An upstream revert is a new version too.
        revert = await http.post(f"{BASE}/documents", **keyed(v1, key, extract="false"))
        assert revert.status_code == 201
        assert revert.json()["document"]["version"] == 3

        versions = (await http.get(f"{BASE}/documents/{one['id']}/versions")).json()
        assert [d["version"] for d in versions["items"]] == [3, 2, 1]
        assert [d["status"] for d in versions["items"]] == ["active", "superseded", "superseded"]
        paged = await http.get(f"{BASE}/documents/{two['id']}/versions", params={"limit": 2})
        rest = await http.get(
            f"{BASE}/documents/{two['id']}/versions",
            params={"cursor": paged.json()["next_cursor"]},
        )
        assert [d["version"] for d in rest.json()["items"]] == [1]
        listed = await http.get(f"{BASE}/documents", params={"source_key": "contracts/terms.md"})
        assert {d["version"] for d in listed.json()["items"]} == {1, 2, 3}
        long = await http.post(f"{BASE}/documents", **keyed(v1, "x" * 1025))
        assert long.status_code == 422
        empty = await http.post(f"{BASE}/documents", **keyed(v1, "   "))
        assert empty.status_code == 422
    job = await scalar(seeded, "SELECT payload FROM jobs WHERE id = :i", i=check)
    assert job == {"document": two["id"], "mode": "version"}
    audited = await scalar(
        seeded,
        "SELECT details FROM audit_log WHERE action = 'document.version' AND target = :t",
        t=two["id"],
    )
    assert audited["replaces"] == one["id"]
    assert audited["version"] == 2
    assert audited["replaced_sha256"] == one["sha256"]


async def test_retire_and_purge(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        uploaded = await http.post(f"{BASE}/documents", **upload(REVIEW, extract="false"))
        document_id = uploaded.json()["document"]["id"]
    proposal_id = await extract(seeded, document_id)
    url = f"{BASE}/documents/{document_id}"
    async with keeper(seeded.app_url) as http:
        detail = (await http.get(url)).json()
        assert (detail["can_retire"], detail["can_purge"]) == (True, True)
        assert detail["open_stale_candidates"] == 0
        retired = await http.post(f"{url}/retirement", json={"reason": "Withdrawn upstream"})
        assert retired.status_code == 200, retired.text
        assert retired.json()["document"]["status"] == "retired"
        job = retired.json()["job_id"]
        assert isinstance(job, int)
        again = await http.post(f"{url}/retirement", json={"reason": "again"})
        assert again.json()["job_id"] is None
        refused = await http.post(f"{BASE}/documents", **upload(REVIEW, extract="true"))
        assert refused.status_code == 409
        assert refused.json()["code"] == "document-retired"
        quiet = await http.post(f"{BASE}/documents", **upload(REVIEW))
        assert quiet.status_code == 200
        assert quiet.json()["job_id"] is None
        # The text is still readable until the purge.
        assert (await http.get(f"{url}/text")).status_code == 200

        wrong = await http.post(f"{url}/purge", json={"reason": "GDPR", "confirm": "doc_other"})
        assert wrong.status_code == 422
        purged = await http.post(f"{url}/purge", json={"reason": "GDPR", "confirm": document_id})
        assert purged.status_code == 200, purged.text
        body = purged.json()
        assert body["document"]["status"] == "purged"
        assert body["job_id"] is None  # retired before
        assert body["blob_deleted"] is True
        assert body["chunks"] > 0
        assert body["items"] > 0
        assert (await http.get(f"{url}/text")).status_code == 404
        items = (await http.get(f"{BASE}/proposals/{proposal_id}/items")).json()["items"]
        quotes = {i["evidence"]["quote"] for i in items if i.get("evidence")}
        assert quotes == {"[erased]"}
        twice = await http.post(f"{url}/purge", json={"reason": "GDPR", "confirm": document_id})
        assert twice.json()["items"] == 0
    provenance = await scalar(
        seeded,
        "SELECT count(*) FROM provenance WHERE origin_ref->>'document_id' = :d",
        d=document_id,
    )
    assert provenance > 0
    audited = await scalar(
        seeded,
        "SELECT count(*) FROM audit_log WHERE target = :t AND action IN "
        "('document.retire', 'document.purge')",
        t=document_id,
    )
    assert audited == 4
    blobs = await scalar(
        seeded,
        "SELECT count(*) FROM document_blobs WHERE domain_id = :d AND sha256 = "
        "(SELECT sha256 FROM documents WHERE id = :i)",
        d=DOMAIN,
        i=document_id,
    )
    assert blobs == 0


async def test_purge_stops_a_running_extraction(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purge landing while a job runs (text and chunks stored, items not yet) leaves no text,
    chunks or items behind: the job stops at its next write and is cancelled."""
    note = REVIEW.read_text(encoding="utf-8") + "\n\nPurged while extracting.\n"
    async with keeper(seeded.app_url) as http:
        uploaded = await http.post(
            f"{BASE}/documents",
            files={"file": ("purge-race.md", note.encode())},
            data={"extract": "false"},
        )
        document_id = uploaded.json()["document"]["id"]
    purged: dict[str, Any] = {}
    original = ExtractionRun._current

    async def purge_then_continue(self: ExtractionRun, *args: Any, **kwargs: Any) -> Any:
        assert await scalar(
            seeded, "SELECT count(*) FROM document_chunks WHERE document_id = :d", d=document_id
        )
        async with keeper(seeded.app_url) as http:
            answer = await http.post(
                f"{BASE}/documents/{document_id}/purge",
                json={"reason": "GDPR", "confirm": document_id},
            )
        assert answer.status_code == 200, answer.text
        purged.update(answer.json())
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ExtractionRun, "_current", purge_then_continue)
    engine = create_engine(DatabaseSettings(), url=seeded.owner_url, statement_timeout_ms=0)
    try:
        run = ExtractionRun(
            async_sessionmaker(engine, expire_on_commit=False),
            StoreRuntime.from_settings(StoreSettings()),
            ExtractionSettings(tiers=(0,), auto_accept_threshold=0.55),
            InferenceSettings(),
            principal="svc_scribe",
            clock=Clock(),
        )
        with pytest.raises(DocumentRetiredError):
            await run.run(DOMAIN, [document_id])
    finally:
        await engine.dispose()
    assert purged["chunks"] > 0
    assert purged["document"]["status"] == "purged"
    chunks = await scalar(
        seeded, "SELECT count(*) FROM document_chunks WHERE document_id = :d", d=document_id
    )
    text_hash = await scalar(seeded, "SELECT text_hash FROM documents WHERE id = :d", d=document_id)
    items = await scalar(
        seeded,
        "SELECT count(*) FROM proposal_items i JOIN extraction_jobs j "
        "ON j.domain_id = i.domain_id AND j.id = i.job_id WHERE j.documents ? :d",
        d=document_id,
    )
    status = await scalar(
        seeded, "SELECT status::text FROM extraction_jobs WHERE documents ? :d", d=document_id
    )
    audited = await scalar(
        seeded,
        "SELECT details->>'jobs_cancelled' FROM audit_log WHERE action = 'document.purge' "
        "AND target = :t",
        t=document_id,
    )
    assert (chunks, text_hash, items, status, audited) == (0, None, 0, "cancelled", "1")
