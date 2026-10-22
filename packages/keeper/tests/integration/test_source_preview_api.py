"""Previews of the expression mappings of candidate Sources through the API."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.db import create_engine
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import DOMAIN, EXAMPLE, Clock, load_supply

pytestmark = pytest.mark.integration

PREVIEW = f"/api/v1/domains/{DOMAIN}/sources:preview"
PEPPER = "source-preview-pepper-0123456789-0123456789"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
SOURCE = "approved.suppliers"


async def _key(store: SqlAuthStore, name: str, role: str, kind: str | None = None) -> str:
    narrowed: dict[str, Any] = (
        {} if kind is None else {"scope_type": GrantScope.KIND, "scope_ref": kind}
    )
    async with store.unit() as unit:
        principal = await unit.principals.add(PrincipalType.SERVICE, name)
        await unit.grants.add(
            domain_id=DOMAIN, subject=principal.id, created_by="usr_seed", role=role, **narrowed
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
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, runtime, clock)
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        keys = {
            "viewer": await _key(store, "viewer", "viewer"),
            "data_only": await _key(store, "data only", "data-only"),
            "suppliers_schema": await _key(store, "suppliers", "schema-only", "Supplier"),
            "parts_schema": await _key(store, "parts", "schema-only", "Part"),
        }
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.facts = keys
    return seeded


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def _section() -> dict[str, Any]:
    return {
        "auth": {
            "mode": "api_key",
            "api_keys": {"pepper": PEPPER},
            "authz": {"custom_roles": {"data-only": ["data.read"], "schema-only": ["schema.read"]}},
        }
    }


def _edited(**mapping: dict[str, Any]) -> dict[str, Any]:
    document = yaml.safe_load((EXAMPLE / "schema.yaml").read_text())
    source = document["sources"][SOURCE]
    source["attribute_mapping"] = {**source["attribute_mapping"], **mapping}
    return {"sources": {SOURCE: source}}


async def _preview(
    http: httpx.AsyncClient,
    key: str,
    patch: dict[str, Any],
    rows: list[dict[str, Any]],
    **extra: Any,
) -> httpx.Response:
    body = {"source": SOURCE, "patch": patch, "rows": rows, **extra}
    return await http.post(PREVIEW, json=body, headers={"Authorization": f"Bearer {key}"})


async def test_expression_mappings_are_evaluated_on_the_rows_sent(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    patch = _edited(
        legal_name={"expression": 'name + " (" + country + ")"'},
        risk_score={"expression": "int(score) * 2"},
    )
    rows = [{"name": "Kavsen", "country": "PL", "score": "21"}, {"name": "X", "country": "DE"}]
    async with keeper(seeded.app_url, _section()) as http:
        response = await _preview(http, key, patch, rows)
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["source"], body["target_kind"]) == (SOURCE, "Supplier")
        assert body["attributes"] == ["legal_name", "risk_score"]
        first, second = body["rows"]
        assert first["legal_name"] == {"value": "Kavsen (PL)", "error": None}
        assert first["risk_score"]["value"] == 42
        # A column only another row has is null here, as in Scribe.
        assert second["risk_score"]["error"]["code"] == "expression.evaluation"
        nullable = _edited(city={"expression": 'town == null ? "none" : town'})
        mixed = await _preview(http, key, nullable, [{"name": "a"}, {"town": "Gdansk"}])
        assert [row["city"]["value"] for row in mixed.json()["rows"]] == ["none", "Gdansk"]
        none = await _preview(http, key, {}, rows)
        assert none.json()["rows"] == [{}, {}]


async def test_invalid_candidates_and_oversized_samples_are_refused(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    async with keeper(seeded.app_url, _section(), read={"source_preview_max_rows": 2}) as http:
        rows = [{"a": "1"}]
        missing = await http.post(
            PREVIEW,
            json={"source": "nothing", "patch": {}, "rows": rows},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert missing.status_code == 422
        assert missing.json()["issues"][0]["code"] == "source.not_found"
        broken = _edited(city={"expression": "a +"})
        accepted = await _preview(http, key, broken, rows)
        assert accepted.status_code == 200
        assert accepted.json()["rows"][0]["city"]["error"]["code"] == "expression.syntax"
        many = await _preview(http, key, {}, rows * 3)
        assert many.status_code == 422
        assert many.json()["errors"][0]["pointer"] == "/rows"
        empty = await _preview(http, key, {}, [])
        assert empty.status_code == 422


async def test_the_caller_needs_to_read_the_schema(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, _section()) as http:
        denied = await _preview(http, seeded.facts["data_only"], {}, [{"a": "1"}])
        assert denied.status_code == 403, denied.text
        assert denied.json()["permission"] == "schema.read"
        anonymous = await http.post(PREVIEW, json={"source": SOURCE, "patch": {}, "rows": [{}]})
        assert anonymous.status_code == 401


async def test_columns_and_cells_are_bounded(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    two = _edited(city={"expression": "a"}, legal_name={"expression": "a"})
    async with keeper(
        seeded.app_url,
        _section(),
        read={"source_preview_max_columns": 2, "source_preview_max_cells": 4},
    ) as http:
        wide = await _preview(http, key, two, [{"a": "1"}, {"a": "1", "b": "2", "c": "3"}])
        assert wide.status_code == 422
        assert wide.json()["errors"][0]["pointer"] == "/rows/1"
        assert (await _preview(http, key, two, [{"a": "1"}] * 2)).status_code == 200
        cells = await _preview(http, key, two, [{"a": "1"}] * 3)
        assert cells.status_code == 422, cells.text
        assert cells.json()["errors"][0]["pointer"] == "/rows"
        # Without expression mappings there is nothing to evaluate.
        assert (await _preview(http, key, {}, [{"a": "1"}] * 3)).status_code == 200


async def test_a_kind_narrowed_grant_covers_its_target_kind_only(seeded: Seeded) -> None:
    facts = seeded.facts
    patch = _edited(city={"expression": "a"})
    invalid = {"sources": {SOURCE: {"type": "nope"}}}
    async with keeper(seeded.app_url, _section()) as http:
        allowed = await _preview(http, facts["suppliers_schema"], patch, [{"a": "x"}])
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["rows"] == [{"city": {"value": "x", "error": None}}]
        denied = await _preview(http, facts["parts_schema"], patch, [{"a": "x"}])
        assert denied.status_code == 403, denied.text
        assert (denied.json()["permission"], denied.json()["kind"]) == ("schema.read", "Supplier")
        missing = await http.post(
            PREVIEW,
            json={"source": "nothing", "patch": {}, "rows": [{"a": "x"}]},
            headers={"Authorization": f"Bearer {facts['suppliers_schema']}"},
        )
        assert missing.status_code == 403
        # A change set that cannot be read is reported to any reader of the schema.
        unreadable = await _preview(http, facts["parts_schema"], invalid, [{"a": "x"}])
        assert unreadable.status_code == 422
