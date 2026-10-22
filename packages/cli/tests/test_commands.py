"""Commands through Typer's CliRunner, against a mocked server.

The HTTP layer is mocked with respx (the same tool as the client tests), so the real client
runs: the tests check the requests the commands make, the text they print and the exit codes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from lorebase_cli.main import app

API = "http://keeper.test/api/v1"
REV = "ab" * 32
REV2 = "cd" * 32
NOW = "2026-10-01T08:00:00Z"
PROBLEM = "application/problem+json"
runner = CliRunner()


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture(autouse=True)
def server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE_URL", "http://keeper.test")
    monkeypatch.setenv("LOREBASE_API_KEY", "lbk_test")


def run(*args: str, input_: str | None = None) -> Result:
    return runner.invoke(app, list(args), input=input_)


def problem(status: int, code: str, detail: str = "went wrong", **members: Any) -> httpx.Response:
    body = {
        "type": f"https://lorebase.dev/problems/{code}",
        "title": code,
        "status": status,
        "code": code,
        "detail": detail,
        "request_id": "req-9",
        **members,
    }
    return httpx.Response(status, json=body, headers={"content-type": PROBLEM})


def domain(domain_id: str = "supply", **extra: Any) -> dict[str, Any]:
    return {
        "id": domain_id,
        "display_name": "Supply chain",
        "description": "",
        "default_branch": "main",
        "settings": {},
        "created_at": NOW,
        "created_by": "usr_1",
        "head": REV,
        **extra,
    }


def branch(name: str = "work", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "head": REV,
        "protected": False,
        "default": name == "main",
        "materialization": "full",
        "version": 1,
        "created_at": NOW,
        "created_by": "usr_1",
        **extra,
    }


def execution(**extra: Any) -> dict[str, Any]:
    return {
        "status": "committed",
        "operation": "import_records",
        "branch": "work",
        "invocation_id": "inv_1",
        "parameters_hash": "ff",
        "head": REV,
        "revision": REV2,
        "revisions": [REV2],
        "edits": [],
        "edit_count": 2,
        "edits_truncated": False,
        "violations": [],
        "violation_count": 0,
        "warnings": [],
        "review_required": False,
        "replayed": False,
        **extra,
    }


SCHEMA_DOC = {
    "domain": "supply",
    "branch": "work",
    "revision": REV,
    "schema": {
        "kinds": {
            "k1": {
                "api_name": "Part",
                "primary_key": ["a1"],
                "attributes": {
                    "a1": {"api_name": "part_number", "type": {"name": "string"}},
                    "a2": {"api_name": "hazardous", "type": {"name": "boolean"}},
                    "a3": {"api_name": "stock", "type": {"name": "integer"}},
                },
            }
        },
        "relations": {
            "r1": {
                "api_name": "supplied_by",
                "attributes": {"a4": {"api_name": "is_primary", "type": {"name": "boolean"}}},
            }
        },
    },
}


def request_bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


# Configuration and failures -----------------------------------------------------------------


def test_no_server_configured_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOREBASE_URL")
    result = run("whoami")
    assert result.exit_code == 1
    assert "LOREBASE_URL" in result.stderr


def test_usage_errors_exit_with_one() -> None:
    assert run("domain", "create").exit_code == 1  # missing argument
    assert run("nonsense").exit_code == 1
    assert run("record", "list", "Part", "--limit", "0").exit_code == 1


def test_no_arguments_prints_help() -> None:
    result = run()
    assert "Commands" in result.stdout
    assert "Exit codes" in result.stdout


def test_version() -> None:
    result = run("--version")
    assert result.exit_code == 0
    assert result.stdout.startswith("lorebase 0.1.0")


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (problem(401, "unauthorized", "No valid credential."), 3),
        (problem(403, "forbidden", "Missing permission data.read.", permission="data.read"), 3),
        (problem(404, "not-found", "No such Domain."), 1),
        (problem(409, "conflict", "Exists."), 2),
        (
            problem(
                422,
                "validation-failed",
                "Invalid.",
                errors=[{"pointer": "/x", "message": "m", "code": "c"}],
            ),
            2,
        ),
        (problem(500, "internal-error", "Boom."), 4),
        (httpx.Response(502, text="Bad gateway"), 4),
    ],
)
def test_failures_print_the_problem_and_exit_by_class(
    http: respx.MockRouter, response: httpx.Response, code: int
) -> None:
    http.get("/domains/supply").mock(return_value=response)
    result = run("domain", "get", "supply")
    assert result.exit_code == code
    assert result.stdout == ""
    assert result.stderr.startswith("error: ")


def test_failure_details_reach_the_person(http: respx.MockRouter) -> None:
    http.get("/domains/supply").mock(
        return_value=problem(
            403,
            "forbidden",
            "Missing permission data.read in Domain supply.",
            permission="data.read",
        )
    )
    result = run("domain", "get", "supply")
    assert "Missing permission data.read in Domain supply." in result.stderr
    assert "request id: req-9" in result.stderr
    http.get("/domains/supply").mock(
        return_value=problem(
            409, "stale-head", "Branch moved.", branch="work", expected_head="aa", current_head="bb"
        )
    )
    assert "now at bb" in run("domain", "get", "supply").stderr


def test_json_mode_reports_failures_as_json_on_standard_error(http: respx.MockRouter) -> None:
    http.get("/domains/supply").mock(return_value=problem(404, "not-found", "No such Domain."))
    result = run("--json", "domain", "get", "supply")
    assert result.exit_code == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert (error["status"], error["code"], error["request_id"]) == (404, "not-found", "req-9")
    flag_after = run("domain", "get", "supply", "--json")
    assert json.loads(flag_after.stderr)["error"]["status"] == 404


def test_unreachable_server_exits_with_four(http: respx.MockRouter) -> None:
    http.get("/auth/whoami").mock(side_effect=httpx.ConnectError("refused"))
    result = run("whoami")
    assert result.exit_code == 4
    assert "cannot reach the server" in result.stderr


def test_invalid_arguments_for_the_server_are_usage_errors(http: respx.MockRouter) -> None:
    result = run("domain", "create", "Not A Slug")
    assert result.exit_code == 1
    assert "invalid arguments" in result.stderr
    assert http.calls.call_count == 0


# Domains, branches, tags --------------------------------------------------------------------


def test_whoami(http: respx.MockRouter) -> None:
    http.get("/auth/whoami").respond(
        200,
        json={
            "id": "usr_1",
            "type": "user",
            "display_name": "Ada",
            "method": "api_key",
            "subjects": ["usr_1"],
            "key_scopes": [],
            "installation_admin": True,
        },
    )
    result = run("whoami")
    assert result.exit_code == 0
    assert "Ada" in result.stdout
    assert "installation admin  true" in result.stdout
    assert json.loads(run("whoami", "--json").stdout)["id"] == "usr_1"


def test_whoami_shows_the_quota_of_an_api_key(http: respx.MockRouter) -> None:
    http.get("/auth/whoami").respond(
        200,
        json={
            "id": "svc_1",
            "type": "service",
            "display_name": "Bot",
            "method": "api_key",
            "subjects": ["svc_1"],
            "key_scopes": [],
            "installation_admin": False,
            "quota": {
                "period": "day",
                "resets_at": "2026-10-21T00:00:00Z",
                "requests": {"limit": 1000, "used": 12, "remaining": 988},
                "context_tokens": {"limit": None, "used": 40, "remaining": None},
            },
        },
    )
    result = run("whoami")
    assert result.exit_code == 0
    assert "requests today" in result.stdout
    assert "12 of 1000" in result.stdout
    assert "40 of unlimited" in result.stdout
    assert json.loads(run("whoami", "--json").stdout)["quota"]["requests"]["remaining"] == 988


def test_domain_create_list_get(http: respx.MockRouter) -> None:
    create = http.post("/domains").respond(201, json=domain())
    http.get("/domains").respond(
        200, json={"items": [domain("a"), domain("b")], "next_cursor": None}
    )
    http.get("/domains/supply").respond(200, json=domain())
    created = run("domain", "create", "supply", "--name", "Supply chain")
    assert created.exit_code == 0
    assert request_bodies(create)[0]["id"] == "supply"
    assert request_bodies(create)[0]["display_name"] == "Supply chain"
    assert "default_branch" not in request_bodies(create)[0]  # left to the server default
    assert "idempotency-key" not in create.calls.last.request.headers
    listed = run("domain", "list")
    assert listed.stdout.splitlines()[0].split() == ["id", "name", "default", "branch", "created"]
    assert len(listed.stdout.splitlines()) == 3
    as_json = json.loads(run("domain", "list", "--json").stdout)
    assert [d["id"] for d in as_json] == ["a", "b"]
    assert "supply" in run("domain", "get", "supply").stdout


def test_listings_stop_at_the_configured_limit_unless_asked(http: respx.MockRouter) -> None:
    items = [domain(f"d{i}") for i in range(5)]
    http.get("/domains").respond(200, json={"items": items, "next_cursor": None})
    capped = run("domain", "list", "--limit", "2")
    assert len(capped.stdout.splitlines()) == 3
    assert "showing the first 2 items" in capped.stderr
    assert len(run("domain", "list", "--all").stdout.splitlines()) == 6
    assert json.loads(run("domain", "list", "-n", "3", "--json").stdout).__len__() == 3


def test_domain_delete_asks_for_confirmation(http: respx.MockRouter) -> None:
    delete = http.delete("/domains/supply").respond(204)
    refused = run("domain", "delete", "supply")
    assert refused.exit_code == 1
    assert "--yes" in refused.stderr
    assert delete.call_count == 0
    done = run("domain", "delete", "supply", "--yes")
    assert done.exit_code == 0
    assert delete.calls.last.request.url.params["confirm"] == "supply"


def test_branch_commands(http: respx.MockRouter) -> None:
    http.get("/domains/supply/branches").respond(
        200, json={"items": [branch("main", protected=True), branch("work")], "next_cursor": None}
    )
    create = http.post("/domains/supply/branches").respond(201, json=branch("agent/x"))
    protect = http.patch("/domains/supply/branches/work").respond(200, json=branch(protected=True))
    delete = http.delete("/domains/supply/branches/agent/x").respond(204)
    http.get("/domains/supply/branches/work").respond(200, json=branch(ahead=3, behind=1))
    listing = run("branch", "list", "-d", "supply")
    assert "main" in listing.stdout
    assert "abababababab" in listing.stdout  # heads are cut to twelve characters
    assert run("branch", "create", "agent/x", "--source", "work", "-d", "supply").exit_code == 0
    assert request_bodies(create) == [{"name": "agent/x", "source": "work"}]
    assert run("branch", "create", "ov", "--mode", "overlay", "-d", "supply").exit_code == 0
    assert request_bodies(create)[1] == {"name": "ov", "mode": "overlay"}
    assert run("branch", "create", "cp", "--mode", "copy", "-d", "supply").exit_code == 0
    assert request_bodies(create)[2] == {"name": "cp", "mode": "copy"}
    wrong = run("branch", "create", "bad", "--mode", "shallow", "-d", "supply")
    assert wrong.exit_code == 1
    assert "overlay" in wrong.stderr
    assert len(request_bodies(create)) == 3  # nothing was sent for the unknown mode
    assert run("branch", "protect", "work", "-d", "supply").exit_code == 0
    assert request_bodies(protect) == [{"protected": True}]
    run("branch", "protect", "work", "--off", "-d", "supply")
    assert request_bodies(protect)[1] == {"protected": False}
    assert run("branch", "delete", "agent/x", "-d", "supply").exit_code == 0
    assert delete.called
    assert "ahead" in run("branch", "get", "work", "-d", "supply").stdout


def _copy_branch(
    status: str, phase: str = "records", done: int = 0, **progress: Any
) -> dict[str, Any]:
    return branch(
        "big",
        materialization="full" if status == "done" else "overlay",
        materialization_status=status,
        materialization_progress={
            "phase": phase,
            "rows_done": done,
            "rows_total": 10,
            "error": None,
            "updated_at": NOW,
            "finished_at": None,
            **progress,
        },
    )


def test_branch_create_wait(http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    from lorebase_cli.commands import domains

    sleeps: list[float] = []
    monkeypatch.setattr(domains, "_sleep", sleeps.append)
    http.post("/domains/supply/branches").respond(201, json=_copy_branch("pending"))
    get = http.get("/domains/supply/branches/big").mock(
        side_effect=[
            httpx.Response(
                200, json={**_copy_branch("running", "links", 4), "ahead": 0, "behind": 0}
            ),
            httpx.Response(
                200, json={**_copy_branch("running", "links", 4), "ahead": 0, "behind": 0}
            ),
            httpx.Response(200, json={**_copy_branch("done", "done", 10), "ahead": 0, "behind": 0}),
        ]
    )
    done = run("branch", "create", "big", "--wait", "--poll-interval", "0.5", "-d", "supply")
    assert done.exit_code == 0
    assert get.call_count == 3
    assert sleeps == [0.5, 0.5, 0.5]
    assert done.stderr.count("links 4/10") == 1  # a note only when the progress changes
    assert "done" in done.stdout

    http.post("/domains/supply/branches").respond(201, json=branch("small"))
    assert run("branch", "create", "small", "--wait", "-d", "supply").exit_code == 0
    assert len(sleeps) == 3  # no background copy: nothing to wait for


def test_branch_create_wait_failure_and_timeout(
    http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lorebase_cli.commands import domains

    monkeypatch.setattr(domains, "_sleep", lambda _s: None)
    http.post("/domains/supply/branches").respond(201, json=_copy_branch("pending"))
    http.get("/domains/supply/branches/big").respond(
        200,
        json={**_copy_branch("failed", "links", 4, error="disk full"), "ahead": 0, "behind": 0},
    )
    failed = run("branch", "create", "big", "--wait", "-d", "supply")
    assert failed.exit_code == 1
    assert "disk full" in failed.stderr
    http.get("/domains/supply/branches/big").respond(
        200, json={**_copy_branch("running"), "ahead": 0, "behind": 0}
    )
    late = run("branch", "create", "big", "--wait", "--wait-timeout", "0", "-d", "supply")
    assert late.exit_code == 1
    assert "still being copied" in late.stderr


def test_domain_comes_from_the_environment_or_the_config_file(
    http: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route = http.get("/domains/supply/branches").respond(
        200, json={"items": [], "next_cursor": None}
    )
    missing = run("branch", "list")
    assert missing.exit_code == 1
    assert "no Domain" in missing.stderr
    monkeypatch.setenv("LOREBASE_DOMAIN", "supply")
    assert run("branch", "list").exit_code == 0
    monkeypatch.delenv("LOREBASE_DOMAIN")
    config = tmp_path / "c.toml"
    config.write_text('domain = "supply"\n')
    assert run("--config", str(config), "branch", "list").exit_code == 0
    assert route.call_count == 2


def test_tags(http: respx.MockRouter) -> None:
    tag = {"name": "v1", "revision": REV, "created_at": NOW, "created_by": "usr_1"}
    http.get("/domains/supply/tags").respond(200, json={"items": [tag], "next_cursor": None})
    create = http.post("/domains/supply/tags").respond(201, json=tag)
    assert "v1" in run("tag", "list", "-d", "supply").stdout
    assert run("tag", "create", "v1", "--source", "work", "-d", "supply").exit_code == 0
    assert request_bodies(create) == [{"name": "v1", "source": "work"}]


# Schema -------------------------------------------------------------------------------------


def test_schema_get_prints_yaml_and_json(http: respx.MockRouter, tmp_path: Path) -> None:
    yaml_route = http.get("/domains/supply/schema", params={"format": "yaml"}).respond(
        200, text="format: lorebase.schema/v1\n", headers={"content-type": "text/yaml"}
    )
    http.get("/domains/supply/schema", params={"format": "json"}).respond(200, json=SCHEMA_DOC)
    text = run("schema", "get", "-d", "supply", "-b", "work")
    assert text.stdout == "format: lorebase.schema/v1\n"
    assert yaml_route.calls.last.request.url.params["branch"] == "work"
    target = tmp_path / "out.yaml"
    run("schema", "get", "-d", "supply", "-o", str(target))
    assert target.read_text() == "format: lorebase.schema/v1\n"
    document = json.loads(run("schema", "get", "-d", "supply", "--json").stdout)
    assert document["schema"]["kinds"]["k1"]["api_name"] == "Part"  # the member keeps its name


def test_schema_validate_exits_two_when_invalid(http: respx.MockRouter, tmp_path: Path) -> None:
    document = tmp_path / "schema.yaml"
    document.write_text("format: lorebase.schema/v1\n")
    route = http.post("/domains/supply/schema:validate").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "valid": True,
                    "issues": [],
                    "issue_count": 0,
                    "changes": [
                        {"op": "create", "section": "kinds", "id": "knd_1", "api_name": "Part"}
                    ],
                },
            ),
            httpx.Response(
                200,
                json={
                    "valid": False,
                    "issues": [
                        {
                            "code": "schema.expressions_disabled",
                            "path": ["operations", "flag"],
                            "message": "expressions are not enabled",
                        }
                    ],
                    "issue_count": 3,
                    "changes": [],
                },
            ),
        ]
    )
    ok = run("schema", "validate", str(document), "-d", "supply")
    assert ok.exit_code == 0
    assert "valid: 1 element changes" in ok.stdout
    assert "Part" in ok.stdout
    assert request_bodies(route)[0]["yaml"] == "format: lorebase.schema/v1\n"
    bad = run("schema", "validate", str(document), "-d", "supply")
    assert bad.exit_code == 2
    assert "not valid: 3 issue(s)" in bad.stdout
    assert (
        "operations.flag: expressions are not enabled [schema.expressions_disabled]" in bad.stdout
    )
    assert "and 2 more" in bad.stdout


def test_schema_apply_uses_the_default_branch_when_none_is_given(
    http: respx.MockRouter, tmp_path: Path
) -> None:
    document = tmp_path / "schema.yaml"
    document.write_text("x: 1\n")
    http.get("/domains/supply").respond(200, json=domain())
    apply_main = http.post("/domains/supply/branches/main/schema/changes").respond(
        409,
        json={
            "type": "t",
            "title": "Branch is protected",
            "status": 409,
            "code": "protected-branch",
            "detail": "Branch 'main' is protected: changes reach it through a Proposal.",
            "proposals": "/api/v1/domains/supply/proposals",
        },
        headers={"content-type": PROBLEM},
    )
    refused = run("schema", "apply", str(document), "-d", "supply")
    assert refused.exit_code == 2
    assert "protected" in refused.stderr
    assert "/api/v1/domains/supply/proposals" in refused.stderr
    assert apply_main.called
    apply_work = http.post("/domains/supply/branches/work/schema/changes").respond(
        200,
        json={
            "status": "committed",
            "branch": "work",
            "head": REV,
            "revision": REV2,
            "changes": [{"op": "create", "section": "kinds", "id": "k", "api_name": "Part"}],
            "change_count": 1,
            "data_changes": 0,
            "issues": [],
            "issue_count": 0,
        },
    )
    done = run(
        "schema", "apply", str(document), "-d", "supply", "-b", "work", "-m", "Load", "--dry-run"
    )
    assert done.exit_code == 0
    assert "committed" in done.stdout
    sent = request_bodies(apply_work)[0]
    assert (sent["yaml"], sent["message"], sent["dry_run"]) == ("x: 1\n", "Load", True)


def test_schema_diff_lists_changes(http: respx.MockRouter, tmp_path: Path) -> None:
    document = tmp_path / "schema.yaml"
    document.write_text("x: 1\n")
    http.post("/domains/supply/schema:validate").respond(
        200,
        json={
            "valid": True,
            "issues": [],
            "issue_count": 0,
            "changes": [{"op": "update", "section": "kinds", "id": "k", "api_name": "Part"}],
        },
    )
    result = run("schema", "diff", str(document), "-d", "supply", "-b", "work")
    assert result.exit_code == 0
    assert result.stdout.split()[:4] == ["change", "section", "element", "id"]
    assert "update" in result.stdout


# Import -------------------------------------------------------------------------------------


def test_import_records_coerces_batches_and_reports_progress(
    http: respx.MockRouter, tmp_path: Path
) -> None:
    http.get("/domains/supply/schema").respond(200, json=SCHEMA_DOC)
    imports = http.post("/domains/supply/operations/import_records/execute").respond(
        200, json=execution()
    )
    data = tmp_path / "parts.csv"
    data.write_text("part_number,hazardous,stock,note\nP-1,true,5,x\nP-2,false,,y\nP-3,TRUE,7,z\n")
    result = run(
        "import", "records", "Part", str(data), "-d", "supply", "-b", "work",
        "--batch-size", "2", "--ignore-unknown",
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    bodies = request_bodies(imports)
    assert [b["parameters"]["rows"] for b in bodies] == [
        [
            {"part_number": "P-1", "hazardous": True, "stock": "5"},
            {"part_number": "P-2", "hazardous": False},
        ],
        [{"part_number": "P-3", "hazardous": True, "stock": "7"}],
    ]
    assert bodies[0]["branch"] == "work"
    assert bodies[0]["parameters"]["kind"] == "Part"
    assert bodies[0]["message"] == "Import Part from parts.csv (rows 1-2)"
    assert "batch 1/2: rows 1-2 committed, 2 edits" in result.stderr
    assert "batch 2/2: rows 3-3 committed" in result.stderr
    assert "rows" in result.stdout
    assert all(call.request.headers["idempotency-key"] for call in imports.calls)
    summary = json.loads(
        run(
            "import", "records", "Part", str(data), "-d", "supply", "--ignore-unknown", "--json"
        ).stdout
    )
    assert summary["rows"] == 3
    assert summary["batches"] == 1
    assert summary["revisions"] == [REV2]


def test_import_rejects_unknown_columns_and_bad_values_before_writing(
    http: respx.MockRouter, tmp_path: Path
) -> None:
    http.get("/domains/supply/schema").respond(200, json=SCHEMA_DOC)
    imports = http.post("/domains/supply/operations/import_records/execute").respond(
        200, json=execution()
    )
    unknown = tmp_path / "u.csv"
    unknown.write_text("part_number,colour\nP-1,red\n")
    result = run("import", "records", "Part", str(unknown), "-d", "supply")
    assert result.exit_code == 1
    assert "row 1: column 'colour'" in result.stderr
    renamed = run(
        "import", "records", "Part", str(unknown), "-d", "supply", "--map", "colour=stock"
    )
    assert "row 1: column 'stock'" in renamed.stderr  # "red" is not an integer
    bad = tmp_path / "b.csv"
    bad.write_text("part_number,stock\nP-1,1\nP-2,many\n")
    result = run("import", "records", "Part", str(bad), "-d", "supply", "--batch-size", "1")
    assert result.exit_code == 1
    assert "row 2: column 'stock'" in result.stderr
    assert imports.call_count == 0  # every row is checked first


def test_import_links_maps_the_csv_ends(http: respx.MockRouter, tmp_path: Path) -> None:
    http.get("/domains/supply/schema").respond(200, json=SCHEMA_DOC)
    imports = http.post("/domains/supply/operations/import_links/execute").respond(
        200, json=execution(operation="import_links")
    )
    data = tmp_path / "links.jsonl"
    data.write_text('{"from_key": "P-1", "to_key": "S-1", "is_primary": "true"}\n')
    result = run("import", "links", "supplied_by", str(data), "-d", "supply")
    assert result.exit_code == 0, result.stderr
    assert request_bodies(imports)[0]["parameters"] == {
        "relation": "supplied_by",
        "rows": [{"from": "P-1", "to": "S-1", "is_primary": True}],
    }


def test_rejected_batch_names_the_file_rows_and_what_is_committed(
    http: respx.MockRouter, tmp_path: Path
) -> None:
    http.get("/domains/supply/schema").respond(200, json=SCHEMA_DOC)
    rejected = problem(
        422,
        "validation-failed",
        "1 violation(s); the edits were not committed.",
        issues=[
            {
                "code": "parameter.invalid",
                "path": ["parameters", "rows", 1, "part_number"],
                "message": "must match ^P-[0-9]+$",
                "severity": "error",
            }
        ],
        issue_count=1,
    )

    def answer(request: httpx.Request) -> httpx.Response:
        rows = json.loads(request.content)["parameters"]["rows"]
        return (
            rejected if rows[0]["part_number"] == "P-3" else httpx.Response(200, json=execution())
        )

    http.post("/domains/supply/operations/import_records/execute").mock(side_effect=answer)
    data = tmp_path / "parts.csv"
    data.write_text("part_number\nP-1\nP-2\nP-3\nbad\n")
    result = run("import", "records", "Part", str(data), "-d", "supply", "--batch-size", "2")
    assert result.exit_code == 2
    assert "rows 3-4 were rejected; 2 earlier rows are committed" in result.stderr
    assert "rows[4].part_number: must match" in result.stderr  # row 4 of the file, 1-based
    as_json = run(
        "import", "records", "Part", str(data), "-d", "supply", "--batch-size", "2", "--json"
    )
    assert as_json.exit_code == 2
    error = json.loads(as_json.stderr)["error"]
    assert (error["first_row"], error["last_row"]) == (3, 4)


def test_import_from_standard_input_and_unknown_formats(http: respx.MockRouter) -> None:
    http.get("/domains/supply/schema").respond(200, json=SCHEMA_DOC)
    imports = http.post("/domains/supply/operations/import_records/execute").respond(
        200, json=execution()
    )
    result = run(
        "import",
        "records",
        "Part",
        "-",
        "--format",
        "csv",
        "-d",
        "supply",
        input_="part_number\nP-1\n",
    )
    assert result.exit_code == 0, result.stderr
    assert request_bodies(imports)[0]["parameters"]["rows"] == [{"part_number": "P-1"}]
    assert "Import Part from stdin" in request_bodies(imports)[0]["message"]
    assert (
        run("import", "records", "Part", "-", "-d", "supply").exit_code == 1
    )  # no suffix to go by


def test_import_parquet(http: respx.MockRouter, tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    http.get("/domains/supply/schema").respond(200, json=SCHEMA_DOC)
    imports = http.post("/domains/supply/operations/import_records/execute").respond(
        200, json=execution()
    )
    path = tmp_path / "parts.parquet"
    parquet.write_table(pa.table({"part_number": ["P-1"], "hazardous": [True], "stock": [4]}), path)
    result = run("import", "records", "Part", str(path), "-d", "supply")
    assert result.exit_code == 0, result.stderr
    assert request_bodies(imports)[0]["parameters"]["rows"] == [
        {"part_number": "P-1", "hazardous": True, "stock": "4"}
    ]


def test_import_dry_run_flag(http: respx.MockRouter, tmp_path: Path) -> None:
    http.get("/domains/supply/schema").respond(200, json=SCHEMA_DOC)
    imports = http.post("/domains/supply/operations/import_records/execute").respond(
        200, json=execution(status="dry_run", revision=None, revisions=[])
    )
    data = tmp_path / "p.csv"
    data.write_text("part_number\nP-1\n")
    result = run("import", "records", "Part", str(data), "-d", "supply", "--dry-run")
    assert result.exit_code == 0
    assert request_bodies(imports)[0]["dry_run"] is True
    assert "dry run" in result.stdout


# Reads --------------------------------------------------------------------------------------


def record(key: str = "P-1", **values: Any) -> dict[str, Any]:
    return {"kind": "Part", "key": key, "title": f"Part {key}", "values": values or {"stock": "5"}}


def test_record_get_and_list(http: respx.MockRouter) -> None:
    http.get("/domains/supply/kinds/Part/records/P-1").respond(
        200,
        json={
            "revision": REV,
            "served_from": "materialized",
            "record": {
                **record(),
                "links": [{"relation": "supplied_by", "direction": "forward", "count": 2}],
            },
        },
    )
    listing = http.get("/domains/supply/kinds/Part/records").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "revision": REV,
                    "served_from": "materialized",
                    "items": [record("P-1"), record("P-2", stock="6")],
                    "next_cursor": "c1",
                },
            ),
            httpx.Response(
                200,
                json={
                    "revision": REV,
                    "served_from": "materialized",
                    "items": [record("P-3")],
                    "next_cursor": None,
                },
            ),
        ]
    )
    one = run("record", "get", "Part", "P-1", "-d", "supply", "--links")
    assert one.exit_code == 0
    assert "Part P-1" in one.stdout
    assert "links: supplied_by  2" in one.stdout
    many = run(
        "record", "list", "Part", "-d", "supply", "-b", "work",
        "--where", '{"attr": "stock", "op": "gte", "value": "5"}',
        "--order-by", "stock:desc", "--select", "stock", "--all",
    )  # fmt: skip
    assert many.exit_code == 0, many.stderr
    assert many.stdout.splitlines()[0].split() == ["key", "title", "stock"]
    assert len(many.stdout.splitlines()) == 4
    params = listing.calls[0].request.url.params
    assert json.loads(params["where"]) == {"attr": "stock", "op": "gte", "value": "5"}
    assert (params["order_by"], params["select"], params["branch"]) == (
        "stock:desc",
        "stock",
        "work",
    )
    assert listing.calls[1].request.url.params["cursor"] == "c1"


def test_record_history_and_lineage(http: respx.MockRouter) -> None:
    served = {
        "revision": REV,
        "served_from": "tree",
        "branch": "work",
        "kind": "Part",
        "key": "P-1",
    }
    change = {
        "revision": REV,
        "generation": 1,
        "at": "2026-01-01T00:00:00Z",
        "author": "ada",
        "message": "add",
        "origin": {},
        "change": "created",
        "attributes": [{"name": "stock", "after": "5"}],
    }
    history = http.get("/domains/supply/kinds/Part/records/P-1/history").respond(
        200, json={**served, "items": [change], "next_cursor": "c1"}
    )
    http.get("/domains/supply/kinds/Part/records/P-1/lineage").respond(
        200,
        json={
            **served,
            "created": change,
            "changes": 1,
            "attributes": [
                {
                    "name": "stock",
                    "value": "5",
                    "revision": REV,
                    "author": "ada",
                    "provenance": {
                        "id": "p1",
                        "origin_type": "operation",
                        "origin": {},
                        "actor": "ada",
                        "at": "2026-01-01T00:00:00Z",
                    },
                }
            ],
            "sources": ["erp"],
            "proposals": [],
        },
    )
    listed = run(
        "record", "history", "Part", "P-1", "-d", "supply", "-b", "work",
        "--values", "--limit", "5", "--cursor", "c0",
    )  # fmt: skip
    assert listed.exit_code == 0, listed.stderr
    assert "created" in listed.stdout
    assert "--cursor c1" in listed.stderr
    params = history.calls[0].request.url.params
    assert (params["branch"], params["values"], params["limit"], params["cursor"]) == (
        "work",
        "true",
        "5",
        "c0",
    )
    as_json = run("record", "history", "Part", "P-1", "-d", "supply", "--json")
    assert json.loads(as_json.stdout)["next_cursor"] == "c1"
    traced = run("record", "lineage", "Part", "P-1", "-d", "supply")
    assert traced.exit_code == 0, traced.stderr
    assert "operation by ada" in traced.stdout
    assert "erp" in traced.stdout


def test_record_list_where_from_a_file_and_bad_json(http: respx.MockRouter, tmp_path: Path) -> None:
    route = http.get("/domains/supply/kinds/Part/records").respond(
        200, json={"revision": REV, "served_from": "tree", "items": [], "next_cursor": None}
    )
    predicate = tmp_path / "w.json"
    predicate.write_text('{"match": "seal"}')
    assert run("record", "list", "Part", "-d", "supply", "--where", f"@{predicate}").exit_code == 0
    assert json.loads(route.calls.last.request.url.params["where"]) == {"match": "seal"}
    bad = run("record", "list", "Part", "-d", "supply", "--where", "{nope")
    assert bad.exit_code == 1
    assert "not valid JSON" in bad.stderr


def test_query_inline_from_file_and_stdin(http: respx.MockRouter, tmp_path: Path) -> None:
    route = http.post("/domains/supply/query").respond(
        200,
        json={
            "revision": REV,
            "served_from": "materialized",
            "items": [record("P-1", category="bearing")],
            "next_cursor": None,
        },
    )
    inline = run("query", '{"kind": "Part", "limit": 5}', "-d", "supply", "-b", "work")
    assert inline.exit_code == 0, inline.stderr
    assert "P-1" in inline.stdout
    assert request_bodies(route)[0] == {"kind": "Part", "limit": 5, "branch": "work"}
    file = tmp_path / "q.json"
    file.write_text('{"kind": "Part"}')
    assert run("query", str(file), "-d", "supply").exit_code == 0
    assert run("query", "-", "-d", "supply", input_='{"kind": "Part"}').exit_code == 0
    assert run("query", "[1]", "-d", "supply").exit_code == 1
    assert run("query", str(tmp_path / "missing.json"), "-d", "supply").exit_code == 1
    as_json = json.loads(run("query", '{"kind": "Part"}', "-d", "supply", "--json").stdout)
    assert as_json[0]["key"] == "P-1"


def test_query_with_an_aggregate_prints_the_counts(http: respx.MockRouter) -> None:
    http.post("/domains/supply/query").respond(
        200,
        json={
            "revision": REV,
            "served_from": "materialized",
            "items": [],
            "next_cursor": None,
            "aggregate": {"counts": [{"kind": "Part", "value": "bearing", "count": 7}]},
        },
    )
    result = run("query", '{"kind": "Part", "aggregate": {"group_by": "category"}}', "-d", "supply")
    assert result.exit_code == 0
    assert "bearing" in result.stdout
    assert result.stdout.splitlines()[0].split() == ["kind", "value", "count"]


def test_search(http: respx.MockRouter) -> None:
    route = http.post("/domains/supply/search").respond(
        200,
        json={
            "revision": REV,
            "served_from": "materialized",
            "mode": "lexical",
            "items": [
                {
                    "kind": "Part",
                    "key": "P-1",
                    "title": "Seal",
                    "snippet": "a <b>seal</b>",
                    "score": 0.5,
                }
            ],
        },
    )
    result = run("search", "seal", "--kind", "Part", "-d", "supply")
    assert result.exit_code == 0
    assert "0.500" in result.stdout
    assert request_bodies(route)[0] == {"q": "seal", "kinds": ["Part"], "limit": 50}


def test_search_with_a_query_vector(http: respx.MockRouter, tmp_path: Path) -> None:
    route = http.post("/domains/supply/search").respond(
        200, json={"revision": REV, "served_from": "materialized", "mode": "hybrid", "items": []}
    )
    embedded = tmp_path / "vector.json"
    embedded.write_text(json.dumps({"model": "m", "dims": 3, "vector": [0.5, 0, -1]}))
    assert run("search", "seal", "--vector-file", str(embedded), "-d", "supply").exit_code == 0
    plain = tmp_path / "plain.json"
    plain.write_text("[1, 2]")
    assert run("search", "seal", "--vector-file", str(plain), "-d", "supply").exit_code == 0
    bodies = request_bodies(route)
    assert bodies[0]["query_vector"] == [0.5, 0.0, -1.0]
    assert bodies[1]["query_vector"] == [1.0, 2.0]
    plain.write_text('["x"]')
    result = run("search", "seal", "--vector-file", str(plain), "-d", "supply")
    assert result.exit_code != 0
    assert "JSON array of numbers" in result.output


# History ------------------------------------------------------------------------------------


def revision(
    rev: str = REV, message: str = "Import parts\nsecond line", **extra: Any
) -> dict[str, Any]:
    return {
        "id": rev,
        "parents": [REV2],
        "root": "00" * 32,
        "generation": 3,
        "author": "usr_1",
        "on_behalf_of": None,
        "message": message,
        "origin": {"type": "import"},
        "stats": {"changes": 2},
        "created_at": NOW,
        **extra,
    }


def test_log(http: respx.MockRouter) -> None:
    http.get("/domains/supply").respond(200, json=domain())
    log = http.get("/domains/supply/branches/main/log").respond(
        200,
        json={
            "branch": "main",
            "head": REV,
            "items": [revision(), revision(REV2, "Root")],
            "next_cursor": None,
        },
    )
    result = run("log", "-d", "supply", "--author", "usr_1", "--all-parents")
    assert result.exit_code == 0
    assert "Import parts" in result.stdout
    assert "second line" not in result.stdout  # the first line of the message
    params = log.calls.last.request.url.params
    assert (params["author"], params["all"]) == ("usr_1", "true")
    assert json.loads(run("log", "main", "-d", "supply", "--json").stdout)[1]["message"] == "Root"


def change(key: str = "P-1") -> dict[str, Any]:
    return {
        "element": "record",
        "change": "modified",
        "partition": "kind:k1",
        "kind": "Part",
        "key": key,
        "changes": [{"name": "stock", "old": "5", "new": "6"}],
    }


def test_diff(http: respx.MockRouter) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        second = request.url.params.get("cursor") == "c1"
        return httpx.Response(
            200,
            json={
                "from_revision": REV,
                "to_revision": REV2,
                "total": 2,
                "summary": [
                    {"partition": "kind:k1", "element": "record", "name": "Part", "modified": 2}
                ],
                "items": [change("P-2" if second else "P-1")],
                "next_cursor": None if second else "c1",
            },
        )

    route = http.get("/domains/supply/diff").mock(side_effect=answer)
    result = run("diff", "main", "work", "-d", "supply", "--kind", "Part", "--all")
    assert result.exit_code == 0, result.stderr
    assert "stock: 5 -> 6" in result.stdout
    assert "P-2" in result.stdout
    assert "2 changes (Part: +0 -0 ~2)" in result.stderr
    params = route.calls[0].request.url.params
    assert (params["from"], params["to"], params["kinds"]) == ("main", "work", "Part")
    document = json.loads(run("diff", "main", "work", "-d", "supply", "--all", "--json").stdout)
    assert document["total"] == 2
    assert [c["key"] for c in document["items"]] == ["P-1", "P-2"]


def test_revision_show_with_changes(http: respx.MockRouter) -> None:
    http.get("/domains/supply/revisions/main").respond(200, json=revision())
    http.get(f"/domains/supply/revisions/{REV}/changeset").respond(
        200,
        json={
            "from_revision": REV2,
            "to_revision": REV,
            "total": 1,
            "summary": [],
            "items": [change()],
            "next_cursor": None,
        },
    )
    plain = run("revision", "show", "main", "-d", "supply")
    assert plain.exit_code == 0
    assert REV in plain.stdout
    detailed = run("revision", "show", "main", "-d", "supply", "--changes")
    assert "stock: 5 -> 6" in detailed.stdout
    assert (
        json.loads(run("revision", "show", "main", "-d", "supply", "--changes", "--json").stdout)[
            "changes"
        ][0]["key"]
        == "P-1"
    )


# API keys -----------------------------------------------------------------------------------


def test_apikey_commands(http: respx.MockRouter) -> None:
    key = {
        "id": "key_1",
        "principal_id": "svc_1",
        "prefix": "abc",
        "scopes": ["domain:supply"],
        "expires_at": None,
        "last_used_at": None,
        "revoked_at": None,
        "created_at": NOW,
    }
    create = http.post("/domains/supply/api-keys").respond(
        201, json={**key, "key": "lbk_abc_secret", "role": "viewer"}
    )
    http.get("/domains/supply/api-keys").respond(200, json={"items": [key], "next_cursor": None})
    revoke = http.delete("/domains/supply/api-keys/key_1").respond(204)
    created = run(
        "apikey", "create", "reader bot", "--role", "viewer", "--ttl-days", "7", "-d", "supply"
    )
    assert created.exit_code == 0
    assert "lbk_abc_secret" in created.stdout
    assert "only time" in created.stderr
    assert request_bodies(create) == [{"name": "reader bot", "role": "viewer", "ttl_days": 7}]
    assert "abc" in run("apikey", "list", "-d", "supply").stdout
    assert run("apikey", "revoke", "key_1", "-d", "supply").exit_code == 0
    assert revoke.called
    assert json.loads(run("apikey", "list", "-d", "supply", "--json").stdout)[0]["id"] == "key_1"


def test_admin_erase_by_record_and_by_value(http: respx.MockRouter) -> None:
    erase = http.post("/domains/supply/vault/erase").respond(
        200, json={"domain": "supply", "kind": "Person", "mode": "record", "erased": 2}
    )
    by_key = run("admin", "erase", "Person", "--key", "P-1", "--yes", "-d", "supply")
    assert by_key.exit_code == 0
    assert "erased 2" in by_key.stderr + by_key.stdout
    by_value = run(
        "admin", "erase", "Person", "--attribute", "email", "--value", "a@b.c", "-y", "-d", "supply"
    )
    assert by_value.exit_code == 0
    from_stdin = run(
        "admin",
        "erase",
        "Person",
        "--attribute",
        "email",
        "--value",
        "-",
        "-y",
        "-d",
        "supply",
        input_="secret@b.c\n",
    )
    assert from_stdin.exit_code == 0
    assert request_bodies(erase) == [
        {"kind": "Person", "key": "P-1"},
        {"kind": "Person", "attribute": "email", "value": "a@b.c"},
        {"kind": "Person", "attribute": "email", "value": "secret@b.c"},
    ]
    # Irreversible: without --yes and a terminal, nothing is sent.
    assert run("admin", "erase", "Person", "--key", "P-2", "-d", "supply").exit_code != 0
    assert run("admin", "erase", "Person", "--value", "x", "-y", "-d", "supply").exit_code != 0
    assert erase.call_count == 3


def test_audit_verify_reports_an_intact_chain(http: respx.MockRouter) -> None:
    verify = http.get("/domains/supply/audit/verify").respond(
        200,
        json={
            "domain_id": "supply",
            "intact": True,
            "checked": 12,
            "after_seq": 40,
            "last_seq": 77,
            "first_broken": None,
        },
    )
    result = run("audit", "verify", "-d", "supply", "--after-seq", "40")
    assert result.exit_code == 0
    assert "intact: 12 entries verified" in result.stdout
    assert "77" in result.stdout
    assert verify.calls.last.request.url.params["after_seq"] == "40"


def test_audit_verify_exits_2_on_a_broken_chain(http: respx.MockRouter) -> None:
    http.get("/domains/supply/audit/verify").respond(
        200,
        json={
            "domain_id": "supply",
            "intact": False,
            "checked": 3,
            "after_seq": 0,
            "last_seq": 9,
            "first_broken": {"seq": 11, "reason": "chain link broken"},
        },
    )
    result = run("audit", "verify", "-d", "supply")
    assert result.exit_code == 2
    assert "broken at seq 11: chain link broken" in result.stdout
    as_json = run("audit", "verify", "-d", "supply", "--json")
    assert as_json.exit_code == 2
    assert json.loads(as_json.stdout)["first_broken"]["seq"] == 11


def test_audit_verify_refused_for_a_non_admin(http: respx.MockRouter) -> None:
    http.get("/domains/supply/audit/verify").mock(
        return_value=problem(403, "forbidden", "Missing permission audit.read.")
    )
    result = run("audit", "verify", "-d", "supply")
    assert result.exit_code == 3
    assert "audit.read" in result.stderr


TRACE_SESSION = {
    "session_id": "s-1",
    "actor": "usr_1",
    "started_at": NOW,
    "last_at": NOW,
    "calls": 2,
    "notes": 1,
    "domains": ["supply"],
    "tools": ["search", "get_record"],
    "proposals": ["prp_1"],
}
TRACE_STEPS = [
    {
        "step": 1,
        "seq": 1,
        "type": "call",
        "at": NOW,
        "actor": "usr_1",
        "tool": "search",
        "method": "POST",
        "route": "/domains/{domain_id}/search",
        "status": 200,
        "latency_ms": 12.4,
        "cache": "miss",
        "result": {"counts": {"items": 3}},
        "refs": {
            "records": [{"kind": "Part", "key": "P-1"}],
            "links": [],
            "truncated": False,
        },
    },
    {
        "step": 2,
        "seq": 2,
        "type": "note",
        "at": NOW,
        "actor": "usr_1",
        "note": "looked \x1b[31mred\x1b[0m",
    },
]


def test_trace_list_filters_and_prints_sessions(http: respx.MockRouter) -> None:
    route = http.get("/trace/sessions").respond(
        200, json={"enabled": True, "items": [TRACE_SESSION], "next_cursor": None}
    )
    result = run("trace", "list", "--actor", "usr_1", "-d", "supply", "--limit", "5")
    assert result.exit_code == 0, result.output
    assert "s-1" in result.stdout
    assert "search, get_record" in result.stdout
    params = route.calls.last.request.url.params
    assert (params["actor"], params["domain_id"], params["limit"]) == ("usr_1", "supply", "5")
    as_json = run("trace", "list", "--json")
    assert json.loads(as_json.stdout)["items"][0]["session_id"] == "s-1"


def test_trace_show_prints_the_timeline_and_notes_as_plain_text(http: respx.MockRouter) -> None:
    route = http.get("/trace/sessions/s-1").respond(
        200, json={"session_id": "s-1", "truncated": False, "steps": TRACE_STEPS}
    )
    result = run("trace", "show", "s-1", "--actor", "usr_1")
    assert result.exit_code == 0, result.output
    assert route.calls.last.request.url.params["actor"] == "usr_1"
    assert "POST /domains/{domain_id}/search" in result.stdout
    assert "items=3" in result.stdout
    assert "1r 0l" in result.stdout
    assert "\x1b" not in result.stdout
    assert "looked ?[31mred?[0m" in result.stdout
    as_json = run("trace", "show", "s-1", "--json")
    assert json.loads(as_json.stdout)["steps"][1]["type"] == "note"


def test_trace_show_unknown_session_is_not_found(http: respx.MockRouter) -> None:
    http.get("/trace/sessions/nope").mock(return_value=problem(404, "not_found", "no session"))
    result = run("trace", "show", "nope")
    assert result.exit_code == 1


def test_trace_note_posts_the_text(http: respx.MockRouter) -> None:
    route = http.post("/trace/sessions/s-1/notes").respond(202, json={"recorded": True})
    result = run("trace", "note", "s-1", "why", "-d", "supply", "--step", "3")
    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content) == {
        "text": "why",
        "domain_id": "supply",
        "step": 3,
    }


def test_apikey_create_and_list_show_allowed_networks(http: respx.MockRouter) -> None:
    key = {
        "id": "key_2",
        "principal_id": "svc_2",
        "prefix": "def",
        "scopes": ["domain:supply"],
        "expires_at": None,
        "last_used_at": None,
        "revoked_at": None,
        "created_at": NOW,
        "allowed_networks": ["203.0.113.0/24", "2001:db8::/48"],
    }
    create = http.post("/domains/supply/api-keys").respond(
        201, json={**key, "key": "lbk_def_secret", "role": "viewer"}
    )
    http.get("/domains/supply/api-keys").respond(200, json={"items": [key], "next_cursor": None})
    created = run(
        "apikey",
        "create",
        "bot",
        "--allow-network",
        "203.0.113.0/24",
        "--allow-network",
        "2001:db8::/48",
        "-d",
        "supply",
    )
    assert created.exit_code == 0, created.output
    assert request_bodies(create) == [
        {"name": "bot", "allowed_networks": ["203.0.113.0/24", "2001:db8::/48"]}
    ]
    assert "203.0.113.0/24, 2001:db8::/48" in created.stdout
    listed = run("apikey", "list", "-d", "supply")
    assert "203.0.113.0/24, 2001:db8::/48" in listed.stdout


# Sources ------------------------------------------------------------------------------------


def test_sources_sync_and_runs(http: respx.MockRouter) -> None:
    sync = http.post("/domains/supply/sources/erp.parts/sync").respond(
        202, json={"job_id": 7, "source": "src_1", "api_name": "erp.parts", "dry_run": True}
    )
    run_row = {
        "id": "srn_1",
        "source": "src_1",
        "status": "failed",
        "started_at": NOW,
        "finished_at": NOW,
        "stats": {},
        "revision": None,
        "proposal_id": None,
        "error": {"message": "too many row errors"},
    }
    http.get("/domains/supply/sources/erp.parts/runs").respond(
        200, json={"items": [run_row], "next_cursor": None}
    )
    queued = run("sources", "sync", "erp.parts", "--dry-run", "-d", "supply")
    assert queued.exit_code == 0, queued.output
    assert "erp.parts (src_1)" in queued.stdout
    assert request_bodies(sync) == [{"dry_run": True}]
    listed = run("sources", "runs", "erp.parts", "-d", "supply")
    assert listed.exit_code == 0, listed.output
    assert "srn_1" in listed.stdout
    assert "failed" in listed.stdout
    as_json = json.loads(run("sources", "runs", "erp.parts", "-d", "supply", "--json").stdout)
    assert as_json[0]["error"] == {"message": "too many row errors"}


# Documents and extraction items -------------------------------------------------------------


DOCUMENT_ROW = {
    "id": "doc_1",
    "filename": "spec.txt",
    "media_type": "text/plain",
    "sha256": "ab" * 32,
    "size": 5,
    "text_sha256": None,
    "metadata": {},
    "created_at": NOW,
    "created_by": "usr_1",
}
ITEM_ROW = {
    "id": "itm_1",
    "type": "link",
    "seq": 1,
    "decision": "pending",
    "confidence": 0.9,
    "tier": 1,
    "model": "m",
    "relation": "supplies",
    "from": "S-1",
    "to": "P-1",
    "subject": {},
    "evidence": None,
    "match": None,
    "edits": None,
    "decided_by": None,
    "decided_at": None,
}


def test_documents_upload_sends_the_file(http: respx.MockRouter, tmp_path: Path) -> None:
    route = http.post("/domains/supply/documents").respond(
        201, json={"document": DOCUMENT_ROW, "duplicate": False, "job_id": 4}
    )
    spec = tmp_path / "spec.txt"
    spec.write_bytes(b"hello")
    result = run("documents", "upload", str(spec), "--no-extract", "--tiers", "0,1", "-d", "supply")
    assert result.exit_code == 0, result.output
    assert "doc_1" in result.stdout
    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"; filename="spec.txt"' in sent.content
    assert b'name="extract"\r\n\r\nfalse' in sent.content
    assert b'name="tiers"\r\n\r\n[0,1]' in sent.content
    as_json = json.loads(run("documents", "upload", str(spec), "-d", "supply", "--json").stdout)
    assert as_json["job_id"] == 4
    assert b'name="extract"' not in route.calls.last.request.content


def test_documents_upload_from_a_url(http: respx.MockRouter) -> None:
    route = http.post("/domains/supply/documents").respond(
        200, json={"document": DOCUMENT_ROW, "duplicate": True, "job_id": None}
    )
    result = run("documents", "upload", "--url", "https://example.test/a.pdf", "-d", "supply")
    assert result.exit_code == 0, result.output
    assert request_bodies(route) == [{"url": "https://example.test/a.pdf"}]
    assert "true" in result.stdout


def test_documents_upload_checks_its_arguments(http: respx.MockRouter, tmp_path: Path) -> None:
    route = http.post("/domains/supply/documents")
    assert run("documents", "upload", "-d", "supply").exit_code == 1
    spec = tmp_path / "spec.txt"
    spec.write_bytes(b"x")
    both = run("documents", "upload", str(spec), "--url", "https://example.test/a", "-d", "supply")
    assert both.exit_code == 1
    assert run("documents", "upload", str(spec), "--tiers", "a", "-d", "supply").exit_code == 1
    missing = run("documents", "upload", str(tmp_path / "none.txt"), "-d", "supply")
    assert missing.exit_code == 1
    assert not route.called


def test_documents_list_and_get(http: respx.MockRouter) -> None:
    http.get("/domains/supply/documents").respond(
        200, json={"items": [DOCUMENT_ROW], "next_cursor": None}
    )
    job = {
        "id": "ext_1",
        "status": "succeeded",
        "model": "m",
        "prompt_version": "v1",
        "branch": "extraction/x",
        "proposal_id": "prp_1",
        "created_at": NOW,
        "finished_at": NOW,
        "usage": {},
        "stats": {},
        "error": None,
    }
    http.get("/domains/supply/documents/doc_1").respond(
        200, json={**DOCUMENT_ROW, "extraction_jobs": [job]}
    )
    listed = run("documents", "list", "-d", "supply")
    assert listed.exit_code == 0, listed.output
    assert "spec.txt" in listed.stdout
    shown = run("documents", "get", "doc_1", "-d", "supply")
    assert shown.exit_code == 0, shown.output
    assert "ext_1" in shown.stdout
    assert "proposal prp_1" in shown.stdout
    as_json = json.loads(run("documents", "get", "doc_1", "-d", "supply", "--json").stdout)
    assert as_json["extraction_jobs"][0]["id"] == "ext_1"


def test_documents_text_prints_the_parsed_text(http: respx.MockRouter) -> None:
    body = {
        "document_id": "doc_1",
        "text_sha256": "ab" * 32,
        "text": "Supplier S-1025 ships part",
        "length": 40,
        "truncated": True,
    }
    http.get("/domains/supply/documents/doc_1/text").respond(200, json=body)
    shown = run("documents", "text", "doc_1", "-d", "supply")
    assert shown.exit_code == 0, shown.output
    assert shown.stdout == "Supplier S-1025 ships part\n"
    assert "text cut at 26 of 40 characters" in shown.stderr
    as_json = json.loads(run("documents", "text", "doc_1", "-d", "supply", "--json").stdout)
    assert as_json["truncated"] is True


def test_proposal_items_filters_and_renders(http: respx.MockRouter) -> None:
    route = http.get("/domains/supply/proposals/prp_1/items").respond(
        200, json={"items": [ITEM_ROW], "next_cursor": None}
    )
    result = run(
        "proposal",
        "items",
        "prp_1",
        "--status",
        "pending",
        "--kind",
        "supplies",
        "--type",
        "link",
        "--min-confidence",
        "0.5",
        "-d",
        "supply",
    )
    assert result.exit_code == 0, result.output
    assert "itm_1" in result.stdout
    assert "supplies" in result.stdout
    params = route.calls.last.request.url.params
    assert params["status"] == "pending"
    assert params["kind"] == "supplies"
    assert params["type"] == "link"
    assert params["min_confidence"] == "0.5"
    as_json = json.loads(run("proposal", "items", "prp_1", "-d", "supply", "--json").stdout)
    assert as_json[0]["from"] == "S-1"
    assert run("proposal", "items", "prp_1", "--status", "done", "-d", "supply").exit_code == 1


def test_proposal_decide_by_item_and_by_filter(http: respx.MockRouter) -> None:
    decided = {
        "proposal_id": "prp_1",
        "title": "Extraction",
        "checks": {},
        "items": [{**ITEM_ROW, "decision": "accepted"}],
        "materialized": {
            "revision": REV,
            "changes": 2,
            "applied": 1,
            "skipped": {"itm_9": "stale"},
        },
    }
    route = http.post("/domains/supply/proposals/prp_1/items:decide").respond(200, json=decided)
    by_item = run(
        "proposal", "decide", "prp_1", "--accept", "itm_1", "--reject", "itm_2", "-d", "supply"
    )
    assert by_item.exit_code == 0, by_item.output
    assert "itm_9: stale" in by_item.stdout
    by_filter = run(
        "proposal",
        "decide",
        "prp_1",
        "--where-status",
        "pending",
        "--where-kind",
        "supplies",
        "--where-min-confidence",
        "0.9",
        "--decision",
        "accepted",
        "-d",
        "supply",
    )
    assert by_filter.exit_code == 0, by_filter.output
    assert request_bodies(route) == [
        {
            "decisions": [
                {"item": "itm_1", "decision": "accepted"},
                {"item": "itm_2", "decision": "rejected"},
            ]
        },
        {
            "decisions": [],
            "where": {"status": ["pending"], "kind": ["supplies"], "min_confidence": 0.9},
            "decision": "accepted",
        },
    ]
    as_json = json.loads(
        run("proposal", "decide", "prp_1", "--accept", "itm_1", "-d", "supply", "--json").stdout
    )
    assert as_json["materialized"]["applied"] == 1


def test_proposal_decide_checks_its_arguments(http: respx.MockRouter) -> None:
    route = http.post("/domains/supply/proposals/prp_1/items:decide")
    assert run("proposal", "decide", "prp_1", "-d", "supply").exit_code == 1
    only_decision = run("proposal", "decide", "prp_1", "--decision", "accepted", "-d", "supply")
    assert only_decision.exit_code == 1
    only_filter = run("proposal", "decide", "prp_1", "--where-kind", "x", "-d", "supply")
    assert only_filter.exit_code == 1
    unknown = run(
        "proposal", "decide", "prp_1", "--where-kind", "x", "--decision", "done", "-d", "supply"
    )
    assert unknown.exit_code == 1
    assert not route.called


def test_quiet_silences_progress_and_notes(http: respx.MockRouter, tmp_path: Path) -> None:
    http.get("/domains/supply/schema").respond(200, json=SCHEMA_DOC)
    http.post("/domains/supply/operations/import_records/execute").respond(200, json=execution())
    data = tmp_path / "p.csv"
    data.write_text("part_number\nP-1\n")
    result = run("--quiet", "import", "records", "Part", str(data), "-d", "supply")
    assert result.exit_code == 0
    assert "batch" not in result.stderr


# Completion and entry points ----------------------------------------------------------------


def test_domain_ids_complete_from_the_server(http: respx.MockRouter) -> None:
    from lorebase_cli.options import complete_domain

    route = http.get("/domains").respond(
        200, json={"items": [domain("supply"), domain("sales"), domain("hr")], "next_cursor": None}
    )
    assert complete_domain("s") == ["supply", "sales"]
    route.respond(500)
    assert complete_domain("s") == []  # completion never fails loudly


def test_module_entry_point_runs() -> None:
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "lorebase_cli", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0
    assert done.stdout.startswith("lorebase 0.1.0")


# Proposals, context and explain --------------------------------------------------------------

FINGERPRINT = "ef" * 32


def proposal(**extra: Any) -> dict[str, Any]:
    return {
        "id": "prp_1",
        "title": "Add suppliers",
        "description": None,
        "source_ref": "work",
        "target_ref": "main",
        "origin": "manual",
        "status": "open",
        "author": "usr_1",
        "created_at": NOW,
        "updated_at": None,
        "merged_revision": None,
        "policy": {},
        "checks": [{"name": "schema", "status": "passed"}],
        **extra,
    }


def proposal_detail(**extra: Any) -> dict[str, Any]:
    return {
        **proposal(),
        "reviews": [],
        "approval": {
            "protected": True,
            "required": 1,
            "approvals": 0,
            "approvers": [],
            "changes_requested": [],
            "satisfied": False,
        },
        "attempt": None,
        **extra,
    }


def attempt(**extra: Any) -> dict[str, Any]:
    return {
        "id": "att_1",
        "proposal": "prp_1",
        "status": "conflicted",
        "result": None,
        "base": REV,
        "ours": REV,
        "theirs": REV2,
        "virtual_base": False,
        "created_at": NOW,
        "created_by": "usr_1",
        "revision": None,
        "policy": {},
        "open_conflicts": 1,
        "resolved_conflicts": 0,
        "counts": {},
        "recomputed": False,
        "stale": False,
        "resolutions": [],
        "unmatched_resolutions": [],
        "auto_resolutions": [],
        **extra,
    }


def conflicts_page(**extra: Any) -> dict[str, Any]:
    conflict = {
        "id": FINGERPRINT,
        "status": "open",
        "kind": "DV1",
        "fingerprint": FINGERPRINT,
        "partition": "Part",
        "key": "P-1",
        "message": "both sides changed stock",
    }
    return {**attempt(), "conflicts": [conflict], "next_cursor": None, **extra}


def test_proposal_create_posts_branches_and_title(http: respx.MockRouter) -> None:
    route = http.post("/domains/supply/proposals").respond(201, json=proposal_detail())
    result = run("proposal", "create", "work", "main", "--title", "Add suppliers", "-d", "supply")
    assert result.exit_code == 0
    assert request_bodies(route) == [
        {"source_ref": "work", "target_ref": "main", "title": "Add suppliers"}
    ]
    assert "work -> main" in result.stdout
    assert "approvals" in result.stdout
    as_json = json.loads(
        run(
            "proposal",
            "create",
            "work",
            "main",
            "--title",
            "T",
            "--description",
            "D",
            "-d",
            "supply",
            "--json",
        ).stdout
    )
    assert as_json["id"] == "prp_1"
    assert request_bodies(route)[1]["description"] == "D"
    assert run("proposal", "create", "work", "main", "-d", "supply").exit_code == 1  # no title


def test_proposal_list_filters_and_renders(http: respx.MockRouter) -> None:
    route = http.get("/domains/supply/proposals").respond(
        200, json={"items": [proposal(), proposal(id="prp_2")], "next_cursor": None}
    )
    result = run(
        "proposal",
        "list",
        "--status",
        "open",
        "--source",
        "work",
        "--target",
        "main",
        "--author",
        "usr_1",
        "-d",
        "supply",
    )
    assert result.exit_code == 0
    assert result.stdout.splitlines()[0].split() == [
        "id",
        "title",
        "source",
        "target",
        "status",
        "author",
        "created",
    ]
    assert "prp_2" in result.stdout
    params = route.calls.last.request.url.params
    assert (params["status"], params["source"], params["target"], params["author"]) == (
        "open",
        "work",
        "main",
        "usr_1",
    )
    as_json = json.loads(run("proposal", "list", "-d", "supply", "-n", "1", "--json").stdout)
    assert [p["id"] for p in as_json] == ["prp_1"]


def test_proposal_get_shows_review_state(http: respx.MockRouter) -> None:
    http.get("/domains/supply/proposals/prp_1").respond(
        200, json=proposal_detail(attempt=attempt())
    )
    result = run("proposal", "get", "prp_1", "-d", "supply")
    assert result.exit_code == 0
    assert "0/1" in result.stdout
    assert "1 open, 0 resolved" in result.stdout
    as_json = json.loads(run("proposal", "get", "prp_1", "-d", "supply", "--json").stdout)
    assert as_json["approval"]["required"] == 1


def test_proposal_merge_sends_a_key_and_reports_the_revision(http: respx.MockRouter) -> None:
    merged = {
        "status": "merged",
        "result": "merged",
        "revision": REV2,
        "proposal": proposal(status="merged", merged_revision=REV2),
        "attempt": conflicts_page(open_conflicts=0),
        "source_deleted": False,
        "replayed": False,
    }
    route = http.post("/domains/supply/proposals/prp_1/merge").respond(200, json=merged)
    result = run("proposal", "merge", "prp_1", "-m", "Ship it", "-d", "supply")
    assert result.exit_code == 0
    assert REV2 in result.stdout
    sent = route.calls.last.request
    assert json.loads(sent.content) == {"message": "Ship it"}
    assert sent.headers["idempotency-key"]
    as_json = json.loads(run("proposal", "merge", "prp_1", "-d", "supply", "--json").stdout)
    assert as_json["revision"] == REV2


def test_proposal_merge_refused_exits_two(http: respx.MockRouter) -> None:
    http.post("/domains/supply/proposals/prp_1/merge").mock(
        return_value=problem(409, "unresolved-conflicts", "1 Conflict is still open.")
    )
    refused = run("proposal", "merge", "prp_1", "-d", "supply")
    assert refused.exit_code == 2
    assert "1 Conflict is still open." in refused.stderr
    as_json = run("proposal", "merge", "prp_1", "-d", "supply", "--json")
    assert as_json.exit_code == 2
    assert json.loads(as_json.stderr)["error"]["status"] == 409
    http.post("/domains/supply/proposals/prp_1/merge").mock(
        return_value=problem(404, "not-found", "No such Proposal.")
    )
    assert run("proposal", "merge", "prp_1", "-d", "supply").exit_code == 1


def test_proposal_review_and_close(http: respx.MockRouter) -> None:
    approval = {**proposal_detail()["approval"], "approvals": 1, "approvers": ["usr_2"]}
    approval["satisfied"] = True
    reviewed = {
        "review": {
            "id": "rvw_1",
            "reviewer": "usr_2",
            "verdict": "approve",
            "comment": "Looks right",
            "at": NOW,
        },
        "approval": approval,
    }
    route = http.post("/domains/supply/proposals/prp_1/reviews").respond(201, json=reviewed)
    result = run("proposal", "review", "prp_1", "approve", "-c", "Looks right", "-d", "supply")
    assert result.exit_code == 0
    assert "1/1 (satisfied)" in result.stdout
    assert json.loads(route.calls.last.request.content) == {
        "verdict": "approve",
        "comment": "Looks right",
    }
    as_json = json.loads(
        run("proposal", "review", "prp_1", "comment", "-d", "supply", "--json").stdout
    )
    assert as_json["review"]["id"] == "rvw_1"
    unknown = run("proposal", "review", "prp_1", "maybe", "-d", "supply")
    assert unknown.exit_code != 0
    assert "unknown verdict" in unknown.stderr

    http.post("/domains/supply/proposals/prp_1/close").respond(
        200, json=proposal_detail(status="closed")
    )
    closed = run("proposal", "close", "prp_1", "-d", "supply", "--json")
    assert closed.exit_code == 0
    assert json.loads(closed.stdout)["status"] == "closed"


def test_proposal_conflicts_lists_and_filters(http: respx.MockRouter) -> None:
    route = http.get("/domains/supply/proposals/prp_1/conflicts").respond(
        200, json=conflicts_page()
    )
    result = run("proposal", "conflicts", "prp_1", "--status", "open", "-d", "supply")
    assert result.exit_code == 0
    assert "DV1" in result.stdout
    assert FINGERPRINT[:12] in result.stdout
    assert route.calls.last.request.url.params["status"] == "open"
    as_json = json.loads(run("proposal", "conflicts", "prp_1", "-d", "supply", "--json").stdout)
    assert as_json[0]["id"] == FINGERPRINT
    assert as_json[0]["message"] == "both sides changed stock"  # members the model does not know


def test_proposal_resolve_one_and_many(http: respx.MockRouter, tmp_path: Path) -> None:
    resolution = {
        "fingerprint": FINGERPRINT,
        "choice": "custom",
        "value": {"stock": "5"},
        "resolver": "usr_1",
        "at": NOW,
    }
    one = http.post(f"/domains/supply/proposals/prp_1/conflicts/{FINGERPRINT}/resolution").respond(
        200, json=resolution
    )
    many = http.post("/domains/supply/proposals/prp_1/conflicts:resolve").respond(
        200, json=conflicts_page(open_conflicts=0, resolved_conflicts=1, status="clean")
    )
    done = run(
        "proposal",
        "resolve",
        "prp_1",
        FINGERPRINT,
        "--choice",
        "custom",
        "--value",
        '{"stock": "5"}',
        "-d",
        "supply",
    )
    assert done.exit_code == 0
    assert request_bodies(one) == [{"choice": "custom", "value": {"stock": "5"}}]
    assert "custom" in done.stdout
    theirs = run(
        "proposal", "resolve", "prp_1", FINGERPRINT, "--choice", "theirs", "-d", "supply", "--json"
    )
    assert json.loads(theirs.stdout)["fingerprint"] == FINGERPRINT
    assert request_bodies(one)[1] == {"choice": "theirs"}

    file = tmp_path / "resolutions.json"
    file.write_text(json.dumps([{"fingerprint": FINGERPRINT, "choice": "ours"}]))
    bulk = run("proposal", "resolve", "prp_1", "--file", str(file), "-d", "supply")
    assert bulk.exit_code == 0
    assert "resolved conflicts" in bulk.stdout
    assert request_bodies(many) == [
        {"resolutions": [{"choice": "ours", "fingerprint": FINGERPRINT}]}
    ]


def test_proposal_resolve_checks_its_arguments(http: respx.MockRouter, tmp_path: Path) -> None:
    missing = run("proposal", "resolve", "prp_1", FINGERPRINT, "-d", "supply")
    assert missing.exit_code == 1
    assert "--choice" in missing.stderr
    file = tmp_path / "r.json"
    file.write_text('{"nope": 1}')
    bad = run("proposal", "resolve", "prp_1", "--file", str(file), "-d", "supply")
    assert bad.exit_code == 1
    both = run("proposal", "resolve", "prp_1", FINGERPRINT, "--file", str(file), "-d", "supply")
    assert both.exit_code == 1
    short = tmp_path / "s.json"
    short.write_text('[{"fingerprint": "x", "choice": "ours"}]')
    invalid = run("proposal", "resolve", "prp_1", "--file", str(short), "-d", "supply")
    assert invalid.exit_code == 1
    assert "invalid arguments" in invalid.stderr


PACK = {
    "domain": "supply",
    "branch": "work",
    "revision": REV,
    "etag": '"e"',
    "seeds": [{"ref": "Part/P-1", "title": "Bearing", "values": {"stock": "3"}}],
    "neighbors": [{"ref": "Supplier/S-1", "title": "Acme", "depth": 1}],
    "links": {},
    "budget": {"limit_tokens": 4000, "used_tokens": 120},
}


def test_context_prints_a_summary_json_or_markdown(http: respx.MockRouter) -> None:
    route = http.post("/domains/supply/context")
    route.side_effect = [
        httpx.Response(200, json=PACK),
        httpx.Response(200, json=PACK),
        httpx.Response(200, text="# Context\n", headers={"content-type": "text/markdown"}),
    ]
    result = run(
        "context",
        "--ref",
        "Part/P-1",
        "--ref",
        "Part/P-2",
        "--budget",
        "4000",
        "--depth",
        "1",
        "--relation",
        "supplied_by",
        "-b",
        "work",
        "-d",
        "supply",
    )
    assert result.exit_code == 0
    assert "120/4000" in result.stdout
    assert "Bearing" in result.stdout
    assert "Supplier/S-1" in result.stdout
    as_json = json.loads(run("context", "--search", "bearing", "-d", "supply", "--json").stdout)
    assert as_json["seeds"][0]["ref"] == "Part/P-1"
    text = run("context", "--query", '{"kind": "Part"}', "--markdown", "-d", "supply")
    assert text.stdout == "# Context\n"
    first, second, third = request_bodies(route)
    assert first["refs"] == ["Part/P-1", "Part/P-2"]
    assert (first["budget_tokens"], first["depth"], first["relations"]) == (
        4000,
        1,
        ["supplied_by"],
    )
    assert first["branch"] == "work"
    assert first["format"] == "json"
    assert second["search"] == "bearing"
    assert third["query"] == {"kind": "Part"}
    assert third["format"] == "markdown"
    assert run("context", "--query", "[1]", "-d", "supply").exit_code == 1


def test_context_rejected_seeds_exit_two(http: respx.MockRouter) -> None:
    http.post("/domains/supply/context").mock(
        return_value=problem(422, "validation-failed", "give exactly one of refs, query or search")
    )
    result = run("context", "-d", "supply")
    assert result.exit_code == 2
    assert "exactly one" in result.stderr


EXPLAINED = {
    "domain": "supply",
    "branch": "main",
    "revision": REV,
    "record": {"ref": "Part/P-1", "kind": "Part", "key": "P-1", "title": "Bearing", "values": {}},
    "created": {"actor": "usr_1", "revision": REV},
    "attributes": [
        {"name": "stock", "value": "3", "actor": "usr_2", "origin_type": "import", "revision": REV2}
    ],
    "links": [],
    "links_truncated": False,
    "history": {"changes": 2},
    "proposals": [{"id": "prp_1", "title": "Add", "status": "merged"}],
}


def test_explain_prints_lineage_json_or_markdown(http: respx.MockRouter) -> None:
    route = http.get("/domains/supply/kinds/Part/records/P-1/explain")
    route.side_effect = [
        httpx.Response(200, json=EXPLAINED),
        httpx.Response(200, json=EXPLAINED),
        httpx.Response(200, text="# Part P-1\n", headers={"content-type": "text/markdown"}),
    ]
    result = run("explain", "Part", "P-1", "-b", "main", "-d", "supply")
    assert result.exit_code == 0
    assert "Part/P-1" in result.stdout
    assert "prp_1 (merged)" in result.stdout
    assert "stock" in result.stdout
    as_json = json.loads(run("explain", "Part", "P-1", "-d", "supply", "--json").stdout)
    assert as_json["history"] == {"changes": 2}
    text = run("explain", "Part", "P-1", "--markdown", "-d", "supply")
    assert text.stdout == "# Part P-1\n"
    params = [dict(c.request.url.params) for c in route.calls]
    assert params == [
        {"branch": "main", "format": "json"},
        {"format": "json"},
        {"format": "markdown"},
    ]


def test_explain_of_a_missing_record_exits_one(http: respx.MockRouter) -> None:
    http.get("/domains/supply/kinds/Part/records/P-9/explain").mock(
        return_value=problem(404, "not-found", "No such Record.")
    )
    assert run("explain", "Part", "P-9", "-d", "supply").exit_code == 1


def health_summary(**changes: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "kinds": 1,
        "records": 30,
        "links": 12,
        "invalid_records": 2,
        "orphan_links": 3,
        "required_relation_gaps": 1,
        "duplicate_groups": 4,
        "stale_kinds": 1,
        "sampled_kinds": 0,
        **changes,
    }


KIND_HEALTH = {
    "kind": "Part",
    "records": 30,
    "last_change": {"revision": REV, "at": NOW},
    "violations": {
        "checked": 30,
        "sampled": False,
        "invalid_records": 2,
        "issues": [],
        "issues_truncated": False,
    },
    "duplicates": {"attributes": ["name"], "groups": [], "total_groups": 4},
    "derived": {"attributes": ["supplier_count"], "records_missing_value": 30, "stale": True},
}
RELATION_HEALTH = {
    "relation": "supplied_by",
    "from_kind": "Part",
    "to_kind": "Supplier",
    "backing": "explicit",
    "links": 12,
    "orphan_links": {"count": 3, "missing_from": 0, "missing_to": 3, "examples": []},
    "required": True,
    "required_gaps": {"missing": 1, "examples": ["P-9"]},
}
HEALTH_HEAD = {"revision": REV, "served_from": "materialized", "domain": "supply", "branch": "main"}


def test_health_prints_the_domain_report_json_or_markdown(http: respx.MockRouter) -> None:
    route = http.get("/domains/supply/health")
    document = {
        **HEALTH_HEAD,
        "summary": health_summary(),
        "kinds": [KIND_HEALTH],
        "relations": [RELATION_HEALTH],
    }
    route.side_effect = [
        httpx.Response(200, json=document),
        httpx.Response(200, json=document),
        httpx.Response(200, text="# Health\n", headers={"content-type": "text/markdown"}),
    ]
    result = run("health", "-d", "supply", "-b", "main")
    assert result.exit_code == 0
    assert "needs attention" in result.stdout
    assert "invalid records" in result.stdout
    assert "Part" in result.stdout
    assert "stale" in result.stdout
    assert "supplied_by" in result.stdout
    assert (
        json.loads(run("health", "-d", "supply", "--json").stdout)["summary"]["orphan_links"] == 3
    )
    assert run("health", "-d", "supply", "--markdown").stdout == "# Health\n"
    assert [dict(c.request.url.params) for c in route.calls] == [
        {"branch": "main", "format": "json"},
        {"format": "json"},
        {"format": "markdown"},
    ]


def test_health_of_one_kind(http: respx.MockRouter) -> None:
    route = http.get("/domains/supply/kinds/Part/health")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                **HEALTH_HEAD,
                "summary": health_summary(ok=True),
                "health": KIND_HEALTH,
                "relations": [{**RELATION_HEALTH, "direction": "outgoing"}],
            },
        ),
        httpx.Response(200, text="# Health of Part\n", headers={"content-type": "text/markdown"}),
    ]
    result = run("health", "Part", "-d", "supply")
    assert result.exit_code == 0
    assert "status" in result.stdout
    assert "ok" in result.stdout
    assert "supplied_by" in result.stdout
    assert run("health", "Part", "-d", "supply", "--markdown").stdout == "# Health of Part\n"


def test_health_of_an_unreadable_kind_exits_three(http: respx.MockRouter) -> None:
    http.get("/domains/supply/kinds/Plant/health").mock(
        return_value=problem(403, "forbidden", "Missing permission on Kind Plant.")
    )
    assert run("health", "Plant", "-d", "supply").exit_code == 3
