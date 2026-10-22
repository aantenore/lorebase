"""Schema suggestions over HTTP, against Postgres: the four example documents are extracted
in-process with the rules (as the `extraction` job would), then suggestions are listed,
rejected and accepted into a schema change Proposal."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker

from lorebase_inference import InferenceSettings
from lorebase_scribe.extraction.pipeline import ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.db import create_engine
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, EXAMPLE, Clock
from .test_documents_api import seed, upload

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
SUGGESTIONS = f"{BASE}/schema/suggestions"
DOCUMENTS = EXAMPLE / "documents"


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


async def extract(seeded: Seeded, document_ids: list[str]) -> None:
    engine = create_engine(DatabaseSettings(), url=seeded.owner_url, statement_timeout_ms=0)
    try:
        run = ExtractionRun(
            async_sessionmaker(engine, expire_on_commit=False),
            StoreRuntime.from_settings(StoreSettings()),
            ExtractionSettings(tiers=(0,)),
            InferenceSettings(),
            principal="svc_scribe",
            clock=Clock(),
        )
        for document_id in document_ids:
            await run.run(DOMAIN, [document_id])
    finally:
        await engine.dispose()


def named(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {f"{i['type']}:{i['name']}": i for i in items}


async def test_suggestions_are_listed_rejected_and_accepted(seeded: Seeded) -> None:
    async with keeper(seeded.app_url) as http:
        ids = []
        for path in sorted(DOCUMENTS.glob("*.md")):
            uploaded = await http.post(f"{BASE}/documents", **upload(path, extract="false"))
            ids.append(uploaded.json()["document"]["id"])
    await extract(seeded, ids)

    async with keeper(seeded.app_url) as http:
        listed = await http.get(SUGGESTIONS)
        assert listed.status_code == 200, listed.text
        supported = named(listed.json()["items"])
        # min_support 2: one quality notice is not enough for a Kind.
        assert "kind:QualityNotice" not in supported
        ends = supported["attribute:ends"]
        assert (ends["kind"], ends["support"], ends["documents"]) == ("Supplier", 3, 1)
        assert ends["type_guess"]["type"] == "date"
        assert ends["examples"][0]["quote"].startswith("| ")
        sole = supported["relation:sole_source_parts"]
        assert (sole["kind"], sole["target"]) == ("Supplier", "Part")
        assert all(i["status"] == "pending" for i in supported.values())

        everything = named((await http.get(SUGGESTIONS, params={"all": "true"})).json()["items"])
        notice = everything["kind:QualityNotice"]
        assert notice["samples"] == ["QN-2025-114"]
        part = everything["relation:part"]
        assert (part["kind"], part["target"]) == ("+QualityNotice", "Part")
        buyer = everything["attribute:buyer"]
        assert buyer["kind"] == "Contract"
        kinds_only = (await http.get(SUGGESTIONS, params={"all": "true", "type": "kind"})).json()
        assert {i["type"] for i in kinds_only["items"]} == {"kind"}
        first = (await http.get(SUGGESTIONS, params={"all": "true", "limit": 1})).json()
        assert len(first["items"]) == 1
        assert first["next_cursor"]

        rejected = await http.post(
            f"{SUGGESTIONS}:decide",
            json={"decisions": [{"suggestion": ends["id"], "decision": "rejected"}]},
        )
        assert rejected.status_code == 200, rejected.text
        body = rejected.json()
        assert body["proposal"] is None
        assert body["suggestions"][0]["status"] == "rejected"
        assert body["suggestions"][0]["decided_by"]

        # A relation of a Kind nobody accepted cannot be applied: nothing is decided.
        orphan = await http.post(
            f"{SUGGESTIONS}:decide",
            json={"decisions": [{"suggestion": part["id"], "decision": "accepted"}]},
        )
        assert orphan.status_code == 422, orphan.text
        assert "QualityNotice" in orphan.text

        accepted = await http.post(
            f"{SUGGESTIONS}:decide",
            json={
                "decisions": [
                    {"suggestion": notice["id"], "decision": "accepted"},
                    {"suggestion": part["id"], "decision": "accepted"},
                    {"suggestion": buyer["id"], "decision": "accepted", "name": "buyer_entity"},
                ]
            },
        )
        assert accepted.status_code == 200, accepted.text
        result = accepted.json()
        proposal = result["proposal"]
        assert proposal["branch"] == f"schema-suggestions/{notice['id']}"
        assert {(c["op"], c["api_name"]) for c in proposal["changes"]} == {
            ("create", "QualityNotice"),
            ("create", "part"),
            ("update", "Contract"),
        }
        assert {s["status"] for s in result["suggestions"]} == {"accepted"}
        assert {s["proposal_id"] for s in result["suggestions"]} == {proposal["id"]}

        detail = (await http.get(f"{BASE}/proposals/{proposal['id']}")).json()
        assert (detail["source_ref"], detail["target_ref"], detail["status"]) == (
            proposal["branch"],
            "main",
            "open",
        )
        branch_yaml = await http.get(
            f"{BASE}/schema", params={"branch": proposal["branch"], "format": "yaml"}
        )
        on_branch = yaml.safe_load(branch_yaml.text)
        assert on_branch["kinds"]["QualityNotice"]["status"] == "draft"
        assert on_branch["relations"]["part"]["to_kind"] == "Part"
        assert "buyer_entity" in on_branch["kinds"]["Contract"]["attributes"]
        main = yaml.safe_load((await http.get(f"{BASE}/schema", params={"format": "yaml"})).text)
        assert "QualityNotice" not in main["kinds"]

        twice = await http.post(
            f"{SUGGESTIONS}:decide",
            json={"decisions": [{"suggestion": notice["id"], "decision": "rejected"}]},
        )
        assert twice.status_code == 409
        missing = await http.post(
            f"{SUGGESTIONS}:decide",
            json={"decisions": [{"suggestion": "ssg_missing", "decision": "rejected"}]},
        )
        assert missing.status_code == 404
        reset = await http.post(
            f"{SUGGESTIONS}:decide",
            json={"decisions": [{"suggestion": ends["id"], "decision": "pending"}]},
        )
        assert reset.json()["suggestions"][0]["status"] == "pending"
        assert reset.json()["suggestions"][0]["decided_by"] is None
