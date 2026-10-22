"""Domains, branches and tags through the API against Postgres, as a non-superuser role."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from lorebase_store.db import create_engine
from lorebase_store.jobs import BRANCH_DELETE_JOB, MATERIALIZE_JOB, JobRow
from lorebase_store.materialize import run_branch_delete_job, run_materialize_job
from lorebase_store.runtime import StoreRuntime
from lorebase_store.settings import DatabaseSettings, MaterializeSettings, StoreSettings

from .test_operations_postgres import csv_rows, execute, supply_yaml

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
        # an installation admin is told the Domain is gone; the viewer, whose grant went with
        # it, is not told whether it ever existed
        gone = await http.get(DOMAIN, headers=admin)
        assert gone.status_code == 404
        assert gone.json()["code"] == "not-found"
        assert (await http.get(DOMAIN, headers=viewer)).status_code == 403
        assert (
            await http.delete(DOMAIN, headers=admin, params={"confirm": "supply"})
        ).status_code == 404

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


def _job(job_type: str, branch: str) -> JobRow:
    now = datetime.now(UTC)
    return JobRow(
        id=0,
        type=job_type,
        domain_id="supply",
        payload={"branch": branch},
        priority=0,
        attempts=1,
        run_after=now,
        locked_by="test",
        created_at=now,
    )


async def test_large_copies_and_deletions_run_in_the_background(
    keeper: Any, installation_admin: Any, role_key: Any, database_url: str
) -> None:
    """A copy above `async_copy_min_rows` answers at once as an overlay with a pending copy;
    a deletion above `async_delete_min_rows` hides the branch at once."""
    materialize = {"async_copy_min_rows": 1, "async_delete_min_rows": 1, "job_batch_rows": 2}
    runtime = StoreRuntime.from_settings(
        StoreSettings(materialize=MaterializeSettings(**materialize))
    )
    admin = installation_admin.headers
    engine = create_engine(DatabaseSettings(), url=database_url, pooled=False)
    try:
        async with keeper({"store": {"materialize": materialize}}) as http:
            await http.post(f"{API}/domains", json={"id": "supply"}, headers=admin)
            editor = await role_key(http, "supply", "editor")
            modeler = await role_key(http, "supply", "modeler")
            viewer = await role_key(http, "supply", "viewer")
            await http.post(f"{DOMAIN}/branches", json={"name": "load"}, headers=editor)
            applied = await http.post(
                f"{DOMAIN}/branches/load/schema/changes",
                json={"yaml": supply_yaml()},
                headers=modeler,
            )
            assert applied.status_code == 200, applied.text
            loaded = await http.post(
                execute("import_records"),
                json={
                    "branch": "load",
                    "parameters": {"kind": "Supplier", "rows": csv_rows("supplier")},
                },
                headers=editor,
            )
            assert loaded.status_code == 200, loaded.text

            created = await http.post(
                f"{DOMAIN}/branches",
                json={"name": "big", "source": "load", "mode": "copy"},
                headers=editor,
            )
            assert created.status_code == 201, created.text
            body = created.json()
            assert (body["materialization"], body["materialization_status"]) == (
                "overlay",
                "pending",
            )
            assert body["materialization_progress"]["phase"] == "records"
            listed = (await http.get(f"{DOMAIN}/branches", headers=viewer)).json()["items"]
            assert {b["name"]: b["materialization_status"] for b in listed} == {
                "big": "pending",
                "load": None,
                "main": None,
            }

            result = await run_materialize_job(
                engine, _job(MATERIALIZE_JOB, "big"), runtime=runtime
            )
            assert result["promoted"]
            done = (await http.get(f"{DOMAIN}/branches/big", headers=viewer)).json()
            assert (done["materialization"], done["materialization_status"]) == ("full", "done")
            progress = done["materialization_progress"]
            assert progress["phase"] == "done"
            assert progress["rows_done"] == progress["rows_total"] > 0

            assert (await http.delete(f"{DOMAIN}/branches/big", headers=editor)).status_code == 204
            assert (await http.get(f"{DOMAIN}/branches/big", headers=viewer)).status_code == 404
            names = [
                b["name"]
                for b in (await http.get(f"{DOMAIN}/branches", headers=viewer)).json()["items"]
            ]
            assert names == ["load", "main"]
            taken = await http.post(f"{DOMAIN}/branches", json={"name": "big"}, headers=editor)
            assert taken.status_code == 409
            assert taken.json()["code"] == "already-exists"
            assert "Retry-After" in taken.headers

            deleted = await run_branch_delete_job(
                engine, _job(BRANCH_DELETE_JOB, "big"), runtime=runtime
            )
            assert deleted["status"] == "done"
            again = await http.post(f"{DOMAIN}/branches", json={"name": "big"}, headers=editor)
            assert again.status_code == 201, again.text
    finally:
        await engine.dispose()
