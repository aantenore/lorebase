"""Derived values whose sensitivity comes from another Kind, on the regular read paths
(design 10.2, 13.5).

In the `supply` example, Supplier `risk_score` is tagged pii and Part `supplier_count` counts
the Suppliers whose risk score is at least 50: the count tells about Suppliers' personal data.
A caller with `pii.read` narrowed to Part sees it masked (and may not filter or order by it);
narrowed to Part and Supplier, or Domain-wide, it sees the count. Same rule as the Derivation
preview (`Masking.hidden` with the Kinds each tag comes from).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model.yaml_schema import load_schema
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

BASE = f"/api/v1/domains/{DOMAIN}"
PEPPER = "derived-sensitivity-pepper-0123456789-0123"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
KEY = "P-10001"
RECORD = f"/kinds/Part/records/{KEY}"
DERIVED = "supplier_count"
MASK = "***"
CALLERS = (("pii_parts", True), ("pii_both", False), ("cleared", False))
"""Caller -> whether the derived value is masked for it."""


async def _principal(store: SqlAuthStore, name: str, *grants: dict[str, Any]) -> str:
    async with store.unit() as unit:
        principal = await unit.principals.add(PrincipalType.SERVICE, name)
        for grant in grants:
            await unit.grants.add(
                domain_id=DOMAIN, subject=principal.id, created_by="usr_seed", **grant
            )
        generated = CODEC.generate()
        await unit.api_keys.add(
            principal_id=principal.id,
            prefix=generated.prefix,
            key_hash=generated.key_hash,
            scopes=[f"domain:{DOMAIN}"],
        )
    return generated.token


def _pii_on(kind: str) -> dict[str, Any]:
    return {"role": "pii-only", "scope_type": GrantScope.KIND, "scope_ref": kind}


def _schema() -> Any:
    """The example schema with Supplier risk scores tagged pii and Part `supplier_count`
    counting risky Suppliers (tagged pii, as the schema rules ask)."""
    document = yaml.safe_load((EXAMPLE / "schema.yaml").read_text(encoding="utf-8"))
    document["kinds"]["Supplier"]["attributes"]["risk_score"]["sensitivity"] = ["pii"]
    for derived in (DERIVED, "single_sourced"):
        document["kinds"]["Part"]["attributes"][derived]["sensitivity"] = ["pii"]
    document["derivations"][DERIVED]["definition"]["filter"] = "risk_score >= 50"
    return load_schema(yaml.safe_dump(document))


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, commits = await load_supply(session, runtime, clock, _schema())
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        viewer = {"role": "viewer"}
        facts = {
            "pii_parts": await _principal(store, "pii parts", viewer, _pii_on("Part")),
            "pii_both": await _principal(
                store, "pii both", viewer, _pii_on("Part"), _pii_on("Supplier")
            ),
            "cleared": await _principal(store, "cleared", {"role": "pii-reader"}),
        }
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.facts = facts
    seeded.revisions = {"schema": commits[1].revision_id.hex()}
    return seeded


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def _section() -> dict[str, Any]:
    return {
        "auth": {
            "mode": "api_key",
            "api_keys": {"pepper": PEPPER},
            "authz": {
                "custom_roles": {
                    "pii-reader": ["schema.read", "data.read", "pii.read"],
                    "pii-only": ["pii.read"],
                }
            },
        }
    }


async def _get(http: httpx.AsyncClient, key: str, path: str, **params: Any) -> httpx.Response:
    response = await http.get(
        BASE + path, params=params, headers={"Authorization": f"Bearer {key}"}
    )
    return response


async def _post(http: httpx.AsyncClient, key: str, path: str, body: Any) -> httpx.Response:
    return await http.post(BASE + path, json=body, headers={"Authorization": f"Bearer {key}"})


def _ok(response: httpx.Response) -> Any:
    assert response.status_code == 200, response.text
    return response.json()


def _derived_of(items: list[dict[str, Any]]) -> list[Any]:
    """The derived value of the seeded Part among Record bodies."""
    return [item["values"].get(DERIVED) for item in items if item["key"] == KEY]


async def test_records_lists_and_queries(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, _section()) as http:
        for caller, masked in CALLERS:
            key = seeded.facts[caller]
            record = _ok(await _get(http, key, RECORD))["record"]["values"]
            assert (record[DERIVED] == MASK) is masked, caller
            assert (record["single_sourced"] == MASK) is masked, caller
            listed = _ok(await _get(http, key, "/kinds/Part/records", limit=100))["items"]
            queried = _ok(await _post(http, key, "/query", {"kind": "Part", "limit": 100}))
            for found in (_derived_of(listed), _derived_of(queried["items"])):
                assert found, caller
                assert (found[0] == MASK) is masked, caller
            # Filtering or ordering by the masked value would tell it: refused.
            where = {"kind": "Part", "where": {"attr": DERIVED, "op": "gte", "value": 1}}
            filtered = await _post(http, key, "/query", where)
            ordered = await _get(http, key, "/kinds/Part/records", order_by=DERIVED)
            grouped = await _post(
                http, key, "/query", {"kind": "Part", "aggregate": {"group_by": DERIVED}}
            )
            for response in (filtered, ordered, grouped):
                assert response.status_code == (403 if masked else 200), (caller, response.text)
                if masked:
                    assert response.json()["code"] == "sensitive_attribute"


async def test_search(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, _section()) as http:
        for caller, masked in CALLERS:
            key = seeded.facts[caller]
            body = {"q": "bearing", "kinds": ["Part"]}
            assert (await _post(http, key, "/search", body)).status_code == 200, caller
            where = {**body, "where": {"attr": DERIVED, "op": "gte", "value": 0}}
            filtered = await _post(http, key, "/search", where)
            assert filtered.status_code == (403 if masked else 200), (caller, filtered.text)


async def test_history_and_lineage(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, _section()) as http:
        for caller, masked in CALLERS:
            key = seeded.facts[caller]
            with_lineage = _ok(await _get(http, key, RECORD, lineage="true"))["record"]
            assert (with_lineage["values"][DERIVED] == MASK) is masked, caller
            # Derived values are not stored in Revisions; any that appears is masked alike.
            lineage = _ok(await _get(http, key, f"{RECORD}/lineage"))
            history = _ok(await _get(http, key, f"{RECORD}/history", values="true"))
            shown = [a["value"] for a in lineage["attributes"] if a["name"] == DERIVED]
            shown += [
                value
                for item in history["items"]
                for a in item["attributes"]
                if a["name"] == DERIVED
                for value in (a["before"], a["after"])
                if value is not None
            ]
            assert all((value == MASK) is masked for value in shown), caller


async def test_context_packs_and_explain(seeded: Seeded) -> None:
    async with keeper(seeded.app_url, _section()) as http:
        for caller, masked in CALLERS:
            key = seeded.facts[caller]
            explained = _ok(await _get(http, key, f"{RECORD}/explain"))
            assert (explained["record"]["values"][DERIVED] == MASK) is masked, caller
            context = _ok(await _post(http, key, "/context", {"refs": [f"Part/{KEY}"]}))
            (seed,) = context["seeds"]
            assert (seed["values"][DERIVED] == MASK) is masked, caller


async def test_change_feed_carries_no_values(seeded: Seeded) -> None:
    """Revision events name changed Kinds and keys only; webhook clearances are Domain-wide
    tags (no Kind-narrowed clearance to check per origin Kind)."""
    since = seeded.revisions["schema"]
    async with keeper(seeded.app_url, _section()) as http:
        for caller, _ in CALLERS:
            changes = await _get(http, seeded.facts[caller], "/branches/main/changes", since=since)
            body = _ok(changes)
            assert body["items"], caller
            assert DERIVED not in changes.text, caller
