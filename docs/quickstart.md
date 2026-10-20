# Quickstart

Lorebase is pre-alpha. This page walks through what works on `main` today and says where it
stops. There is no CLI and no MCP server yet, so there is no complete
"ask a question, get an answer through an agent" flow; what you can do is run the versioning and
merge engine, bring up the Keeper REST API on Postgres, load the sample Domain through it, read
it back with the read API, and browse the web app against a mock backend.

| Step | What you get | State |
|------|--------------|-------|
| 3 | Core engine demo: branches, three-way merge, Conflicts, resolutions, diff | works, no services needed |
| 4 | Unit tests | works |
| 5 | Postgres, migrations and Keeper | works |
| 6, 7 | Create a Domain, apply a schema, import data through the REST API | works, with the limits noted below |
| 7 | Read Records, query, search, log and diff through the REST API | works |
| 8 | Web app in mock mode | works, mock data only |
| later | Python client, CLI, MCP server | [coming next](#coming-next) |

## 1. Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Docker with compose (steps 5 to 7)
- Node.js 24 and pnpm (step 8)
- `curl`

## 2. Clone and install

```bash
git clone https://github.com/aantenore/lorebase.git
cd lorebase
uv sync --all-packages
```

All later commands run from the repository root.

## 3. Run the core demo

```bash
uv run python examples/merge_demo.py
```

No Postgres and no network. The script loads the supply schema and its CSV data into an
in-memory tree on `main`, forks `model/risk` and `source/erp`, makes conflicting and
non-conflicting changes on both, merges, lists the Conflicts by kind, resolves them, merges
again and diffs two Revisions. The output is the same on every run. Excerpt:

```text
== 5. merge source/erp into main
merge base 7ad4a553: 5 Conflicts

DV1 ATTRIBUTE_VALUE (1)
  where            attribute   base  ours  theirs  choices
  -----            ---------   ----  ----  ------  -------
  Supplier S-1001  risk_score  32    80    45      ours/theirs/base/custom
...
XS4 MISSING_VALUE (1)
  where                        attribute  base  ours  theirs  choices
  -----                        ---------  ----  ----  ------  -------
  Supplier x2: S-1032, S-1033  risk_band                      custom/relax
```

More examples are indexed in [examples/README.md](../examples/README.md); editing a schema as
code is in [examples/schema_as_code.md](../examples/schema_as_code.md).

## 4. Run the tests

```bash
make test-unit
```

This runs the unit tests of every package and of the repository tooling, including the demo
above; none of them needs Docker or the network. To run only the demo test:

```bash
uv run pytest tests/tooling/test_merge_demo.py
```

`make lint type` runs the linters and the type checker. Integration tests (`make test-int`)
need Docker.

## 5. Start Postgres and Keeper

```bash
cp .env.example .env
```

Edit `.env` and set `POSTGRES_PASSWORD` (URL-safe characters only; it is embedded in the
connection string). The other values have working defaults. Then:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml up -d postgres migrate keeper
```

The first run builds the backend image, which takes a few minutes. Compose starts Postgres 16,
runs the Alembic migrations once (`migrate` exits when done) and starts Keeper on
`127.0.0.1:8080`. Check it:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml ps
curl http://localhost:8080/readyz
```

```json
{"status":"ready","checks":{"database":{"status":"ok","detail":null}}}
```

`/healthz` is the liveness probe and `http://localhost:8080/api/v1/docs` serves the OpenAPI
docs.

> **Development only.** The shipped `config/lorebase.example.yaml` sets `keeper.auth.mode:
> none`: Keeper accepts every request as an administrator
> (`curl http://localhost:8080/api/v1/auth/whoami` answers `"anonymous"` with
> `"installation_admin":true`). Compose publishes the port on `127.0.0.1` only. Do not expose it,
> and use `api_key` or `oidc` modes (they need the `database` section and further settings,
> see the comments in the file) for anything shared.

## 6. Create a Domain and apply the supply schema

Create the Domain `supply`. It starts with one Revision and a protected `main` branch.

```bash
curl -s -X POST http://localhost:8080/api/v1/domains \
  -H 'Content-Type: application/json' \
  -d '{"id": "supply", "display_name": "Supply chain"}'
```

`main` is protected by default (`keeper.domains.protect_default_branch`): changes to it go
through a Proposal (`POST /api/v1/domains/supply/proposals`, then a review and a merge; approvals
are set by `keeper.proposals` and the Domain's `required_approvals` setting), so a direct change
answers `409 protected-branch`. Work on another branch:

```bash
curl -s -X POST http://localhost:8080/api/v1/domains/supply/branches \
  -H 'Content-Type: application/json' \
  -d '{"name": "work"}'
```

The schema of the sample Domain has two Operations (`flag_supplier_risk`, `approve_contract`)
and two Derivations (`Part.supplier_count`, `Part.single_sourced`); the Operations and
`single_sourced` are written in CEL. Expressions are type checked against the schema when it is
applied (`schema.expressions_enabled`, on by default). Check it first with the validate
endpoint, which writes nothing:

```bash
curl -s -X POST 'http://localhost:8080/api/v1/domains/supply/schema:validate?branch=work' \
  -H 'Content-Type: text/yaml' --data-binary @examples/supply/schema.yaml
```

The answer has `"valid":true`. Apply it to `work` as one schema change set (a YAML body is the
full schema document; options go in the query string):

```bash
curl -s -X POST 'http://localhost:8080/api/v1/domains/supply/branches/work/schema/changes?message=Apply%20supply%20schema' \
  -H 'Content-Type: text/yaml' --data-binary @examples/supply/schema.yaml
```

The answer is `"status":"committed"` with the new Revision and 21 changes (the Domain, six
Kinds, seven Relations, a Trait, two Derivations, two Operations and two Sources). Read the schema back:

```bash
curl -s 'http://localhost:8080/api/v1/domains/supply/schema?branch=work&format=yaml'
```

## 7. Import the sample data

Every write goes through an Operation. The built-in `import_records`, `import_links` and
`import_batch` Operations take rows keyed by Attribute api name; booleans must be JSON booleans
and Link rows use `from` and `to`. A Record row may carry `links`, a map from a Relation name to
the key (or keys) at the other end: `Contract`, `PurchaseOrder` and `RiskFlag` need their
`with_supplier`, `for_part` and `flags` Link to exist, so each one is committed in the same
Revision as its Links. This script posts the CSV files of `examples/supply/data` to the `work`
branch (it is safe to run twice: imports upsert):

```bash
uv run python - <<'EOF'
import csv
import json
import urllib.request

FLAGS = {"true": True, "false": False}  # JSON booleans, not text
KEYS = {"from_key": "from", "to_key": "to"}  # Link endpoints
API = "http://localhost:8080/api/v1/domains/supply"


def execute(operation, parameters):
    request = urllib.request.Request(
        f"{API}/operations/{operation}/execute",
        data=json.dumps({"branch": "work", "parameters": parameters}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def rows(name):
    with open(f"examples/supply/data/{name}.csv", newline="") as handle:
        return [
            {KEYS.get(k, k): FLAGS.get(v, v) for k, v in row.items() if v != ""}
            for row in csv.DictReader(handle)
        ]


def with_links(name, key, *relations):
    """Record rows with the targets of their Links of `relations`."""
    targets = {r: {} for r in relations}
    for relation in relations:
        for link in rows(relation):
            targets[relation].setdefault(link["from"], []).append(link["to"])
    return [
        {**row, "links": {r: targets[r][row[key]] for r in relations if row[key] in targets[r]}}
        for row in rows(name)
    ]


for kind, file in (("Supplier", "supplier"), ("Part", "part"), ("Plant", "plant")):
    result = execute("import_records", {"kind": kind, "rows": rows(file)})
    print(kind, result["status"], result["edit_count"], "edits")
contracts = with_links("contract", "contract_number", "with_supplier")
result = execute("import_records", {"kind": "Contract", "rows": contracts})
print("Contract", result["status"], result["edit_count"], "edits")
orders = with_links("purchase_order", "po_number", "for_part", "delivered_to")
result = execute("import_records", {"kind": "PurchaseOrder", "rows": orders})
print("PurchaseOrder", result["status"], result["edit_count"], "edits")
result = execute(
    "import_batch",
    {
        "records": [{"kind": "RiskFlag", **row} for row in rows("risk_flag")],
        "links": [
            {"relation": relation, **row}
            for relation in ("flags", "supplied_by", "stocked_at")
            for row in rows(relation)
        ],
    },
)
print("RiskFlag and Links", result["status"], result["edit_count"], "edits")
EOF
```

```text
Supplier committed 30 edits
Part committed 40 edits
Plant committed 20 edits
Contract committed 50 edits
PurchaseOrder committed 150 edits
RiskFlag and Links committed 136 edits
```

Add `"dry_run": true` next to `"branch"` to compute the edits and violations without committing.
Rows that break the schema are rejected as a whole with `422` and `issues` that point at the row
and Attribute.

Each import is a Revision; the branch moved ahead of `main`:

```bash
curl -s http://localhost:8080/api/v1/domains/supply/branches/work
```

### Read it back

Reads go through the REST API and need no model. Fetch one Record, with its Lineage, from the
`work` branch:

```bash
curl -s 'http://localhost:8080/api/v1/domains/supply/kinds/Supplier/records/S-1001?branch=work&lineage=true'
```

Query with the JSON DSL (here: up to five Suppliers in Italy or Germany; `branch` picks where
to read, the rest is the query of the [design](design.md#82-query-model)):

```bash
curl -s -X POST http://localhost:8080/api/v1/domains/supply/query \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "kind": "Supplier",
       "where": {"attr": "country", "op": "in", "value": ["IT", "DE"]},
       "limit": 5}'
```

The same API serves `search`, `traverse`, `neighborhood`, the branch `log`, `revisions/{id}` and
`diff`. Every response carries the Revision it was served from in the `Lorebase-Revision` header
and an `ETag`. The full list is in the OpenAPI docs at `http://localhost:8080/api/v1/docs`. If you
want to look at the materialized rows directly, query Postgres (the tables are under row-level
security, so the session names the Domain first):

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml exec -T postgres \
  psql -U lorebase -d lorebase \
  -c "set lorebase.domain_ids = 'supply'" \
  -c "select branch, kind_id, count(*) from mat_records group by 1, 2 order by 2" \
  -c "select key, title from mat_records where key = 'S-1001'"
```

You should see 30 Suppliers, 40 Parts and 20 Plants on `work`, and `S-1001 | Kavsen Alloys Sp. z o.o.`.

### Derived values and custom Operations

`Part.supplier_count` and `Part.single_sourced` are computed in the materialized head on every
commit (never stored in the Revision tree) and read like any other value:

```bash
curl -s 'http://localhost:8080/api/v1/domains/supply/kinds/Part/records/P-10001?branch=work'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/query \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "kind": "Part",
       "where": {"attr": "single_sourced", "op": "eq", "value": true}, "limit": 5}'
```

The custom Operations of the schema run with their CEL preconditions, steps and review policy
(`keeper.operations.expressions_enabled`, on by default). Flag a supplier, try a terminated
one (`422 precondition-failed`, with the expression and its position) and approve a small draft
contract. A `critical` flag or a contract above 500000 (such as `C-3003`) answers
`409 review-required`: its review policy sends the edits to a Proposal.

```bash
curl -s -X POST http://localhost:8080/api/v1/domains/supply/operations/flag_supplier_risk/execute \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "parameters": {"supplier": "S-1001", "severity": "high",
       "reason": "Two late deliveries in March"}}'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/operations/flag_supplier_risk/execute \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "parameters": {"supplier": "S-1019", "severity": "low", "reason": "x"}}'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/operations/approve_contract/execute \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "parameters": {"contract": "C-3015"}}'
```

## 8. Web app in mock mode

The web app talks to a `KeeperApi` interface with two adapters. In `mock` mode it serves
its own sample Domains from memory and needs no backend; the `supply` Domain you created above
is not shown there. `live` mode points at Keeper; the read endpoints it needs exist now, but the
screens have not been verified against a running Keeper, including Review against the Proposal
endpoints. Use mock mode for now.

```bash
cd apps/web
cp .env.example .env     # VITE_API_MODE=mock
pnpm install
pnpm dev                 # http://localhost:5173
```

Explorer, Canvas, the schema overview and Review (Proposals and Conflicts, including a seeded
merge of `model/risk` into `main`) are all driven by the mock. See
[apps/web/README.md](../apps/web/README.md) for the variables and what the mock emulates.

## 9. Stop and clean up

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml down      # keep the data
docker compose --env-file .env -f deploy/compose/docker-compose.yaml down -v   # delete the data
```

Stop `pnpm dev` with Ctrl+C.

## Coming next

These do not exist yet; the order follows the [M1 plan](plan/m1.md).

- **Lineage and history API** (T23, the per-Record lineage summary is already on Record reads),
  **Python client** (T24) and **CLI** (T25).
- **Herald, the MCP server** (M3).
- Source syncs from CSV or Postgres (`approved.suppliers`, `erp.purchase_orders` in the sample
  schema are declared but nothing runs them), document extraction and semantic search: M2 and
  M4.
