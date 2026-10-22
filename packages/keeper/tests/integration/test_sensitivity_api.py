"""Sensitive values in reads and the vault through the API (design 10.2, 13.5).

The `supply` Supplier Kind gains `email` (pii, stored in the vault), `phone` (pii) and
`nickname` (pii, searchable). A viewer sees them masked on every read path; a caller with
`pii.read` (Domain-wide or on the Kind) sees them; filters on them need the clearance; erasure
reaches every Revision and is audited.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lorebase_core.model import Attribute, Sensitivity
from lorebase_core.revision import Origin
from lorebase_core.types import TypeSpec
from lorebase_core.validate import RecordUpsert
from lorebase_keeper.auth.api_keys import ApiKeyCodec
from lorebase_keeper.auth.config import ApiKeySettings
from lorebase_keeper.reading.caching import CacheKey, entity_tag
from lorebase_store.auth import SqlAuthStore
from lorebase_store.commit import SchemaUpsert, commit
from lorebase_store.db import create_engine
from lorebase_store.materialize import create_branch
from lorebase_store.models import AuditEntry, AuditReadEntry
from lorebase_store.models.enums import GrantScope, PrincipalType
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, StoreSettings
from lorebase_store.vault import VaultSettings

from .read_fixtures import Seeded, keeper, owner_session, seeded_database
from .supply import AUTHOR, DOMAIN, Clock, commit_changes, load_supply, record_change, rows

pytestmark = pytest.mark.integration

BASE = f"/api/v1/domains/{DOMAIN}"
PEPPER = "sensitivity-pepper-0123456789-0123456789"
CODEC = ApiKeyCodec(ApiKeySettings(pepper=PEPPER))
VAULT = {"token_key": "t" * 40, "encryption_key": "e" * 40, "key_id": "k1"}
PII = (Sensitivity.PII,)
KEY = "S-1002"
RECORD = f"/kinds/Supplier/records/{KEY}"
STALE = "stale"
OLD_EMAIL, NEW_EMAIL = "old.person@example.com", "new.person@example.com"
PHONE, NICKNAME = "555 0100", "Quixotica"
SECRETS = (OLD_EMAIL, NEW_EMAIL, PHONE, NICKNAME)


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


def _attribute(attribute_id: str, name: str, **extra: Any) -> Attribute:
    return Attribute(
        id=attribute_id,
        api_name=name,
        description=f"The {name}.",
        type=TypeSpec.of("string"),
        sensitivity=PII,
        **extra,
    )


async def seed(
    session: AsyncSession, _runtime: StoreRuntime, clock: Clock, owner_url: str, app_url: str
) -> Seeded:
    runtime = StoreRuntime.from_settings(StoreSettings(), vault=VaultSettings(**VAULT))
    schema, _ = await load_supply(session, runtime, clock)
    supplier = schema.kind_by_api_name("Supplier")
    assert supplier is not None
    added = {
        "att_01JB8ZSENS00000000000000E1": _attribute(
            "att_01JB8ZSENS00000000000000E1", "email", vault=True
        ),
        # Tagged only after the first values are written (see below).
        "att_01JB8ZSENS00000000000000P1": _attribute(
            "att_01JB8ZSENS00000000000000P1", "phone", searchable=True
        ).model_copy(update={"sensitivity": ()}),
        "att_01JB8ZSENS00000000000000N1": _attribute(
            "att_01JB8ZSENS00000000000000N1", "nickname", searchable=True
        ),
    }
    supplier = supplier.model_copy(update={"attributes": {**supplier.attributes, **added}})

    async def change_schema(kind: Any, message: str) -> None:
        await commit(
            session,
            DOMAIN,
            "main",
            [],
            author=AUTHOR,
            message=message,
            origin=Origin(type="test"),
            schema_changes=[SchemaUpsert(kind)],
            runtime=runtime,
            clock=clock,
        )

    await change_schema(supplier, "Sensitive Attributes")
    original = next(r for r in rows("supplier.csv") if r["supplier_code"] == KEY)

    def change(email: str) -> RecordUpsert:
        return record_change(
            supplier, {**original, "email": email, "phone": PHONE, "nickname": NICKNAME}
        )

    first = await commit_changes(session, runtime, clock, [change(OLD_EMAIL)], message="Contact")
    # A branch forked before phone is tagged: its index still holds the phone's words.
    await create_branch(session, DOMAIN, STALE, created_by="usr_seed", runtime=runtime, clock=clock)
    phone = "att_01JB8ZSENS00000000000000P1"
    tagged = supplier.attributes[phone].model_copy(update={"sensitivity": PII})
    supplier = supplier.model_copy(update={"attributes": {**supplier.attributes, phone: tagged}})
    await change_schema(supplier, "Phone numbers are personal data")
    second = await commit_changes(session, runtime, clock, [change(NEW_EMAIL)], message="Moved")
    engine = create_engine(DatabaseSettings(), url=owner_url, pooled=False)
    try:
        store = SqlAuthStore(async_sessionmaker(engine, expire_on_commit=False))
        viewer = await _principal(store, "viewer", {"role": "viewer"})
        cleared = await _principal(store, "cleared", {"role": "pii-reader"})
        kind_cleared = await _principal(
            store,
            "kind cleared",
            {"role": "viewer"},
            {"role": "pii-reader", "scope_type": GrantScope.KIND, "scope_ref": "Supplier"},
        )
        admin = await _principal(store, "admin", {"role": "admin"})
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.revisions = {"first": first.revision_id.hex(), "second": second.revision_id.hex()}
    seeded.facts = {"viewer": viewer, "cleared": cleared, "kind": kind_cleared, "admin": admin}
    return seeded


@pytest.fixture(scope="module")
def seeded(postgres_server_url: str) -> Iterator[Seeded]:
    yield from seeded_database(postgres_server_url, seed)


def _section() -> dict[str, Any]:
    return {
        "auth": {
            "mode": "api_key",
            "api_keys": {"pepper": PEPPER},
            "authz": {"custom_roles": {"pii-reader": ["schema.read", "data.read", "pii.read"]}},
        }
    }


async def _get(http: httpx.AsyncClient, key: str, path: str, **params: Any) -> httpx.Response:
    return await http.get(BASE + path, params=params, headers={"Authorization": f"Bearer {key}"})


async def _post(http: httpx.AsyncClient, key: str, path: str, body: Any) -> httpx.Response:
    return await http.post(BASE + path, json=body, headers={"Authorization": f"Bearer {key}"})


def _values(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    values: dict[str, Any] = response.json()["record"]["values"]
    return values


def _hidden(response: httpx.Response) -> None:
    assert response.status_code == 200, response.text
    assert not any(secret in response.text for secret in SECRETS), response.text


async def test_viewers_see_masks_on_every_read_path(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    revs = seeded.revisions
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        values = _values(await _get(http, key, RECORD))
        assert (values["email"], values["phone"], values["nickname"]) == ("***",) * 3
        # A Revision whose schema had no tag on phone yet: the default branch's tags apply.
        earlier = _values(await _get(http, key, RECORD, at=revs["first"]))
        assert (earlier["email"], earlier["phone"]) == ("***", "***")
        _hidden(await _get(http, key, RECORD, lineage="true"))
        _hidden(await _get(http, key, "/kinds/Supplier/records", limit=100))
        _hidden(await _post(http, key, "/query", {"kind": "Supplier", "limit": 100}))
        _hidden(await _get(http, key, f"{RECORD}/history", values="true"))
        _hidden(await _get(http, key, f"{RECORD}/lineage"))
        _hidden(await _get(http, key, "/diff", **{"from": revs["first"], "to": revs["second"]}))
        _hidden(await _get(http, key, f"/revisions/{revs['second']}/changeset"))
        _hidden(await _get(http, key, f"{RECORD}/explain"))
        _hidden(await _get(http, key, f"{RECORD}/explain", format="markdown"))
        _hidden(await _get(http, key, "/branches/main/changes", since=revs["first"]))
        seeds = {"seeds": [{"kind": "Supplier", "key": KEY}]}
        _hidden(await _post(http, key, "/neighborhood", seeds))
        _hidden(await _post(http, key, "/context", {"refs": [f"Supplier/{KEY}"]}))
        search = await _post(http, key, "/search", {"q": NICKNAME})
        _hidden(search)
        assert search.json()["items"] == []
        # A branch forked before the default branch tagged phone: still masked, and its
        # full-text index (which holds the phone's words) is not searched for this caller.
        stale = _values(await _get(http, key, RECORD, branch=STALE))
        assert stale["phone"] == "***"
        on_stale = await _post(http, key, "/search", {"q": "0100", "branch": STALE})
        _hidden(on_stale)
        assert on_stale.json()["items"] == []
        # Filtering, ordering or grouping by a masked value is refused.
        refused = await _post(
            http,
            key,
            "/query",
            {"kind": "Supplier", "where": {"attr": "phone", "op": "eq", "value": PHONE}},
        )
        assert refused.status_code == 403
        assert refused.json()["code"] == "sensitive_attribute"
        ordered = await _get(http, key, "/kinds/Supplier/records", order_by="phone")
        assert ordered.status_code == 403


async def test_clearance_unmasks_and_responses_are_kept_apart(seeded: Seeded) -> None:
    facts = seeded.facts
    revs = seeded.revisions
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        for name in ("viewer", "cleared", "viewer", "kind", "viewer"):
            values = _values(await _get(http, facts[name], RECORD))
            if name == "viewer":
                assert values["email"] == "***"
            else:
                assert (values["email"], values["phone"]) == (NEW_EMAIL, PHONE)
        history = await _get(http, facts["cleared"], f"{RECORD}/history", values="true")
        changed = [
            (a["before"], a["after"])
            for item in history.json()["items"]
            for a in item["attributes"]
            if a["name"] == "email"
        ]
        assert (OLD_EMAIL, NEW_EMAIL) in changed
        diff = await _get(
            http, facts["cleared"], "/diff", **{"from": revs["first"], "to": revs["second"]}
        )
        assert NEW_EMAIL in diff.text
        assert OLD_EMAIL in diff.text
        # Filtering by a cleared value works; by a vault value it cannot (tokens).
        where = {"kind": "Supplier", "where": {"attr": "phone", "op": "eq", "value": PHONE}}
        found = await _post(http, facts["cleared"], "/query", where)
        assert [item["key"] for item in found.json()["items"]] == [KEY]
        vaulted = {"kind": "Supplier", "where": {"attr": "email", "op": "eq", "value": NEW_EMAIL}}
        assert (await _post(http, facts["cleared"], "/query", vaulted)).status_code == 422
        cleared_search = await _post(
            http, facts["cleared"], "/search", {"q": "0100", "branch": STALE}
        )
        assert [hit["key"] for hit in cleared_search.json()["items"]] == [KEY]


async def test_erasure_reaches_history_and_is_audited(seeded: Seeded) -> None:
    facts = seeded.facts
    body = {"kind": "Supplier", "key": KEY}
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        assert (await _post(http, facts["cleared"], "/vault/erase", body)).status_code == 403
        erased = await _post(http, facts["admin"], "/vault/erase", body)
        assert erased.status_code == 200, erased.text
        assert erased.json()["erased"] == 2
        again = await _post(http, facts["admin"], "/vault/erase", body)
        assert again.json()["erased"] == 0
        values = _values(await _get(http, facts["cleared"], RECORD))
        assert (values["email"], values["phone"]) == ("[erased]", PHONE)
        history = await _get(http, facts["cleared"], f"{RECORD}/history", values="true")
        assert OLD_EMAIL not in history.text
        assert NEW_EMAIL not in history.text
        assert "[erased]" in history.text
    async with owner_session(seeded.owner_url) as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        entries = (
            await session.execute(
                select(AuditEntry.action, AuditEntry.target, AuditEntry.details).where(
                    AuditEntry.action == "vault.erase"
                )
            )
        ).all()
    assert [e.details["erased"] for e in entries if "mode" in e.details] == [2, 0]
    assert not any(secret in str(entries) for secret in SECRETS)
    # The erased Record's key is kept as a keyed digest only.
    assert KEY not in str(entries)
    digests = {e.details["key_digest"] for e in entries if "mode" in e.details}
    assert len(digests) == 1
    assert len(next(iter(digests))) == 64


async def test_vault_keys_need_an_installation_administrator(seeded: Seeded) -> None:
    facts = seeded.facts
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        headers = {"Authorization": f"Bearer {facts['admin']}"}
        assert (await http.get("/api/v1/vault/keys", headers=headers)).status_code == 403
        assert (await http.post("/api/v1/vault/rotate", headers=headers)).status_code == 403
    async with keeper(seeded.app_url, vault=VAULT) as http:  # authentication off: allowed
        keys = await http.get("/api/v1/vault/keys")
        assert keys.status_code == 200, keys.text
        found = keys.json()
        assert (found["key_id"], found["fingerprint_key"]) == ("k1", "1/2")
        assert set(found["encryption_keys"]) <= {"k1"}
        assert (found["outdated"], found["digests"]) == (0, "vault.token_key")
        queued = await http.post("/api/v1/vault/rotate")
        assert queued.status_code == 202, queued.text
        again = await http.post("/api/v1/vault/rotate")
        assert again.json()["job_id"] == queued.json()["job_id"]


async def test_early_not_modified_depends_on_the_default_branch_tags(seeded: Seeded) -> None:
    key = seeded.facts["viewer"]
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        first = await _get(http, key, RECORD, branch=STALE)
        assert first.status_code == 200
        head = first.headers["Lorebase-Revision"]
        current = await http.get(
            BASE + RECORD,
            params={"branch": STALE},
            headers={"Authorization": f"Bearer {key}", "If-None-Match": first.headers["ETag"]},
        )
        assert current.status_code == 304
        # The tag a client got before the default branch tagged phone (no tags then): its
        # body showed the phone; it must not be revalidated now.
        before = entity_tag(CacheKey(DOMAIN, head, f"{BASE}{RECORD}", (("branch", STALE),), ""))
        stale = await http.get(
            BASE + RECORD,
            params={"branch": STALE},
            headers={"Authorization": f"Bearer {key}", "If-None-Match": before},
        )
        assert stale.status_code == 200


async def test_sensitive_reads_are_audited_whatever_the_sampling(seeded: Seeded) -> None:
    facts = seeded.facts
    section = _section()
    section["auth"]["audit"] = {"reads": {"enabled": True, "sample_rate": 0}}
    async with keeper(seeded.app_url, section, vault=VAULT) as http:
        # the second read of the viewer is answered from the response cache
        for name in ("viewer", "viewer", "cleared"):
            _values(await _get(http, facts[name], RECORD))
        branches = await _get(http, facts["viewer"], "/branches")
        assert branches.status_code == 200, branches.text  # no values: sampled out
    async with owner_session(seeded.owner_url) as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        rows = (
            await session.execute(
                select(AuditReadEntry.domain_id, AuditReadEntry.details).order_by(
                    AuditReadEntry.seq
                )
            )
        ).all()
    # the pii reader sees the pii values; a confidential Attribute stays masked for it
    expected = [["masked"], ["masked"], ["masked", "shown"]]
    assert [row.details["sensitive"] for row in rows] == expected
    assert {row.domain_id for row in rows} == {DOMAIN}
    assert not any(secret in str(rows) for secret in SECRETS)
