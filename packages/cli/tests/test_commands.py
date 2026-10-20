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
    assert run("branch", "protect", "work", "-d", "supply").exit_code == 0
    assert request_bodies(protect) == [{"protected": True}]
    run("branch", "protect", "work", "--off", "-d", "supply")
    assert request_bodies(protect)[1] == {"protected": False}
    assert run("branch", "delete", "agent/x", "-d", "supply").exit_code == 0
    assert delete.called
    assert "ahead" in run("branch", "get", "work", "-d", "supply").stdout


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
