"""Document intake and extraction item review over HTTP, against Postgres.

The Domain holds the supply schema with Suppliers and Parts only, so the supplier review
proposes `supplied_by` Links between existing Records. Scribe's extraction runs in-process with
rules only (tier 0), as the `extraction` job would.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.revision import Origin
from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.pipeline import ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.db import create_engine
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import AUTHOR, DOMAIN, EXAMPLE, Clock, record_change, rows, supply_schema

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
REVIEW = EXAMPLE / "documents" / "supplier-review-2025-q4.md"


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema = supply_schema()
    await create_domain(session, schema.domain, author=AUTHOR, runtime=runtime, clock=clock)
    await commit(
        session,
        DOMAIN,
        "main",
        [],
        schema_changes=[SchemaUpsert(e) for e in schema.elements()],  # type: ignore[arg-type]
        author=AUTHOR,
        message="Load the supply schema",
        origin=Origin(type="import"),
        runtime=runtime,
        clock=clock,
    )
    changes = []
    for kind_name, file in (("Supplier", "supplier.csv"), ("Part", "part.csv")):
        kind = schema.kind_by_api_name(kind_name)
        assert kind is not None
        changes.extend(record_change(kind, row) for row in rows(file))
    await commit(
        session,
        DOMAIN,
        "main",
        changes,
        author=AUTHOR,
        message="Import suppliers and parts",
        origin=Origin(type="import"),
        runtime=runtime,
        clock=clock,
    )
    return Seeded(owner_url=owner_url, app_url=app_url, schema=schema)


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def upload(path: Path, name: str | None = None, **fields: str) -> dict[str, Any]:
    return {"files": {"file": (name or path.name, path.read_bytes())}, "data": fields}


async def test_upload_stores_documents_once_and_queues_extraction(
    seeded: Seeded, tmp_path: Path
) -> None:
    async with keeper(seeded.app_url) as http:
        first = await http.post(f"{BASE}/documents", **upload(REVIEW))
        assert first.status_code == 201, first.text
        body = first.json()
        document = body["document"]
        assert (document["filename"], document["media_type"]) == (REVIEW.name, "text/markdown")
        assert body["duplicate"] is False
        assert isinstance(body["job_id"], int)
        again = await http.post(f"{BASE}/documents", **upload(REVIEW, extract="false"))
        assert again.status_code == 200
        assert again.json()["duplicate"] is True
        assert again.json()["job_id"] is None
        assert again.json()["document"]["id"] == document["id"]

        listed = (await http.get(f"{BASE}/documents")).json()
        assert [d["id"] for d in listed["items"]] == [document["id"]]
        detail = (await http.get(f"{BASE}/documents/{document['id']}")).json()
        assert detail["sha256"] == document["sha256"]
        assert detail["extraction_jobs"] == []
        assert (await http.get(f"{BASE}/documents/doc_missing")).status_code == 404

        fake_pdf = tmp_path / "scan.pdf"
        fake_pdf.write_bytes(b"not a pdf")
        assert (await http.post(f"{BASE}/documents", **upload(fake_pdf))).status_code == 422
        exe = tmp_path / "tool.exe"
        exe.write_bytes(b"MZ")
        assert (await http.post(f"{BASE}/documents", **upload(exe))).status_code == 422
        no_urls = await http.post(f"{BASE}/documents", json={"url": "https://docs.test/a.md"})
        assert no_urls.status_code == 422
    async with keeper(seeded.app_url, {"documents": {"url_hosts": ["docs.example.org"]}}) as http:
        other = await http.post(f"{BASE}/documents", json={"url": "https://elsewhere.test/a.md"})
        assert other.status_code == 422
        assert "url_hosts" in other.text
    async with owner_session(seeded.owner_url) as session, session.begin():
        job = (
            await session.execute(
                text("SELECT type, payload FROM jobs WHERE id = :i"), {"i": body["job_id"]}
            )
        ).one()
        audited = (
            await session.execute(
                text(
                    "SELECT count(*) FROM audit_log WHERE domain_id = :d "
                    "AND action = 'document.upload'"
                ),
                {"d": DOMAIN},
            )
        ).scalar_one()
    assert job.type == "extraction"
    assert job.payload["documents"] == [document["id"]]
    assert job.payload["tiers"] is None
    assert audited == 2


async def extract(seeded: Seeded, document_id: str) -> str:
    """Run the extraction job in-process; returns the Proposal id."""
    engine = create_engine(DatabaseSettings(), url=seeded.owner_url, statement_timeout_ms=0)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        run = ExtractionRun(
            sessions,
            StoreRuntime.from_settings(StoreSettings()),
            ExtractionSettings(tiers=(0,), auto_accept_threshold=0.55),
            InferenceSettings(),
            principal="svc_scribe",
            clock=Clock(),
        )
        report = await run.run(DOMAIN, [document_id])
    finally:
        await engine.dispose()
    assert report.proposal_id is not None
    return report.proposal_id


async def test_items_are_listed_and_decided(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        uploaded = await http.post(f"{BASE}/documents", **upload(REVIEW, extract="false"))
        document_id = uploaded.json()["document"]["id"]
    proposal_id = await extract(seeded, document_id)
    items_url = f"{BASE}/proposals/{proposal_id}/items"
    async with keeper(seeded.app_url) as http:
        page = (await http.get(items_url, params={"type": "link", "kind": "supplied_by"})).json()
        links = page["items"]
        assert links
        assert all(i["relation"] == "supplied_by" for i in links)
        first = next(i for i in links if (i["from"], i["to"]) == ("P-10012", "S-1025"))
        assert first["decision"] == "accepted"
        assert first["evidence"]["quote"]
        pending = (await http.get(items_url, params={"status": "pending"})).json()["items"]
        assert pending
        assert all(i["decision"] == "pending" for i in pending)
        paged = (await http.get(items_url, params={"limit": 1})).json()
        assert len(paged["items"]) == 1
        assert paged["next_cursor"]

        decided = await http.post(
            f"{items_url}:decide",
            json={
                "decisions": [{"item": first["id"], "decision": "rejected"}],
                "where": {"type": ["link"], "kind": ["supplied_by"], "status": ["pending"]},
                "decision": "accepted",
            },
        )
        assert decided.status_code == 200, decided.text
        body = decided.json()
        decisions = {i["id"]: i["decision"] for i in body["items"]}
        assert decisions[first["id"]] == "rejected"
        assert body["materialized"]["changes"] >= 1
        assert body["checks"]["extraction"]["decisions"]["rejected"] == 1
        detail = (await http.get(f"{BASE}/proposals/{proposal_id}")).json()
        assert any(c["name"] == "extraction" for c in detail["checks"])

        unknown = await http.post(
            f"{items_url}:decide", json={"decisions": [{"item": "pit_x", "decision": "accepted"}]}
        )
        assert unknown.status_code == 422
        empty = await http.post(f"{items_url}:decide", json={})
        assert empty.status_code == 422
