"""Domains, branches and tags through the API against Postgres, as a non-superuser role."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from lorebase_store.db import create_engine
from lorebase_store.settings import DatabaseSettings

pytestmark = pytest.mark.integration

API = "/api/v1"
DOMAIN = f"{API}/domains/supply"


async def _count(database_url: str, sql: str) -> int:
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with engine.connect() as connection:
            return int((await connection.execute(text(sql))).scalar_one())
    finally:
        await engine.dispose()


async def test_domain_lifecycle(
    keeper: Any, installation_admin: Any, role_key: Any, database_url: str
) -> None:
    admin = installation_admin.headers
    async with keeper() as http:
        created = await http.post(
            f"{API}/domains",
            json={"id": "supply", "display_name": "Supply"},
            headers={**admin, "Idempotency-Key": "create-supply"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["default_branch"] == "main"
        assert len(body["head"]) == 64
        assert created.headers["location"] == DOMAIN

        retried = await http.post(
            f"{API}/domains",
            json={"id": "supply", "display_name": "Supply"},
            headers={**admin, "Idempotency-Key": "create-supply"},
        )
        assert retried.status_code == 200
        assert retried.headers["idempotent-replayed"] == "true"
        assert retried.json()["head"] == body["head"]
        duplicate = await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "already-exists"

        viewer = await role_key(http, "supply", "viewer")
        assert (await http.get(DOMAIN, headers=viewer)).json()["display_name"] == "Supply"
        listed = (await http.get(f"{API}/domains", headers=viewer)).json()
        assert [d["id"] for d in listed["items"]] == ["supply"]
        forbidden = await http.post(f"{API}/domains", json={"id": "other"}, headers=viewer)
        assert forbidden.status_code == 403
        assert forbidden.json()["permission"] == "domain.create"

        patched = await http.patch(DOMAIN, json={"description": "Parts."}, headers=admin)
        assert patched.status_code == 200
        assert patched.json()["description"] == "Parts."
        assert (
            await http.patch(DOMAIN, json={"description": "x"}, headers=viewer)
        ).status_code == 403

        assert (
            await http.delete(DOMAIN, headers=viewer, params={"confirm": "supply"})
        ).status_code == 403
        assert (
            await http.delete(DOMAIN, headers=admin, params={"confirm": "nope"})
        ).status_code == 422
        assert (
            await http.delete(DOMAIN, headers=admin, params={"confirm": "supply"})
        ).status_code == 204
        # the creator's grant went with the Domain: nothing tells whether it existed
        assert (await http.get(DOMAIN, headers=admin)).status_code == 403

    assert await _count(database_url, "SELECT count(*) FROM revisions") == 0
    actions = await _count(
        database_url,
        "SELECT count(*) FROM audit_log WHERE action IN ('domain.create', 'domain.delete')",
    )
    assert actions == 3  # the create, its idempotent retry and the delete


async def test_branch_lifecycle(keeper: Any, installation_admin: Any, role_key: Any) -> None:
    admin = installation_admin.headers
    async with keeper() as http:
        assert (
            await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
        ).status_code == 201
        editor = await role_key(http, "supply", "editor")
        viewer = await role_key(http, "supply", "viewer")

        main = (await http.get(f"{DOMAIN}/branches/main", headers=viewer)).json()
        assert main["protected"] is True
        assert main["default"] is True
        assert (main["ahead"], main["behind"]) == (0, 0)

        denied = await http.post(f"{DOMAIN}/branches", json={"name": "work/a"}, headers=viewer)
        assert denied.status_code == 403
        created = await http.post(
            f"{DOMAIN}/branches",
            json={"name": "work/a"},
            headers={**editor, "Idempotency-Key": "branch-a"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["head"] == main["head"]
        assert created.json()["protected"] is False
        again = await http.post(
            f"{DOMAIN}/branches",
            json={"name": "work/a"},
            headers={**editor, "Idempotency-Key": "branch-a"},
        )
        assert again.status_code == 200
        conflict = await http.post(f"{DOMAIN}/branches", json={"name": "work/a"}, headers=editor)
        assert conflict.status_code == 409

        bad = await http.post(f"{DOMAIN}/branches", json={"name": "a b"}, headers=editor)
        assert bad.status_code == 422
        unknown = await http.post(
            f"{DOMAIN}/branches", json={"name": "x", "revision": "0" * 64}, headers=editor
        )
        assert unknown.status_code == 404

        detail = await http.get(f"{DOMAIN}/branches/work%2Fa", headers=viewer)
        assert detail.status_code == 200
        assert detail.json()["name"] == "work/a"
        names = [
            b["name"]
            for b in (await http.get(f"{DOMAIN}/branches", headers=viewer)).json()["items"]
        ]
        assert names == ["main", "work/a"]

        tag = await http.post(f"{DOMAIN}/tags", json={"name": "v1"}, headers=editor)
        assert tag.status_code == 201
        assert tag.json()["revision"] == main["head"]
        tags = (await http.get(f"{DOMAIN}/tags", headers=viewer)).json()["items"]
        assert [t["name"] for t in tags] == ["v1"]
        from_tag = await http.post(
            f"{DOMAIN}/branches", json={"name": "from-tag", "source": "v1"}, headers=editor
        )
        assert from_tag.status_code == 201

        assert (await http.delete(f"{DOMAIN}/branches/main", headers=admin)).status_code == 409
        protect = await http.patch(
            f"{DOMAIN}/branches/work/a", json={"protected": True}, headers=editor
        )
        assert protect.status_code == 403
        protect = await http.patch(
            f"{DOMAIN}/branches/work/a", json={"protected": True}, headers=admin
        )
        assert protect.json()["protected"] is True
        assert (await http.delete(f"{DOMAIN}/branches/work/a", headers=editor)).status_code == 409
        await http.patch(f"{DOMAIN}/branches/work/a", json={"protected": False}, headers=admin)
        assert (await http.delete(f"{DOMAIN}/branches/work/a", headers=editor)).status_code == 204
        assert (await http.get(f"{DOMAIN}/branches/work/a", headers=viewer)).status_code == 404


async def test_materialized_branch_cap(keeper: Any, installation_admin: Any) -> None:
    admin = installation_admin.headers
    async with keeper(branches={"max_materialized_per_domain": 2}) as http:
        await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
        assert (
            await http.post(f"{DOMAIN}/branches", json={"name": "b1"}, headers=admin)
        ).status_code == 201
        capped = await http.post(f"{DOMAIN}/branches", json={"name": "b2"}, headers=admin)
        assert capped.status_code == 409
        assert capped.json()["code"] == "limit-exceeded"
