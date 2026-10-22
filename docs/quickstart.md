# Quickstart

Lorebase is pre-alpha. This page walks through what works on `main` today and says where it
stops. What you can do is run the versioning and merge engine, bring up the Keeper REST API on
Postgres, load the sample Domain through it with curl or with the `lorebase` command line, read it
back (Records, queries, search, history, diff), and browse the web app against a mock backend.
The MCP server (Herald) has its own page: [mcp.md](mcp.md).

| Step | What you get | State |
|------|--------------|-------|
| 3 | Core engine demo: branches, three-way merge, Conflicts, resolutions, diff | works, no services needed |
| 4 | Unit tests | works |
| 5 | Postgres, migrations and Keeper | works |
| 6, 7 | Create a Domain, apply a schema, import data and read it back, with curl and with the CLI | works, with the limits noted below |
| 8 | Web app in mock mode | works, mock data only |
| later | Lineage and history API, expressions in schemas, Source syncs | [coming next](#coming-next) |

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

Edit `.env` and set `POSTGRES_PASSWORD` and `LOREBASE_APP_PASSWORD` (URL-safe characters
only, for example `openssl rand -hex 24`; they are embedded in the connection strings). The
other values have working defaults. Then:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml up -d postgres migrate keeper
```

The first run builds the backend image, which takes a few minutes. Compose starts Postgres 16,
creates the application role Keeper connects as (`db-roles`), runs the Alembic migrations once
(`migrate` exits when done) and starts Keeper on
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
> `"installation_admin":true`). Compose publishes the port on `127.0.0.1` only and sets
> `KEEPER_ALLOW_INSECURE=true`, without which Keeper refuses that mode off loopback. Do not expose it,
> and use `api_key` or `oidc` modes (they need the `database` section and further settings,
> see the comments in the file) for anything shared.

### The first administrator key

In `api_key` mode Keeper accepts no request without a key, and only an administrator can issue
one. The first key comes from the database side, with Keeper's own settings: list the
administrator in `keeper.auth.installation_admins` (for example `[svc_admin]`), then run

```bash
docker compose -f deploy/compose/docker-compose.yaml exec -T keeper \
  python -m lorebase_keeper bootstrap > admin.key      # or `uv run python -m lorebase_keeper bootstrap`
```

It creates the service principal `svc_admin` when it does not exist and a key with the default
lifetime (`keeper.auth.api_keys.default_ttl_days`), writes an `apikey.bootstrap` entry to the audit
log, and prints the key once on standard output (messages go to standard error). It refuses when
an installation administrator already holds a usable key: issue further keys with that one, or
pass `--force` to add another (existing keys stay valid; revoke them over the API).
It connects as the owner role of `database.maintenance_dsn` when that is set (compose sets it), else
with `database.dsn`.
`--principal` picks another entry of `installation_admins`. The administrator creates a Domain and
holds `admin` in it; `python -m lorebase_keeper principal create ID` adds a service principal with a
chosen id, for a schema that names it in Operation `permissions`.

## 6. Create a Domain and apply the supply schema

Each step is shown twice: with curl against the REST API, and with the `lorebase` command line
(installed by `uv sync --all-packages`, run as `uv run lorebase`). Both call the same
endpoints. Tell the CLI where Keeper is:

```bash
export LOREBASE_URL=http://localhost:8080
export LOREBASE_DOMAIN=supply        # the default for --domain
uv run lorebase whoami
```

With `keeper.auth.mode: api_key` or `oidc` also set `LOREBASE_API_KEY` (or `LOREBASE_TOKEN`, an
OIDC access token). Instead of environment variables you can keep a profile with the URL,
credentials, `domain` and `branch` in `~/.config/lorebase/config.toml` (see
[the settings module](../packages/cli/src/lorebase_cli/settings.py)). Every command takes `--json`
for machine-readable output; exit codes are 0 success, 1 usage or not found, 2 validation or
conflict, 3 authentication or permission, 4 server error. `lorebase --install-completion` sets
up shell completion. The same API is available from Python (`lorebase_client`, sync and async).

Create the Domain `supply`. It starts with one Revision and a protected `main` branch.

```bash
# curl
curl -s -X POST http://localhost:8080/api/v1/domains \
  -H 'Content-Type: application/json' \
  -d '{"id": "supply", "display_name": "Supply chain"}'

# CLI
uv run lorebase domain create supply --name "Supply chain"
```

`main` is protected by default (`keeper.domains.protect_default_branch`): changes to it go
through a Proposal (`POST /api/v1/domains/supply/proposals` or `lorebase proposal create`, then a
review and a merge; approvals are set by `keeper.proposals` and the Domain's `required_approvals`
setting), so a direct change answers `409 protected-branch` (exit code 2 in the CLI). Work on
another branch:

```bash
# curl
curl -s -X POST http://localhost:8080/api/v1/domains/supply/branches \
  -H 'Content-Type: application/json' \
  -d '{"name": "work"}'

# CLI
uv run lorebase branch create work
```

`--mode copy|overlay` chooses how the branch stores its rows (own copy, or only its changes); without it `store.materialize.branch_mode` applies. A copy of a large branch is usable at once and finishes copying in the background; `--wait` waits for it ([operations](operations.md#large-branches)).

The schema of the sample Domain has two Operations (`flag_supplier_risk`, `approve_contract`)
and two Derivations (`Part.supplier_count`, `Part.single_sourced`); the Operations and
`single_sourced` are written in CEL. Expressions are type checked against the schema when it is
applied (`schema.expressions_enabled`, on by default). Check it first with the validate
endpoint, which writes nothing:

```bash
# curl
curl -s -X POST 'http://localhost:8080/api/v1/domains/supply/schema:validate?branch=work' \
  -H 'Content-Type: text/yaml' --data-binary @examples/supply/schema.yaml

# CLI (exit code 2 when the document is not valid)
uv run lorebase schema validate examples/supply/schema.yaml -b work
```

The answer has `"valid":true`. Apply it to `work` as one schema change set (a YAML body is the
full schema document; options go in the query string). `schema diff` shows what would change
first:

```bash
# curl
curl -s -X POST 'http://localhost:8080/api/v1/domains/supply/branches/work/schema/changes?message=Apply%20supply%20schema' \
  -H 'Content-Type: text/yaml' --data-binary @examples/supply/schema.yaml

# CLI
uv run lorebase schema diff examples/supply/schema.yaml -b work
uv run lorebase schema apply examples/supply/schema.yaml -b work -m "Apply supply schema"
```

The answer is `"status":"committed"` with the new Revision and 21 changes (the Domain, six
Kinds, seven Relations, a Trait, two Derivations, two Operations and two Sources). Read the schema back:

```bash
# curl
curl -s 'http://localhost:8080/api/v1/domains/supply/schema?branch=work&format=yaml'

# CLI (YAML by default, JSON with --json)
uv run lorebase schema get -b work
```

## 7. Import the sample data and read it back

Every write goes through an Operation. The built-in `import_records`, `import_links` and
`import_batch` Operations take rows keyed by Attribute api name; booleans must be JSON booleans
and Link rows use `from` and `to`. A Record row may carry `links`, a map from a Relation name to
the key (or keys) at the other end: `Contract`, `PurchaseOrder` and `RiskFlag` need their
`with_supplier`, `for_part` and `flags` Link to exist, so each one is committed in the same
Revision as its Links. Imports upsert, so running one twice is safe.

The CLI does the conversions for you: it reads the schema of the branch, turns `true` and
`false` into booleans, keeps integers and decimals exact, leaves empty cells unset, maps
`from_key` and `to_key` to `from` and `to`, and sends the file in batches (one Revision each,
`--batch-size`) with progress on standard error. `import records` and `import links` load the
Kinds that need no Link on creation and the Link files:

```bash
# CLI
uv run lorebase import records Supplier examples/supply/data/supplier.csv -b work
uv run lorebase import records Part examples/supply/data/part.csv -b work
uv run lorebase import records Plant examples/supply/data/plant.csv -b work
uv run lorebase import links supplied_by examples/supply/data/supplied_by.csv -b work
uv run lorebase import links stocked_at examples/supply/data/stocked_at.csv -b work
```

```text
batch 1/1: rows 1-30 committed, 30 edits
field      value
record     Supplier
branch     work
mode       committed
rows       30
edits      30
batches    1
revisions  3403d8d356d9
```

`Contract`, `PurchaseOrder` and `RiskFlag` need a Link when they are created, so their Link
files travel with them. `import batch` takes a Kind file per `--records` and a Relation file per
`--links` (same formats and conversions) and cuts them into requests that never separate a
Record from its Links: a Link goes with the Record at its `from` end, or at its `to` end when
that Record is not in the import. Records without Links go first, so name the Kinds that others
point to before the ones that point to them. `--batch-size` counts Records and Links together:

```bash
# CLI
uv run lorebase import batch -b work \
  --records Contract=examples/supply/data/contract.csv \
  --records PurchaseOrder=examples/supply/data/purchase_order.csv \
  --records RiskFlag=examples/supply/data/risk_flag.csv \
  --links with_supplier=examples/supply/data/with_supplier.csv \
  --links for_part=examples/supply/data/for_part.csv \
  --links delivered_to=examples/supply/data/delivered_to.csv \
  --links flags=examples/supply/data/flags.csv
```

```text
batch 1/1: rows 1-240 committed, 240 edits
field      value
batch      Contract, PurchaseOrder, RiskFlag, with_supplier, for_part, delivered_to, flags
branch     work
mode       committed
rows       240
edits      240
batches    1
revisions  2d84947d8d75
```

The same call exists as the `import_batch` Operation, which takes `records` (rows with a `kind`)
and `links` (rows with a `relation`, `from` and `to`) and commits a Record with its Links in one
Revision. This one only computes the edits (`dry_run`):

```bash
# curl
curl -s -X POST http://localhost:8080/api/v1/domains/supply/operations/import_batch/execute \
  -H 'Content-Type: application/json' \
  -d '{"branch": "work", "dry_run": true, "parameters": {
        "records": [{"kind": "Contract", "contract_number": "C-3999", "title": "Spot buy",
                     "start_date": "2026-02-01"}],
        "links": [{"relation": "with_supplier", "from": "C-3999", "to": "S-1001"}]}}'
```

When a file already has a column with the key at the other end, `import records` sends it as the
Link of each row: `import records Contract contracts.csv --link with_supplier=supplier_code`
(repeatable; JSON Lines and Parquet cells may hold a list of keys). Files can be CSV, TSV, JSON
Lines, a JSON array or Parquet (`pip install 'lorebase-cli[parquet]'`); `--map
file_column=attribute` renames columns, `--dry-run` computes without committing. A rejected
request names the file and row of each issue and how many rows are already committed.

Add `"dry_run": true` next to `"branch"` (CLI: `--dry-run`) to compute the edits and
violations without committing. Rows that break the schema are rejected as a whole with `422` and
`issues` that point at the row and Attribute; the CLI prints them with the row numbers of the
file and says which earlier rows are already committed.

Each import is a Revision; the branch moved ahead of `main`:

```bash
# curl
curl -s http://localhost:8080/api/v1/domains/supply/branches/work

# CLI
uv run lorebase branch list
uv run lorebase log work -n 3
```

### Read it back

Reads go through the REST API and need no model. Fetch one Record with its Lineage, list and
query with the JSON DSL (the query of the [design](design.md#82-query-model); `branch` picks where
to read), search and diff, on `work`:

```bash
# curl
curl -s 'http://localhost:8080/api/v1/domains/supply/kinds/Supplier/records/S-1001?branch=work&lineage=true'
curl -s -G 'http://localhost:8080/api/v1/domains/supply/kinds/Part/records' \
  --data-urlencode 'branch=work' --data-urlencode 'limit=5' \
  --data-urlencode 'where={"attr":"category","op":"eq","value":"bearing"}'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/query \
  -H 'Content-Type: application/json' \
  -d '{"kind": "Part", "branch": "work", "aggregate": {"group_by": "category"}}'
curl -s -X POST http://localhost:8080/api/v1/domains/supply/search \
  -H 'Content-Type: application/json' -d '{"branch": "work", "q": "bearing", "kinds": ["Part"]}'
curl -s 'http://localhost:8080/api/v1/domains/supply/diff?from=main&to=work&kinds=Supplier&limit=3'

# CLI
uv run lorebase record get Supplier S-1001 -b work
uv run lorebase record list Part -b work -n 5 \
  --where '{"attr": "category", "op": "eq", "value": "bearing"}' --select category --select unit_cost
uv run lorebase query '{"kind": "Part", "aggregate": {"group_by": "category"}}' -b work
uv run lorebase search bearing --kind Part -b work
uv run lorebase diff main work --kind Supplier -n 3
```

```text
key      title                        category  unit_cost
P-10001  Tapered roller bearing 6305  bearing   10.79
P-10010  Needle roller bearing 6305   bearing   40.48
P-10013  Needle roller bearing NK25   bearing   25.62
P-10023  Needle roller bearing NK25   bearing   37.59
P-10029  Needle roller bearing 6001   bearing   45.94
```

The same API serves `traverse`, `neighborhood`, the branch `log`, `revisions/{id}`, context
packs and Record explanations. Every response carries the Revision it was served from in the
`Lorebase-Revision` header and an `ETag`. The full list is in the OpenAPI docs at
`http://localhost:8080/api/v1/docs` and in [openapi.json](openapi.json). If you want to look at
the materialized rows directly, query Postgres (the tables are under row-level security, so the
session names the Domain first):

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml exec -T postgres \
  psql -U lorebase -d lorebase \
  -c "set lorebase.domain_ids = 'supply'" \
  -c "select branch, kind_id, count(*) from mat_records group by 1, 2 order by 2" \
  -c "select key, title from mat_records where key = 'S-1001'"
```

You should see 30 Suppliers, 40 Parts, 20 Plants, 25 Contracts, 50 PurchaseOrders and 20
RiskFlags on `work`, and `S-1001` titled `Kavsen Alloys Sp. z o.o.`.

### Propose the changes to `main`

`main` takes changes through a Proposal. The CLI covers the flow (`proposal create`, `list`,
`get`, `review`, `merge`, `close`, `conflicts` and `resolve`; each takes `--json`):

```bash
uv run lorebase proposal create work main --title "Load the supply sample"
uv run lorebase proposal list --status open
uv run lorebase proposal review <id> approve
uv run lorebase proposal merge <id>
```

The development config answers every request as the same anonymous administrator, and
`keeper.proposals.allow_self_approval` is `false`, so the approval has to come from another
principal; for a local trial set `allow_self_approval: true`. Open Conflicts or missing approvals
refuse the merge with exit code 2. `uv run lorebase context --ref Supplier/S-1001 -b work` and
`uv run lorebase explain Supplier S-1001 -b work` print what an agent gets from the context pack
and Record explanation endpoints ([agents.md](agents.md)).

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

## 8. Web app

The web app talks to a `KeeperApi` interface with two adapters. In `mock` mode it serves
its own sample Domains from memory and needs no backend; the `supply` Domain you created above
is not shown there. In `live` mode it talks to Keeper, and the flows of the app (Explorer, the
Canvas, the Modeler through a Proposal, Review with Conflicts, Settings) are tested against a
real Keeper on every change.

```bash
cd apps/web
pnpm install
cp .env.example .env     # VITE_API_MODE=mock
pnpm dev                 # http://localhost:5173, sample data
```

To see the Domain of the steps above instead, point the dev server at Keeper. The proxy makes
the browser talk to the app's own origin, so Keeper needs no CORS setting:

```bash
printf 'VITE_API_MODE=live\nVITE_KEEPER_BASE_URL=/\n' > .env.local
LOREBASE_KEEPER_PROXY=http://localhost:8080 pnpm dev
```

Pick the `supply` Domain and the `work` or `main` Branch in the top bar. `main` is protected, so
a schema change there goes through a Branch and a Proposal, and merging it needs an approval
(`keeper.proposals.allow_self_approval: true` for a trial with authentication off). See
[apps/web/README.md](../apps/web/README.md#live-mode) for the variables, the API token and the
limits against Keeper, and for the end-to-end suite (`pnpm e2e`).

## 9. Stop and clean up

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.yaml down      # keep the data
docker compose --env-file .env -f deploy/compose/docker-compose.yaml down -v   # delete the data
```

Stop `pnpm dev` with Ctrl+C.

## Next steps

- Plug in an agent: [examples/agent](../examples/agent/README.md) reads the `supply` Domain through Herald and proposes changes as Proposals, with Ollama or a hosted model.

## Coming next

These do not exist yet; the order follows the [M1 plan](plan/m1.md).

- **Lineage and history API** (T23; the per-Record lineage summary is already on Record reads).
- Source syncs from CSV or Postgres (`approved.suppliers`, `erp.purchase_orders` in the sample
  schema are declared but nothing runs them), document extraction and semantic search: M2 and
  M4.
