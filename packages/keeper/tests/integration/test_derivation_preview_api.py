"""Previews of candidate Derivations through the API: unsaved edits evaluated on sample Records
with Keeper's permissions and masking, and nothing written.

The `supply` Supplier Kind gains `risk_band`, derived from `risk_score`, which is tagged `pii`
here, and so is the band (a derived Attribute carries the tags of what it reads).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Attribute, Derivation, ExpressionDerivation, Sensitivity
from lorebase_core.revision import Origin
from lorebase_core.types import TypeSpec
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_store.auth import SqlAuthStore
from lorebase_store.commit import SchemaUpsert, commit
from lorebase_store.db import create_engine
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings

from .read_fixtures import Seeded, keeper, seeded_database
from .supply import AUTHOR, DOMAIN, EXAMPLE, Clock, load_supply, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
PREVIEW = f"{BASE}/derivations:preview"
PEPPER = "preview-pepper-0123456789-0123456789"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
BAND_ID = "att_01JB8ZPREV00000000000000B1"
BAND_DERIVATION = "drv_01JB8ZPREV00000000000000D1"
BAND = "risk_band"


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


async def seed(
    session: AsyncSession, runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    schema, _ = await load_supply(session, runtime, clock)
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    score = supplier.attribute_by_api_name("risk_score")
    assert score is not None
    band = Attribute(
        id=BAND_ID,
        api_name=BAND,
        description="Risk band of the supplier. Derived.",
        type=TypeSpec.of("string"),
        sensitivity=(Sensitivity.PII,),
        derivation_id=BAND_DERIVATION,
    )
    derivation = Derivation(
        id=BAND_DERIVATION,
        api_name=BAND,
        description="High from a risk score of 70.",
        definition=ExpressionDerivation(
            kind=supplier.id,
            attribute=BAND_ID,
            expression='risk_score >= 70 ? "high" : "low"',
        ),
    )
    tagged = score.model_copy(update={"sensitivity": (Sensitivity.PII,)})
    supplier = supplier.model_copy(
        update={"attributes": {**supplier.attributes, score.id: tagged, BAND_ID: band}}
    )
    await commit(
        session,
        DOMAIN,
        "main",
        [],
        schema_changes=[SchemaUpsert(derivation), SchemaUpsert(supplier)],
        author=AUTHOR,
        message="Risk band",
        origin=Origin(type="test"),
        runtime=runtime,
        clock=clock,
    )
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        keys = {
            "viewer": await _principal(store, "viewer", {"role": "viewer"}),
            "cleared": await _principal(store, "cleared", {"role": "pii-reader"}),
            "schema_only": await _principal(store, "schema only", {"role": "schema-only"}),
            "parts_only": await _principal(
                store,
                "parts only",
                {"role": "viewer", "scope_type": GrantScope.KIND, "scope_ref": "Part"},
            ),
            # pii.read narrowed to the Kinds a value of Part computed from Suppliers involves.
            "pii_parts": await _principal(store, "pii parts", {"role": "viewer"}, _pii_on("Part")),
            "pii_suppliers": await _principal(
                store, "pii suppliers", {"role": "viewer"}, _pii_on("Supplier")
            ),
            "pii_both": await _principal(
                store, "pii both", {"role": "viewer"}, _pii_on("Part"), _pii_on("Supplier")
            ),
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
            "authz": {
                "custom_roles": {
                    "pii-reader": ["schema.read", "data.read", "pii.read"],
                    "schema-only": ["schema.read"],
                    "pii-only": ["pii.read"],
                }
            },
        }
    }


async def _preview(
    http: httpx.AsyncClient, key: str, derivation: str, patch: dict[str, Any], **extra: Any
) -> httpx.Response:
    body = {"derivation": derivation, "patch": patch, **extra}
    return await http.post(PREVIEW, json=body, headers={"Authorization": f"Bearer {key}"})


def _edit(name: str, **definition: Any) -> dict[str, Any]:
    return {"derivations": {name: {"definition": definition}}}


def _supplier_counts() -> dict[str, int]:
    counts = {row["part_number"]: 0 for row in rows("part.csv")}
    for row in rows("supplied_by.csv"):
        counts[row["from_key"]] += 1
    return counts


def _values(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return {item["key"]: item["value"] for item in response.json()["items"]}


async def test_an_unsaved_expression_is_evaluated_on_sample_records(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    counts = _supplier_counts()
    edit = _edit(
        "single_sourced",
        variant="expression",
        kind="Part",
        attribute="single_sourced",
        expression="supplier_count >= 2",
    )
    async with keeper(seeded.app_url, _section()) as http:
        response = await _preview(http, key, "single_sourced", edit, limit=4)
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["kind"], body["attribute"], body["served_from"]) == (
            "Part",
            "single_sourced",
            "materialized",
        )
        first = sorted(counts)[:4]
        assert [item["key"] for item in body["items"]] == first
        assert _values(response) == {k: counts[k] >= 2 for k in first}
        # The Derivation as saved, and nothing stored by the preview.
        saved = await _preview(http, key, "single_sourced", {}, limit=4)
        assert _values(saved) == {k: counts[k] == 1 for k in first}
        record = await http.get(
            f"{BASE}/kinds/Part/records/{first[0]}", headers={"Authorization": f"Bearer {key}"}
        )
        assert record.json()["record"]["values"]["single_sourced"] == (counts[first[0]] == 1)


async def test_an_aggregate_with_a_filter_and_chosen_keys(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    status = {row["supplier_code"]: row["status"] for row in rows("supplier.csv")}
    active = dict.fromkeys(_supplier_counts(), 0)
    for row in rows("supplied_by.csv"):
        if status[row["to_key"]] == "active":
            active[row["from_key"]] += 1
    edit = _edit(
        "supplier_count",
        variant="aggregate",
        kind="Part",
        attribute="supplier_count",
        relation="supplied_by",
        function="count",
        filter='status == "active"',
    )
    wanted = sorted(active)[3:6]
    async with keeper(seeded.app_url, _section()) as http:
        response = await _preview(
            http, key, "supplier_count", edit, keys=[*wanted, "P-missing"], limit=10
        )
        assert {k: int(v) for k, v in _values(response).items()} == {k: active[k] for k in wanted}


async def test_a_candidate_that_does_not_type_check_is_refused(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    wrong = _edit(
        "single_sourced",
        variant="expression",
        kind="Part",
        attribute="single_sourced",
        expression="supplier_count + 'x'",
    )
    async with keeper(seeded.app_url, _section()) as http:
        response = await _preview(http, key, "single_sourced", wrong)
        assert response.status_code == 422, response.text
        assert response.json()["issues"][0]["code"] == "schema.expression_invalid"
        missing = await _preview(http, key, "nothing", {})
        assert missing.status_code == 422
        assert missing.json()["issues"][0]["code"] == "derivation.not_found"
        # The limits of the expression engine are the schema's.
    async with keeper(
        seeded.app_url, _section(), schema={"expression_limits": {"max_source_length": 8}}
    ) as http:
        long = _edit(
            "single_sourced",
            variant="expression",
            kind="Part",
            attribute="single_sourced",
            expression="supplier_count == 1 || supplier_count == 2",
        )
        refused = await _preview(http, key, "single_sourced", long)
        assert refused.status_code == 422
        assert any("single_sourced" in issue["path"] for issue in refused.json()["issues"])


async def test_the_sample_size_is_bounded_by_the_settings(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    async with keeper(seeded.app_url, _section()) as http:
        default = await _preview(http, key, "single_sourced", {})
        assert len(default.json()["items"]) == 5
        many = await _preview(http, key, "single_sourced", {}, limit=26)
        assert many.status_code == 422
        assert many.json()["errors"][0]["pointer"] == "/limit"
    async with keeper(
        seeded.app_url,
        _section(),
        read={"derivation_preview": {"default_limit": 2, "max_limit": 6}},
    ) as http:
        assert len((await _preview(http, key, "single_sourced", {})).json()["items"]) == 2
        capped = await _preview(http, key, "single_sourced", {}, limit=6)
        assert len(capped.json()["items"]) == 6
        assert (await _preview(http, key, "single_sourced", {}, limit=7)).status_code == 422
        old = await _preview(http, key, "single_sourced", {}, at="0" * 64)
        assert old.status_code in (403, 404)


async def test_values_computed_from_sensitive_attributes_are_masked(seeded: Seeded) -> None:
    facts = seeded.facts
    edit = _edit(
        BAND,
        variant="expression",
        kind="Supplier",
        attribute=BAND,
        expression='risk_score >= 50 ? "high" : "low"',
    )
    async with keeper(seeded.app_url, _section()) as http:
        masked = await _preview(http, facts["viewer"], BAND, edit, limit=5)
        assert masked.status_code == 200, masked.text
        items = masked.json()["items"]
        assert items
        assert all(item["masked"] and item["value"] == "***" for item in items)
        assert not any('"high"' in str(item) or '"low"' in str(item) for item in items)
        shown = await _preview(http, facts["cleared"], BAND, edit, limit=5)
        values = _values(shown)
        scores = {r["supplier_code"]: r["risk_score"] for r in rows("supplier.csv")}
        assert all(not item["masked"] for item in shown.json()["items"])
        for supplier, value in values.items():
            if scores[supplier] != "":
                assert value == ("high" if int(scores[supplier]) >= 50 else "low")


async def test_the_caller_needs_to_read_every_kind_the_derivation_touches(seeded: Seeded) -> None:
    facts = seeded.facts
    async with keeper(seeded.app_url, _section()) as http:
        none = await _preview(http, facts["schema_only"], "single_sourced", {})
        assert none.status_code == 403
        assert none.json()["permission"] == "data.read"
        # supplier_count reads Suppliers through supplied_by; single_sourced only Parts.
        denied = await _preview(http, facts["parts_only"], "supplier_count", {})
        assert denied.status_code == 403, denied.text
        assert denied.json()["kind"] == "Supplier"
        allowed = await _preview(http, facts["parts_only"], "single_sourced", {})
        assert allowed.status_code == 200, allowed.text
        anonymous = await http.post(PREVIEW, json={"derivation": "single_sourced", "patch": {}})
        assert anonymous.status_code == 401


async def test_masked_failures_carry_no_code(seeded: Seeded) -> None:
    facts = seeded.facts
    failing = _edit(
        BAND,
        variant="expression",
        kind="Supplier",
        attribute=BAND,
        expression='risk_score / 0 >= 1 ? "high" : "low"',
    )
    async with keeper(seeded.app_url, _section()) as http:
        masked = await _preview(http, facts["viewer"], BAND, failing, limit=5)
        assert masked.status_code == 200, masked.text
        items = masked.json()["items"]
        assert items
        assert all(item["masked"] for item in items)
        assert all(item["error"] == {"code": None, "message": None} for item in items)
        shown = await _preview(http, facts["cleared"], BAND, failing, limit=5)
        errors = [item["error"] for item in shown.json()["items"]]
        assert errors
        assert all(error["code"] and error["message"] for error in errors)


async def test_keys_are_bounded_in_length(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    async with keeper(
        seeded.app_url, _section(), read={"derivation_preview_max_key_length": 8}
    ) as http:
        fine = await _preview(http, key, "single_sourced", {}, keys=["P-1"])
        assert fine.status_code == 200, fine.text
        long = await _preview(http, key, "single_sourced", {}, keys=["P-1", "P-" + "9" * 8])
        assert long.status_code == 422
        assert long.json()["errors"][0]["pointer"] == "/keys/1"


async def test_values_from_a_sensitive_far_kind_need_clearance_on_every_kind(
    seeded: Seeded,
) -> None:
    """supplier_count of Parts counting Suppliers by their pii risk score (tagged pii, as the
    schema rules ask) tells about the Suppliers: pii.read is needed on Supplier, and on Part,
    which holds the value."""
    facts = seeded.facts
    filtered = _edit(
        "supplier_count",
        variant="aggregate",
        kind="Part",
        attribute="supplier_count",
        relation="supplied_by",
        function="count",
        filter="risk_score >= 50",
    )
    part = yaml.safe_load((EXAMPLE / "schema.yaml").read_text())["kinds"]["Part"]
    for derived in ("supplier_count", "single_sourced"):  # single_sourced reads the count
        part["attributes"][derived]["sensitivity"] = ["pii"]
    filtered["kinds"] = {"Part": part}
    async with keeper(seeded.app_url, _section()) as http:
        for caller, masked in (
            ("pii_parts", True),
            ("pii_suppliers", True),
            ("pii_both", False),
            ("cleared", False),
            ("viewer", True),
        ):
            response = await _preview(http, facts[caller], "supplier_count", filtered, limit=3)
            assert response.status_code == 200, (caller, response.text)
            items = response.json()["items"]
            assert items
            assert all(item["masked"] is masked for item in items), caller
        # Read as saved, the count reads nothing sensitive: a Part clearance is not needed.
        plain = await _preview(http, facts["viewer"], "supplier_count", {}, limit=3)
        assert not any(item["masked"] for item in plain.json()["items"])
