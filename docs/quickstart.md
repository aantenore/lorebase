# Quickstart

Lorebase is pre-alpha. This page walks through what works on `main` today and says where it
stops. There is no CLI, no MCP server and no read API for Records yet, so there is no complete
"ask a question, get an answer" flow to try; what you can do is run the versioning and merge
engine, bring up the Keeper REST API on Postgres, load the sample Domain through it, and browse
the web app against a mock backend.

| Step | What you get | State |
|------|--------------|-------|
| 3 | Core engine demo: branches, three-way merge, Conflicts, resolutions, diff | works, no services needed |
| 4 | Unit tests | works |
| 5 | Postgres, migrations and Keeper | works |
| 6, 7 | Create a Domain, apply a schema, import data through the REST API | works, with the limits noted below |
| 8 | Web app in mock mode | works, mock data only |
| later | Read API, Proposals and merge over REST, Python client, CLI, MCP server | [coming next](#coming-next) |

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

`main` is protected by default (`keeper.domains.protect_default_branch`): changes to it must go
through a Proposal, and the Proposals API is not there yet, so a direct change answers `409
protected-branch`. Work on another branch instead:

```bash
curl -s -X POST http://localhost:8080/api/v1/domains/supply/branches \
  -H 'Content-Type: application/json' \
  -d '{"name": "work"}'
```

The schema of the sample Domain has two Operations and one Derivation written in CEL. Expressions
are not enabled yet, so Keeper refuses them (the `keeper.operations` setting
`skip_expression_preconditions` only skips CEL preconditions when an Operation runs, it does not
make schemas with expressions valid). You can see the refusal with the validate endpoint, which
writes nothing:

```bash
curl -s -X POST 'http://localhost:8080/api/v1/domains/supply/schema:validate?branch=work' \
  -H 'Content-Type: text/yaml' --data-binary @examples/supply/schema.yaml
```

The answer has `"valid":false` and three `schema.expressions_disabled` issues. Strip those
elements into a copy of the schema:

```bash
uv run python - <<'EOF'
import pathlib

import yaml

doc = yaml.safe_load(pathlib.Path("examples/supply/schema.yaml").read_text())
for name in ("flag_supplier_risk", "approve_contract"):
    del doc["operations"][name]
del doc["derivations"]["single_sourced"]
del doc["kinds"]["Part"]["attributes"]["single_sourced"]
pathlib.Path("/tmp/supply-schema.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
EOF
```

Apply it to `work` as one schema change set (a YAML body is the full schema document; options
go in the query string):

```bash
curl -s -X POST 'http://localhost:8080/api/v1/domains/supply/branches/work/schema/changes?message=Apply%20supply%20schema' \
  -H 'Content-Type: text/yaml' --data-binary @/tmp/supply-schema.yaml
```

The answer is `"status":"committed"` with the new Revision and 18 changes (the Domain, six
Kinds, seven Relations, a Trait, a Derivation and two Sources). Read the schema back:

```bash
curl -s 'http://localhost:8080/api/v1/domains/supply/schema?branch=work&format=yaml'
```

## 7. Import the sample data

Every write goes through an Operation. The built-in `import_records` and `import_links`
Operations take rows keyed by Attribute api name; booleans must be JSON booleans and Link rows
use `from` and `to`. This script posts the CSV files of `examples/supply/data` to the `work`
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


for kind, file in (("Supplier", "supplier"), ("Part", "part"), ("Plant", "plant")):
    result = execute("import_records", {"kind": kind, "rows": rows(file)})
    print(kind, result["status"], result["edit_count"], "edits")
result = execute("import_links", {"relation": "supplied_by", "rows": rows("supplied_by")})
print("supplied_by", result["status"], result["edit_count"], "edits")
EOF
```

```text
Supplier committed 30 edits
Part committed 40 edits
Plant committed 20 edits
supplied_by committed 48 edits
```

Add `"dry_run": true` next to `"branch"` to compute the edits and violations without committing.
Rows that break the schema are rejected as a whole with `422` and `issues` that point at the row
and Attribute.

Each import is a Revision; the branch moved ahead of `main`:

```bash
curl -s http://localhost:8080/api/v1/domains/supply/branches/work
```

There is no endpoint to read Records yet. To look at what was materialized, query Postgres
(the tables are under row-level security, so the session names the Domain first):

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml exec -T postgres \
  psql -U lorebase -d lorebase \
  -c "set lorebase.domain_ids = 'supply'" \
  -c "select branch, kind_id, count(*) from mat_records group by 1, 2 order by 2" \
  -c "select key, title from mat_records where key = 'S-1001'"
```

You should see 30 Suppliers, 40 Parts and 20 Plants on `work`, and `S-1001 | Kavsen Alloys Sp. z o.o.`.

## 8. Web app in mock mode

The web app talks to a `KeeperApi` interface with two adapters. In `mock` mode it serves
its own sample Domains from memory and needs no backend; the `supply` Domain you created above
is not shown there. `live` mode points at Keeper but most screens need read endpoints that do not exist yet,
so use mock mode for now.

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

- **Read API** (T20): Records and Links, the query DSL, search, time travel. Until then, the only
  way to look at data is SQL.
- **Revisions, log and diff API** (T21) and **Proposals, merge and Conflict resolution API**
  (T22). The merge engine is complete and exercised by the demo in step 3, but nothing in Keeper
  calls it yet, which is why `main` cannot be changed over REST.
- **Lineage and history API** (T23), **Python client** (T24) and **CLI** (T25).
- **Herald, the MCP server** (M3), and expressions in schemas: custom Operations are M3 and
  Derivations M4 on the roadmap.
- Source syncs from CSV or Postgres (`approved.suppliers`, `erp.purchase_orders` in the sample
  schema are declared but nothing runs them), document extraction and semantic search: M2 and
  M4.
