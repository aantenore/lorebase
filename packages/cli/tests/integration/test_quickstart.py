"""The quickstart flow through the CLI against a real Keeper on Postgres.

Keeper runs on a local port (uvicorn in a thread, from the shared harness) and the CLI runs
in-process through Typer's CliRunner, so the whole path is exercised: arguments, settings from
the environment, the client, HTTP, authentication, the database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from keeper_harness import RunningKeeper
from supply_example import (
    BATCH_KIND_FILES,
    BATCH_LINK_FILES,
    DATA,
    DOMAIN,
    EXAMPLE,
    KIND_FILES,
    LINK_FILES,
    WORK,
    write_stripped_schema,
)
from typer.testing import CliRunner, Result

from lorebase_cli.main import app

pytestmark = pytest.mark.integration


@pytest.fixture
def lorebase(served_keeper: RunningKeeper, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Run a `lorebase` command against the served Keeper as its administrator."""
    monkeypatch.setenv("LOREBASE_URL", served_keeper.url)
    monkeypatch.setenv("LOREBASE_API_KEY", served_keeper.admin_key)
    runner = CliRunner()

    def run(*args: str, expect: int = 0) -> Result:
        result = runner.invoke(app, list(args))
        assert result.exit_code == expect, (args, result.stdout, result.stderr)
        return result

    return run


def test_quickstart_flow(lorebase, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    me = json.loads(lorebase("whoami", "--json").stdout)
    assert me["installation_admin"] is True

    lorebase("domain", "create", DOMAIN, "--name", "Supply chain")
    assert DOMAIN in lorebase("domain", "list").stdout
    assert (
        json.loads(lorebase("domain", "get", DOMAIN, "--json").stdout)["default_branch"] == "main"
    )
    lorebase("branch", "create", WORK, "-d", DOMAIN)

    schema = write_stripped_schema(tmp_path / "supply-schema.yaml")
    check = lorebase("schema", "validate", str(schema), "-d", DOMAIN, "-b", WORK)
    assert "valid: 18 element changes" in check.stdout
    assert "kinds" in lorebase("schema", "diff", str(schema), "-d", DOMAIN, "-b", WORK).stdout
    applied = lorebase(
        "schema", "apply", str(schema), "-d", DOMAIN, "-b", WORK, "-m", "Apply the supply schema"
    )
    assert "committed" in applied.stdout

    # Changes to the protected default branch are refused (409: exit code 2).
    refused = lorebase("schema", "apply", str(schema), "-d", DOMAIN, expect=2)
    assert "protected" in refused.stderr

    for kind, file in KIND_FILES.items():
        done = lorebase("import", "records", kind, str(DATA / file), "-d", DOMAIN, "-b", WORK)
        assert "committed" in done.stdout
        assert "batch 1/1" in done.stderr
    for relation, file in LINK_FILES.items():
        lorebase("import", "links", relation, str(DATA / file), "-d", DOMAIN, "-b", WORK)
    again = json.loads(
        lorebase(
            "import", "records", "Supplier", str(DATA / "supplier.csv"),
            "-d", DOMAIN, "-b", WORK, "--batch-size", "10", "--json",
        ).stdout
    )  # fmt: skip
    assert again["rows"] == 30
    assert again["batches"] == 3  # imports upsert, so running one twice is safe

    supplier = json.loads(
        lorebase("record", "get", "Supplier", "S-1001", "-d", DOMAIN, "-b", WORK, "--json").stdout
    )
    assert supplier["record"]["title"] == "Kavsen Alloys Sp. z o.o."
    assert supplier["record"]["values"]["risk_score"] == "32"
    parts = lorebase(
        "record", "list", "Part", "-d", DOMAIN, "-b", WORK,
        "--where", '{"attr": "category", "op": "eq", "value": "bearing"}',
        "--select", "category", "--all",
    )  # fmt: skip
    assert parts.stdout.splitlines()[0].split() == ["key", "title", "category"]
    assert all("bearing" in line for line in parts.stdout.splitlines()[1:])
    counted = lorebase(
        "query", '{"kind": "Part", "aggregate": {"group_by": "category"}}',
        "-d", DOMAIN, "-b", WORK, "--json",
    )  # fmt: skip
    assert json.loads(counted.stdout)["aggregate"]["counts"]
    assert (
        "Part" in lorebase("search", "bearing", "--kind", "Part", "-d", DOMAIN, "-b", WORK).stdout
    )

    branches = json.loads(lorebase("branch", "list", "-d", DOMAIN, "--json").stdout)
    assert {b["name"] for b in branches} == {"main", WORK}
    lorebase("tag", "create", "v1", "--source", WORK, "-d", DOMAIN)
    assert "v1" in lorebase("tag", "list", "-d", DOMAIN).stdout

    history = json.loads(lorebase("log", WORK, "-d", DOMAIN, "--all", "--json").stdout)
    assert history[0]["message"].startswith("Import")
    changes = lorebase(
        "diff", "main", WORK, "-d", DOMAIN, "--kind", "Supplier", "--no-schema", "--all"
    )
    assert "added" in changes.stdout
    assert "S-1001" in changes.stdout
    shown = lorebase("revision", "show", WORK, "-d", DOMAIN, "--changes", "--limit", "3")
    assert history[0]["id"] in shown.stdout

    created = json.loads(
        lorebase("apikey", "create", "reader", "--role", "viewer", "-d", DOMAIN, "--json").stdout
    )
    assert created["key"].startswith("lbk_")
    assert [
        k["id"] for k in json.loads(lorebase("apikey", "list", "-d", DOMAIN, "--json").stdout)
    ] == [created["id"]]
    lorebase("apikey", "revoke", created["id"], "-d", DOMAIN)


def test_import_batch_loads_the_full_supply_set(lorebase, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Contract, PurchaseOrder and RiskFlag need a Link when they are created, so all six
    Kinds go through one `import batch`, in requests that keep each Record with its Links."""
    lorebase("domain", "create", DOMAIN)
    lorebase("branch", "create", WORK, "-d", DOMAIN)
    lorebase("schema", "apply", str(EXAMPLE / "schema.yaml"), "-d", DOMAIN, "-b", WORK)

    arguments = ["import", "batch", "-d", DOMAIN, "-b", WORK, "--json"]
    for kind, file in BATCH_KIND_FILES.items():
        arguments += ["--records", f"{kind}={DATA / file}"]
    for relation, file in BATCH_LINK_FILES.items():
        arguments += ["--links", f"{relation}={DATA / file}"]

    # a request of 60 rows has to cut the set into several, none of which splits a unit
    loaded = json.loads(lorebase(*arguments, "--batch-size", "60").stdout)
    assert loaded["batches"] > 1
    assert loaded["rows"] == 185 + 241  # 185 Records and 241 Links in the CSV files
    again = json.loads(lorebase(*arguments).stdout)  # imports upsert: one request this time
    assert (again["batches"], again["rows"]) == (1, loaded["rows"])

    counts = {
        kind: len(
            json.loads(
                lorebase("record", "list", kind, "-d", DOMAIN, "-b", WORK, "--all", "--json").stdout
            )
        )
        for kind in BATCH_KIND_FILES
    }
    assert counts == {
        "Supplier": 30,
        "Part": 40,
        "Plant": 20,
        "Contract": 25,
        "PurchaseOrder": 50,
        "RiskFlag": 20,
    }
    contract = json.loads(
        lorebase(
            "record", "get", "Contract", "C-3001", "-d", DOMAIN, "-b", WORK, "--links", "--json"
        ).stdout
    )
    assert {c["relation"]: c["count"] for c in contract["record"]["links"]}["with_supplier"] == 1

    # a Contract without its Link is refused by the server and the CLI says where
    broken = tmp_path / "contract.csv"
    broken.write_text("contract_number,title,status\nC-9999,No supplier,active\n")
    refused = lorebase(
        "import", "batch", "-d", DOMAIN, "-b", WORK, "--records", f"Contract={broken}", expect=2
    )
    assert f"{broken} row 1" in refused.stderr
    assert "was rejected; 0 earlier rows are committed" in refused.stderr


def test_errors_map_to_exit_codes_against_a_real_server(lorebase, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    lorebase("domain", "create", DOMAIN)
    missing = lorebase("branch", "get", "ghost", "-d", DOMAIN, expect=1)
    assert "error:" in missing.stderr
    assert "request id:" in missing.stderr
    exists = lorebase("domain", "create", DOMAIN, expect=2)
    assert "already exists" in exists.stderr.lower() or "exists" in exists.stderr.lower()
    broken = tmp_path / "broken.yaml"
    broken.write_text("format: lorebase.schema/v1\nkinds: {Part: {}}\n")
    lorebase("schema", "validate", str(broken), "-d", DOMAIN, expect=2)
    # A credential the server does not know.
    from typer.testing import CliRunner

    denied = CliRunner().invoke(app, ["whoami"], env={"LOREBASE_API_KEY": "lbk_nope_nothing"})
    assert denied.exit_code == 3
