"""Reviewer edits that set a value of a vault Attribute need `pii.import` (design 13.5): the
extraction commit checks vault values against erased ones, so the outcome would tell any
reviewer whether a guessed value was erased."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Attribute, Sensitivity
from lorebase_core.revision import Origin
from lorebase_core.types import TypeSpec
from lorebase_core.validate import RecordUpsert
from lorebase_inference import InferenceSettings
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_scribe.extraction.pipeline import ExtractionRun
from lorebase_scribe.extraction.settings import ExtractionSettings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.commit import SchemaUpsert, commit, create_domain
from lorebase_store.db import create_engine
from lorebase_store.extraction import record_element
from lorebase_store.models.enums import PrincipalType
from lorebase_store.repositories import scope_to_domain
from lorebase_store.repositories.proposal_items import (
    VALUE_ITEM,
    ItemRow,
    ProposalItemRepository,
)
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings
from lorebase_store.vault import VaultSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import AUTHOR, DOMAIN, EXAMPLE, Clock, record_change, rows, supply_schema

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
PEPPER = "vault-edits-pepper-0123456789-0123456789"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
VAULT = {"token_key": "t" * 40, "encryption_key": "e" * 40, "key_id": "k1"}
EMAIL = "att_01KEZF7C00VAVTEMA000000001"
REVIEW = EXAMPLE / "documents" / "supplier-review-2025-q4.md"
KEY = "S-1025"


def _runtime() -> StoreRuntime:
    return StoreRuntime.from_settings(StoreSettings(), vault=VaultSettings(**VAULT))


async def _key(store: SqlAuthStore, name: str, role: str) -> str:
    async with store.unit() as unit:
        principal = await unit.principals.add(PrincipalType.SERVICE, name)
        await unit.grants.add(
            domain_id=DOMAIN, subject=principal.id, created_by="usr_seed", role=role
        )
        generated = CODEC.generate()
        await unit.api_keys.add(
            principal_id=principal.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=[f"domain:{DOMAIN}"],
        )
    return generated.token


async def seed(
    session: AsyncSession, _runtime_: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    runtime = _runtime()
    schema = supply_schema()
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    email = Attribute(
        id=EMAIL,
        api_name="email",
        description="Contact email.",
        type=TypeSpec.of("string"),
        sensitivity=(Sensitivity.PII,),
        vault=True,
    )
    supplier = supplier.model_copy(update={"attributes": {**supplier.attributes, EMAIL: email}})
    schema = schema.model_copy(update={"kinds": {**schema.kinds, supplier.id: supplier}})
    await create_domain(session, schema.domain, author=AUTHOR, runtime=runtime, clock=clock)
    await commit(
        session,
        DOMAIN,
        "main",
        [],
        schema_changes=[SchemaUpsert(e) for e in schema.elements()],  # type: ignore[arg-type]
        author=AUTHOR,
        message="Load the supply schema",
        origin=Origin(type="test"),
        runtime=runtime,
        clock=clock,
    )
    # Records only, no Links: the review document then proposes Links and values.
    changes: list[RecordUpsert] = []
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
        origin=Origin(type="test"),
        runtime=runtime,
        clock=clock,
    )
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        facts = {
            "reviewer": await _key(store, "reviewer", "reviewer"),
            "admin": await _key(store, "admin", "admin"),
            "supplier": supplier.id,
        }
    finally:
        await engine.dispose()
    return Seeded(owner_url=owner_url, app_url=app_url, schema=schema, facts=facts)


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def _section() -> dict[str, Any]:
    return {"auth": {"mode": "api_key", "api_keys": {"pepper": PEPPER}}}


async def _proposal_with_vault_item(seeded: Seeded) -> tuple[str, str]:
    """An extraction Proposal of the review document, plus a pending value item proposing
    an email for S-1025; returns the Proposal and item ids."""
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        uploaded = await http.post(
            f"{BASE}/documents",
            files={"file": (REVIEW.name, REVIEW.read_bytes(), "text/markdown")},
            data={"extract": "false"},
            headers={"Authorization": f"Bearer {seeded.facts['admin']}"},
        )
        assert uploaded.status_code == 201, uploaded.text
        document_id = uploaded.json()["document"]["id"]
    engine = create_engine(DatabaseSettings(), url=seeded.owner_url, statement_timeout_ms=0)
    try:
        run = ExtractionRun(
            async_sessionmaker(engine, expire_on_commit=False),
            _runtime(),
            ExtractionSettings(tiers=(0,)),
            InferenceSettings(),
            principal="svc_scribe",
            clock=Clock(),
        )
        report = await run.run(DOMAIN, [document_id])
    finally:
        await engine.dispose()
    assert report.proposal_id is not None, report
    partition, element_key = record_element(seeded.facts["supplier"], KEY)
    item_id = "pit_vaultemail0000000000000001"
    async with owner_session(seeded.owner_url) as session, session.begin():
        await scope_to_domain(session, DOMAIN)
        await ProposalItemRepository(session, DOMAIN).insert(
            report.proposal_id,
            [
                ItemRow(
                    item_id=item_id,
                    item_type=VALUE_ITEM,
                    seq=10_000,
                    subject={
                        "kind": seeded.facts["supplier"],
                        "key": KEY,
                        "attribute": EMAIL,
                        "value": "someone@example.com",
                        "current": None,
                    },
                    partition=partition,
                    element_key=element_key,
                    attribute_id=EMAIL,
                    confidence=0.5,
                )
            ],
        )
    return report.proposal_id, item_id


async def _decide(
    http: httpx.AsyncClient, key: str, proposal_id: str, decision: dict[str, Any]
) -> httpx.Response:
    return await http.post(
        f"{BASE}/proposals/{proposal_id}/items:decide",
        json={"decisions": [decision]},
        headers={"Authorization": f"Bearer {key}"},
    )


async def test_edits_setting_vault_values_need_pii_import(seeded: Seeded) -> None:
    proposal_id, item_id = await _proposal_with_vault_item(seeded)
    edited = {"item": item_id, "decision": "accepted", "edits": {"value": "guess@example.com"}}
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        refused = await _decide(http, seeded.facts["reviewer"], proposal_id, edited)
        assert refused.status_code == 403, refused.text
        assert refused.json()["permission"] == "pii.import"
        # Nothing was decided.
        listed = await http.get(
            f"{BASE}/proposals/{proposal_id}/items",
            params={"status": "accepted"},
            headers={"Authorization": f"Bearer {seeded.facts['admin']}"},
        )
        assert item_id not in {i["id"] for i in listed.json()["items"]}
        # Rejecting it, or edits without a vault value, need no more than proposal.review.
        rejected = await _decide(
            http,
            seeded.facts["reviewer"],
            proposal_id,
            {"item": item_id, "decision": "rejected", "edits": {}},
        )
        assert rejected.status_code == 200, rejected.text
        allowed = await _decide(http, seeded.facts["admin"], proposal_id, edited)
        assert allowed.status_code == 200, allowed.text
