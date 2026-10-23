"""Sensitive values in reads and the vault through the API (design 10.2, 13.5).

The `supply` Supplier Kind gains `email` (pii, stored in the vault), `phone` (pii) and
`nickname` (pii, searchable). A viewer sees them masked on every read path; a caller with
`pii.read` (Domain-wide or on the Kind) sees them; filters on them need the clearance; erasure
reaches every Revision and is audited.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import yaml
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
NOTES, REMARK = "att_01JB8ZSENS00000000000000J1", "att_01JB8ZSENS00000000000000R1"
EMAIL = "att_01JB8ZSENS00000000000000E1"
OTHER, CONFLICTING = "S-1003", "conflicting"
BRANCH_EMAIL, MAIN_EMAIL = "branch.person@example.com", "main.person@example.com"


async def _email_token(session: AsyncSession, kind_id: str, key: str) -> str:
    """The vault token the head of `main` holds for the email of a Supplier."""
    found = await session.execute(
        text(
            """SELECT "values" -> :a ->> '$vault' FROM mat_records """
            "WHERE domain_id = :d AND branch = 'main' AND kind_id = :kind AND key = :k"
        ),
        {"d": DOMAIN, "k": key, "a": EMAIL, "kind": kind_id},
    )
    token: str = found.scalar_one()
    return token


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
        # Plain Attributes: free notes, and a remark that will hold text shaped like a
        # vault reference.
        NOTES: _attribute(NOTES, "notes").model_copy(
            update={"sensitivity": (), "type": TypeSpec.of("json")}
        ),
        REMARK: _attribute(REMARK, "remark").model_copy(update={"sensitivity": ()}),
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

    def change(email: str, **more: str) -> RecordUpsert:
        return record_change(
            supplier, {**original, "email": email, "phone": PHONE, "nickname": NICKNAME, **more}
        )

    first = await commit_changes(session, runtime, clock, [change(OLD_EMAIL)], message="Contact")
    # A branch forked before phone is tagged: its index still holds the phone's words.
    await create_branch(session, DOMAIN, STALE, created_by="usr_seed", runtime=runtime, clock=clock)
    phone = "att_01JB8ZSENS00000000000000P1"
    tagged = supplier.attributes[phone].model_copy(update={"sensitivity": PII})
    supplier = supplier.model_copy(update={"attributes": {**supplier.attributes, phone: tagged}})
    await change_schema(supplier, "Phone numbers are personal data")
    second = await commit_changes(session, runtime, clock, [change(NEW_EMAIL)], message="Moved")
    # A plain string that looks like the reference of the email: text, never a reference.
    async with session.begin():
        token = await _email_token(session, supplier.id, KEY)
    forged = json.dumps({"$vault": token})
    await commit_changes(
        session, runtime, clock, [change(NEW_EMAIL, remark=forged)], message="Remark"
    )
    # Two branches give another Supplier different emails: a Conflict over vault values.
    other = next(r for r in rows("supplier.csv") if r["supplier_code"] == OTHER)
    await create_branch(
        session, DOMAIN, CONFLICTING, created_by="usr_seed", runtime=runtime, clock=clock
    )
    for branch, email in ((CONFLICTING, BRANCH_EMAIL), ("main", MAIN_EMAIL)):
        await commit_changes(
            session,
            runtime,
            clock,
            [record_change(supplier, {**other, "email": email})],
            message="Other contact",
            branch=branch,
        )
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
        kind_viewer = await _principal(
            store,
            "kind viewer",
            {"role": "viewer", "scope_type": GrantScope.KIND, "scope_ref": "Supplier"},
        )
        part_viewer = await _principal(
            store,
            "part viewer",
            {"role": "viewer", "scope_type": GrantScope.KIND, "scope_ref": "Part"},
        )
        admin = await _principal(store, "admin", {"role": "admin"})
        editor = await _principal(store, "editor", {"role": "editor"})
        modeler = await _principal(store, "modeler", {"role": "modeler"})
        stale_modeler = await _principal(
            store,
            "stale modeler",
            {"role": "branch-modeler", "scope_type": GrantScope.BRANCH, "scope_ref": STALE},
        )
    finally:
        await engine.dispose()
    seeded = Seeded(owner_url=owner_url, app_url=app_url, schema=schema)
    seeded.revisions = {"first": first.revision_id.hex(), "second": second.revision_id.hex()}
    seeded.facts = {
        "viewer": viewer,
        "cleared": cleared,
        "kind": kind_cleared,
        "kind_viewer": kind_viewer,
        "part_viewer": part_viewer,
        "admin": admin,
        "editor": editor,
        "modeler": modeler,
        "stale_modeler": stale_modeler,
        "token": token,
        "forged": forged,
    }
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
                    "branch-modeler": ["schema.write"],
                }
            },
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


async def test_lineage_masks_values_for_a_reader_narrowed_to_the_kind(seeded: Seeded) -> None:
    facts = seeded.facts
    revs = seeded.revisions
    lineage = f"{RECORD}/lineage"
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        for at in (None, revs["first"]):
            params = {} if at is None else {"at": at}
            response = await _get(http, facts["kind_viewer"], lineage, **params)
            _hidden(response)
            body = response.json()
            email = next(a for a in body["attributes"] if a["name"] == "email")
            assert email["value"] == "***"
            assert email["revision"] is not None
            assert email["provenance"] is not None
            # The creating change names the Attributes it set, never their values.
            created = body["created"]
            assert created is not None
            assert created["attributes"]
            assert all(a["before"] is None and a["after"] is None for a in created["attributes"])
            # A grant narrowed to another Kind reads no lineage of this one: the Kind is
            # invisible to it (design 13.3).
            refused = await _get(http, facts["part_viewer"], lineage, **params)
            assert refused.status_code == 404, refused.text
        cleared = await _get(http, facts["kind"], lineage)
        email = next(a for a in cleared.json()["attributes"] if a["name"] == "email")
        assert email["value"] == NEW_EMAIL


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


async def _diff_lines(http: httpx.AsyncClient, key: str, revs: dict[str, str]) -> httpx.Response:
    params = {"from": revs["first"], "to": revs["second"], "format": "ndjson"}
    response = await _get(http, key, "/diff", **params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return response


async def _dry_update(http: httpx.AsyncClient, key: str, **values: Any) -> httpx.Response:
    body = {"parameters": {"key": KEY, **values}, "dry_run": True}
    response = await _post(http, key, "/operations/update_supplier/execute", body)
    assert response.status_code == 200, response.text
    return response


def _unremarked(response: httpx.Response, facts: dict[str, Any]) -> str:
    """The body without the remark, which holds text shaped like a reference on purpose."""
    return response.text.replace(json.dumps(facts["forged"])[1:-1], "")


def _email_of_edit(response: httpx.Response) -> Any:
    (edit,) = response.json()["edits"]
    return edit["values"]["email"]


async def test_streamed_diffs_and_operation_edits_show_values_not_references(
    seeded: Seeded,
) -> None:
    facts = seeded.facts
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        cleared = await _diff_lines(http, facts["cleared"], seeded.revisions)
        assert NEW_EMAIL in cleared.text
        assert OLD_EMAIL in cleared.text
        assert "vlt_" not in _unremarked(cleared, facts)
        viewer = await _diff_lines(http, facts["viewer"], seeded.revisions)
        assert "vlt_" not in viewer.text
        assert not any(secret in viewer.text for secret in SECRETS)

        # an update's edit carries the Record's current values, the vault ones included
        shown = await _dry_update(http, facts["admin"], phone="555 0199")
        assert _email_of_edit(shown) == NEW_EMAIL
        assert "vlt_" not in _unremarked(shown, facts)
        masked = await _dry_update(http, facts["editor"], phone="555 0199")
        assert _email_of_edit(masked) == "***"
        assert "vlt_" not in _unremarked(masked, facts)
        assert NEW_EMAIL not in masked.text


async def test_violations_on_unreadable_values_do_not_tell_what_is_wrong(seeded: Seeded) -> None:
    taken = next(r for r in rows("supplier.csv") if r["supplier_code"] != KEY)["vat_number"]
    facts = seeded.facts
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        # vat_number is unique and confidential: telling an uncleared caller that a value is
        # taken would confirm another Supplier's VAT number
        admin = await _dry_update(http, facts["admin"], vat_number=taken)
        editor = await _dry_update(http, facts["editor"], vat_number=taken)
    (shown,) = admin.json()["violations"]
    (hidden,) = editor.json()["violations"]
    assert shown["code"] == "value.unique"
    assert hidden["code"] == "value.invalid"
    assert hidden["message"] == "'vat_number' is not valid"
    assert (hidden["path"], hidden.get("key"), hidden.get("attribute")) == (
        shown["path"],
        shown.get("key"),
        shown.get("attribute"),
    )


async def test_a_reference_nested_in_an_edit_is_refused_not_opened(seeded: Seeded) -> None:
    facts = seeded.facts
    nested = {"copy": {"$vault": facts["token"]}}
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        dry = await _dry_update(http, facts["admin"], notes=nested)
    assert [v["code"] for v in dry.json()["violations"]] == ["value.vault_reference"]
    (edit,) = dry.json()["edits"]
    assert edit["values"]["notes"] == nested  # shown as written, never opened
    async with owner_session(seeded.owner_url) as session, session.begin():
        await session.execute(text("SELECT set_config('lorebase.bypass_rls', 'on', true)"))
        entries = (
            await session.execute(
                select(AuditEntry.details).where(AuditEntry.action == "operation.dry_run")
            )
        ).scalars()
        noted = [entry.get("sensitive") for entry in entries]
    # the dry run answered with values of the Record: the audit entry says so
    assert ["masked", "shown"] in noted or ["shown"] in noted


async def test_revalidating_unreadable_values_tells_only_how_many_fail(seeded: Seeded) -> None:
    facts = seeded.facts
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        current = await _get(http, facts["modeler"], "/schema", format="yaml")
        assert current.status_code == 200, current.text
        document = yaml.safe_load(current.text)
        phone = document["kinds"]["Supplier"]["attributes"]["phone"]
        phone["constraints"] = {"pattern": "^[a-z]+$"}
        body = {"yaml": yaml.safe_dump(document), "dry_run": True}
        path = "/branches/main/schema/changes"
        shown = await _post(http, facts["admin"], path, body)
        hidden = await _post(http, facts["modeler"], path, body)
    assert shown.status_code == hidden.status_code == 200, (shown.text, hidden.text)
    phone = [i for i in shown.json()["issues"] if i.get("attribute") == "phone"]
    assert [(i["code"], i["key"]) for i in phone] == [("value.pattern", KEY)]
    (only,) = [i for i in hidden.json()["issues"] if i.get("attribute") == "phone"]
    assert only["code"] == "value.invalid"
    assert only["details"] == {"count": 1}
    assert "key" not in only
    assert only["path"] == []
    assert KEY not in hidden.text


async def test_text_shaped_like_a_reference_stays_text(seeded: Seeded) -> None:
    facts = seeded.facts
    token = facts["token"]
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        explained = await _get(http, facts["cleared"], f"{RECORD}/explain", format="markdown")
        pack = await _post(
            http,
            facts["cleared"],
            "/context",
            {"refs": [f"Supplier/{KEY}"], "format": "markdown"},
        )
        record = _values(await _get(http, facts["cleared"], RECORD))
    assert record["remark"] == facts["forged"]
    for response in (explained, pack):
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/markdown")
        assert token in response.text  # the remark, as stored
        assert NEW_EMAIL in response.text  # the email itself, opened for this caller
        assert "LBVAULT" not in response.text


async def test_conflicts_over_vault_values_show_values_not_references(seeded: Seeded) -> None:
    facts = seeded.facts
    body = {"source_ref": CONFLICTING, "target_ref": "main", "title": "Other contact"}
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        created = await _post(http, facts["admin"], "/proposals", body)
        assert created.status_code == 201, created.text
        path = f"/proposals/{created.json()['id']}/conflicts"
        cleared = await _get(http, facts["admin"], path)
        viewer = await _get(http, facts["viewer"], path)
    assert cleared.status_code == 200, cleared.text
    (conflict,) = cleared.json()["conflicts"]
    assert {conflict["ours"], conflict["theirs"]} == {BRANCH_EMAIL, MAIN_EMAIL}
    assert "vlt_" not in cleared.text
    assert viewer.status_code == 200, viewer.text
    assert "vlt_" not in viewer.text
    assert BRANCH_EMAIL not in viewer.text
    assert MAIN_EMAIL not in viewer.text


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
        streamed = await _diff_lines(http, facts["cleared"], seeded.revisions)
        assert "[erased]" in streamed.text
        assert "vlt_" not in streamed.text
        assert (
            _email_of_edit(await _dry_update(http, facts["admin"], phone="555 0177")) == "[erased]"
        )
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


async def test_a_branch_forked_before_a_tag_takes_the_default_branch_tags(
    seeded: Seeded,
) -> None:
    facts = seeded.facts
    path = f"/branches/{STALE}/rebase-tags"
    async with keeper(seeded.app_url, _section(), vault=VAULT) as http:
        refused = await _post(http, facts["viewer"], path, {})
        assert refused.status_code == 403, refused.text
        admin = facts["admin"]
        planned = await _post(http, admin, path, {"dry_run": True})
        assert planned.status_code == 200, planned.text
        body = planned.json()
        assert (body["status"], body["revision"]) == ("dry_run", None)
        assert body["tags"] == [
            {"section": "kinds", "owner": "Supplier", "attribute": "phone", "tags": ["pii"]}
        ]
        # A writer of the branch alone is not told what the default branch tags.
        modeled = await _post(http, facts["stale_modeler"], path, {"dry_run": True})
        assert modeled.status_code == 200, modeled.text
        assert (modeled.json()["change_count"], modeled.json()["tags"]) == (1, [])
        queued = await _post(http, admin, path, {"rows_only": True})
        assert queued.status_code == 200, queued.text
        assert queued.json()["status"] == "queued"
        assert isinstance(queued.json()["job"], int)
        done = await _post(http, admin, path, {"message": "Take the tags"})
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "committed"
        schema = await _get(http, admin, "/schema/kinds/Supplier", branch=STALE)
        phone = next(a for a in schema.json()["attributes"].values() if a["api_name"] == "phone")
        assert phone["sensitivity"] == ["pii"]
        assert (await _post(http, admin, path, {})).json()["status"] == "no_op"
        # The protected default branch has nothing to take.
        on_main = await _post(http, admin, "/branches/main/rebase-tags", {})
        assert on_main.json()["status"] == "no_op"
        # The stale branch's index no longer holds the phone's words.
        on_stale = await _post(http, facts["viewer"], "/search", {"q": "0100", "branch": STALE})
        _hidden(on_stale)
        assert on_stale.json()["items"] == []
