"""`import batch` and `import records --link`, against a mocked server.

The schema has a Kind whose Records need a Link when they are created (`Contract`, with the
Relation `with_supplier` marked required_from), a Kind with a minted key, one with a composite
key and a Relation derived from an Attribute.
"""

from __future__ import annotations

import itertools
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
runner = CliRunner()

SCHEMA_DOC = {
    "domain": "supply",
    "branch": "work",
    "revision": REV,
    "schema": {
        "kinds": {
            "k1": {
                "api_name": "Supplier",
                "primary_key": ["a1"],
                "attributes": {
                    "a1": {"api_name": "supplier_code", "type": {"name": "string"}},
                    "a2": {"api_name": "name", "type": {"name": "string"}},
                },
            },
            "k2": {
                "api_name": "Contract",
                "primary_key": ["a3"],
                "attributes": {
                    "a3": {"api_name": "contract_number", "type": {"name": "string"}},
                    "a4": {"api_name": "value", "type": {"name": "integer"}},
                    "a5": {"api_name": "active", "type": {"name": "boolean"}},
                },
            },
            "k3": {
                "api_name": "RiskFlag",
                "primary_key": [],
                "attributes": {"a6": {"api_name": "severity", "type": {"name": "string"}}},
            },
            "k4": {
                "api_name": "Pair",
                "primary_key": ["a7", "a8"],
                "attributes": {
                    "a7": {"api_name": "left", "type": {"name": "string"}},
                    "a8": {"api_name": "right", "type": {"name": "string"}},
                },
            },
        },
        "relations": {
            "r1": {
                "api_name": "with_supplier",
                "inverse_api_name": "contracts",
                "from_kind": "k2",
                "to_kind": "k1",
                "backing": "explicit",
                "attributes": {},
            },
            "r2": {
                "api_name": "flags",
                "inverse_api_name": "risk_flags",
                "from_kind": "k3",
                "to_kind": "k1",
                "backing": "explicit",
                "attributes": {
                    "a9": {"api_name": "confirmed", "type": {"name": "boolean"}},
                },
            },
            "r3": {
                "api_name": "issued_to",
                "inverse_api_name": "purchase_orders",
                "from_kind": "k2",
                "to_kind": "k1",
                "backing": "foreign_key",
                "attributes": {},
            },
        },
    },
}


@pytest.fixture
def http() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        router.get("/domains/supply/schema").respond(200, json=SCHEMA_DOC)
        yield router


@pytest.fixture(autouse=True)
def server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOREBASE_URL", "http://keeper.test")
    monkeypatch.setenv("LOREBASE_API_KEY", "lbk_test")


def execution(**extra: Any) -> dict[str, Any]:
    return {
        "status": "committed",
        "operation": "import_batch",
        "branch": "work",
        "invocation_id": "inv_1",
        "parameters_hash": "ff",
        "head": REV,
        "revision": REV2,
        "revisions": [REV2],
        "edits": [],
        "edit_count": 0,
        "edits_truncated": False,
        "violations": [],
        "violation_count": 0,
        "warnings": [],
        "review_required": False,
        "replayed": False,
        **extra,
    }


def bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(call.request.content) for call in route.calls]


@pytest.fixture
def route(http: respx.MockRouter) -> respx.Route:
    return http.post("/domains/supply/operations/import_batch/execute").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=execution(
                edit_count=len(json.loads(request.content)["parameters"]["records"])
                + len(json.loads(request.content)["parameters"]["links"])
            ),
        )
    )


@pytest.fixture
def files(tmp_path: Path) -> dict[str, str]:
    contents = {
        "suppliers": "supplier_code,name\nS-1,Alpha\nS-2,Beta\nS-3,Gamma\n",
        "contracts": "contract_number,value,active\nC-1,10,true\nC-2,20,false\nC-3,30,true\n",
        "with_supplier": "from_key,to_key\nC-1,S-1\nC-2,S-2\nC-3,S-1\n",
        "flags": "key,severity\nF-1,high\nF-2,low\n",
        "flags_links": "from_key,to_key,confirmed\nF-1,S-3,true\nF-2,S-1,\n",
    }
    paths = {}
    for name, text in contents.items():
        paths[name] = str(tmp_path / f"{name}.csv")
        Path(paths[name]).write_text(text)
    return paths


def batch(files: dict[str, str], *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "import", "batch", "-d", "supply", "-b", "work",
            "--records", f"Contract={files['contracts']}",
            "--records", f"Supplier={files['suppliers']}",
            "--links", f"with_supplier={files['with_supplier']}",
            *extra,
        ],
    )  # fmt: skip


def test_records_travel_with_their_links_and_records_without_links_go_first(
    route: respx.Route, files: dict[str, str]
) -> None:
    result = batch(files)
    assert result.exit_code == 0, result.stderr
    [body] = bodies(route)
    parameters = body["parameters"]
    assert [r["kind"] for r in parameters["records"]] == ["Supplier"] * 3 + ["Contract"] * 3
    assert parameters["records"][3] == {
        "kind": "Contract",
        "contract_number": "C-1",
        "value": "10",
        "active": True,
    }
    assert parameters["links"] == [
        {"relation": "with_supplier", "from": "C-1", "to": "S-1"},
        {"relation": "with_supplier", "from": "C-2", "to": "S-2"},
        {"relation": "with_supplier", "from": "C-3", "to": "S-1"},
    ]
    assert body["branch"] == "work"
    assert body["message"] == "Import 2 Kinds and 1 Relations (rows 1-9)"
    assert "batch 1/1: rows 1-9 committed, 9 edits" in result.stderr
    assert "batch" in result.stdout


def test_a_unit_is_never_split_between_requests(route: respx.Route, files: dict[str, str]) -> None:
    result = batch(files, "--batch-size", "4")
    assert result.exit_code == 0, result.stderr
    requests = [b["parameters"] for b in bodies(route)]
    assert [(len(p["records"]), len(p["links"])) for p in requests] == [(3, 0), (2, 2), (1, 1)]
    for parameters in requests:
        contracts = {r["contract_number"] for r in parameters["records"] if r["kind"] == "Contract"}
        assert {link["from"] for link in parameters["links"]} == contracts
    assert "batch 3/3: rows 8-9 committed" in result.stderr
    assert all(call.request.headers["idempotency-key"] for call in route.calls)


def test_a_link_goes_with_its_to_end_when_the_from_end_is_not_imported(
    route: respx.Route, files: dict[str, str], tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "import", "batch", "-d", "supply",
            "--records", f"Supplier={files['suppliers']}",
            "--links", f"with_supplier={files['with_supplier']}",
            "--batch-size", "3",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    requests = [b["parameters"] for b in bodies(route)]
    # S-3 has no Link and goes first; S-1 and S-2 follow with the Links that end at them
    assert [[r["supplier_code"] for r in p["records"]] for p in requests] == [
        ["S-3"],
        ["S-1"],
        ["S-2"],
    ]
    assert [[link["to"] for link in p["links"]] for p in requests] == [[], ["S-1", "S-1"], ["S-2"]]


def test_links_between_records_that_are_not_imported_are_sent_last(
    route: respx.Route, files: dict[str, str]
) -> None:
    result = runner.invoke(
        app,
        ["import", "batch", "-d", "supply", "--links", f"with_supplier={files['with_supplier']}"],
    )
    assert result.exit_code == 0, result.stderr
    [body] = bodies(route)
    assert body["parameters"]["records"] == []
    assert len(body["parameters"]["links"]) == 3


def test_minted_keys_are_taken_from_the_key_column_and_link_attributes_are_coerced(
    route: respx.Route, files: dict[str, str]
) -> None:
    result = runner.invoke(
        app,
        [
            "import", "batch", "-d", "supply", "--dry-run", "--json",
            "--records", f"RiskFlag={files['flags']}",
            "--records", f"Supplier={files['suppliers']}",
            "--links", f"flags={files['flags_links']}",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    [body] = bodies(route)
    assert body["dry_run"] is True
    assert body["parameters"]["records"][-2:] == [
        {"kind": "RiskFlag", "key": "F-1", "severity": "high"},
        {"kind": "RiskFlag", "key": "F-2", "severity": "low"},
    ]
    assert body["parameters"]["links"] == [
        {"relation": "flags", "from": "F-1", "to": "S-3", "confirmed": True},
        {"relation": "flags", "from": "F-2", "to": "S-1"},
    ]
    summary = json.loads(result.stdout)
    assert (summary["target"], summary["rows"], summary["dry_run"]) == ("batch", 7, True)
    assert summary["name"] == "RiskFlag, Supplier, flags"


def test_every_row_is_checked_before_anything_is_written(
    route: respx.Route, files: dict[str, str], tmp_path: Path
) -> None:
    bad = tmp_path / "bad_contracts.csv"
    bad.write_text("contract_number,value\nC-1,10\nC-2,lots\n")
    result = runner.invoke(
        app,
        ["import", "batch", "-d", "supply", "--records", f"Contract={bad}",
         "--records", f"Supplier={files['suppliers']}"],
    )  # fmt: skip
    assert result.exit_code == 1
    assert f"{bad}: row 2: column 'value'" in result.stderr
    assert route.call_count == 0

    no_ends = tmp_path / "no_ends.csv"
    no_ends.write_text("from_key,to_key\nC-1,\n")
    result = runner.invoke(
        app, ["import", "batch", "-d", "supply", "--links", f"with_supplier={no_ends}"]
    )
    assert result.exit_code == 1
    assert "row 1: a Link needs `from` and `to`" in result.stderr
    assert route.call_count == 0


def test_arguments_are_checked(route: respx.Route, files: dict[str, str]) -> None:
    def fails(*args: str) -> str:
        result = runner.invoke(app, ["import", "batch", "-d", "supply", *args])
        assert result.exit_code == 1, result.stdout
        return result.stderr

    assert "give at least one" in fails()
    assert "--records expects Kind=file" in fails("--records", "Supplier")
    assert "--links expects relation=file" in fails("--links", "flags=")
    assert "not standard input" in fails("--records", "Supplier=-")
    assert "unknown Kind 'Ghost'" in fails("--records", f"Ghost={files['suppliers']}")
    assert "unknown Relation 'ghost'" in fails("--links", f"ghost={files['with_supplier']}")
    assert "composite key" in fails("--records", f"Pair={files['suppliers']}")
    assert "cannot tell the format" in fails("--records", "Supplier=suppliers.txt")
    assert route.call_count == 0


def test_a_rejected_request_names_the_files_and_rows(
    http: respx.MockRouter, files: dict[str, str]
) -> None:
    rejected = httpx.Response(
        422,
        json={
            "type": "https://lorebase.dev/problems/validation-failed",
            "title": "validation-failed",
            "status": 422,
            "code": "validation-failed",
            "detail": "2 violation(s); the edits were not committed.",
            "request_id": "req-9",
            "issues": [
                {
                    "code": "parameter.invalid",
                    "path": ["parameters", "records", 1, "value"],
                    "message": "must be at least 15",
                    "severity": "error",
                },
                {
                    "code": "parameter.invalid",
                    "path": ["parameters", "links", 1, "to"],
                    "message": "no such Record",
                    "severity": "error",
                },
                {
                    "code": "link.required",
                    "path": ["parameters"],
                    "message": "a Link is missing",
                    "severity": "error",
                },
            ],
            "issue_count": 3,
        },
        headers={"content-type": "application/problem+json"},
    )
    calls = itertools.cycle([httpx.Response(200, json=execution(edit_count=3)), rejected])
    http.post("/domains/supply/operations/import_batch/execute").mock(
        side_effect=lambda request: next(calls)
    )
    result = batch(files, "--batch-size", "4")
    assert result.exit_code == 2
    assert "request 2 (2 Records, 2 Links) was rejected; 3 earlier rows are committed" in (
        result.stderr
    )
    assert "contracts.csv row 2 value: must be at least 15" in result.stderr
    assert "with_supplier.csv row 2 to: no such Record" in result.stderr
    assert "parameters: a Link is missing" in result.stderr
    as_json = batch(files, "--batch-size", "4", "--json")
    error = json.loads(as_json.stderr)["error"]
    assert (error["first_row"], error["last_row"]) == (4, 7)
    assert error["committed_rows"] == 3


def test_import_records_sends_link_columns_as_the_links_of_the_row(
    http: respx.MockRouter, tmp_path: Path
) -> None:
    imports = http.post("/domains/supply/operations/import_records/execute").respond(
        200, json=execution(operation="import_records")
    )
    data = tmp_path / "contracts.csv"
    data.write_text("contract_number,value,vendor\nC-1,10,S-1\nC-2,20,\n")
    result = runner.invoke(
        app,
        [
            "import",
            "records",
            "Contract",
            str(data),
            "-d",
            "supply",
            "--link",
            "with_supplier=vendor",
        ],
    )
    assert result.exit_code == 0, result.stderr
    [body] = (json.loads(call.request.content) for call in imports.calls)
    assert body["parameters"]["rows"] == [
        {"contract_number": "C-1", "value": "10", "links": {"with_supplier": "S-1"}},
        {"contract_number": "C-2", "value": "20"},
    ]


def test_link_columns_take_lists_and_inverse_relation_names(
    http: respx.MockRouter, tmp_path: Path
) -> None:
    imports = http.post("/domains/supply/operations/import_records/execute").respond(
        200, json=execution(operation="import_records")
    )
    data = tmp_path / "suppliers.jsonl"
    data.write_text(
        '{"supplier_code": "S-1", "deals": ["C-1", "C-2"]}\n{"supplier_code": "S-2", "deals": []}\n'
    )
    result = runner.invoke(
        app,
        ["import", "records", "Supplier", str(data), "-d", "supply", "--link", "contracts=deals"],
    )
    assert result.exit_code == 0, result.stderr
    [body] = (json.loads(call.request.content) for call in imports.calls)
    assert body["parameters"]["rows"] == [
        {"supplier_code": "S-1", "links": {"contracts": ["C-1", "C-2"]}},
        {"supplier_code": "S-2"},
    ]


def test_link_options_are_checked_against_the_schema(
    http: respx.MockRouter, tmp_path: Path
) -> None:
    imports = http.post("/domains/supply/operations/import_records/execute").respond(
        200, json=execution()
    )
    data = tmp_path / "contracts.csv"
    data.write_text("contract_number,vendor\nC-1,S-1\n")

    def fails(*link: str) -> str:
        result = runner.invoke(
            app, ["import", "records", "Contract", str(data), "-d", "supply", *link]
        )
        assert result.exit_code == 1, result.stdout
        return result.stderr

    assert "--link expects relation=column" in fails("--link", "with_supplier")
    assert "'flags' is not a stored Relation of Contract" in fails("--link", "flags=vendor")
    assert "'issued_to' is not a stored Relation" in fails("--link", "issued_to=vendor")
    assert "'contracts' is not a stored Relation" in fails("--link", "contracts=vendor")
    assert imports.call_count == 0
