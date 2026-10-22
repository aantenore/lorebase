# Examples

Run everything from the repository root after `uv sync --all-packages`.

| Path | What it shows | Needs |
|------|---------------|-------|
| [merge_demo.py](merge_demo.py) | The core engine on the supply Domain, in memory: load the schema and CSV data into `main`, branch `model/risk` and `source/erp`, change both, three-way merge, list the Conflicts by kind, resolve them, merge again, diff two Revisions. Deterministic output. | nothing (no Postgres, no network) |
| [schema_as_code.md](schema_as_code.md) | Editing `schema.yaml` as code: add a Kind, rename an Attribute and keep its id, validate with the loader, write it back. Every snippet runs. | nothing |
| [five_day_demo/](five_day_demo) | The 5-day proof scenario of [design 3.2](../docs/design.md#32-the-5-day-proof-scenario) with the `lorebase` CLI against a running stack: schema inference, branches, Conflicts, a Source sync, a webhook, an agent over MCP, a backup. `make demo` brings the stack up, runs it and removes it. Narrative and transcript: [docs/demo.md](../docs/demo.md). | Docker (`make demo`), or any Keeper |
| [supply/](supply/README.md) | The sample Domain: schema, hand-sized CSV data, a generator for larger datasets and sample documents. Load it into Keeper with the CLI (below). | nothing to read; Keeper and Postgres to load |

```bash
uv run python examples/merge_demo.py
```

The demo is covered by `tests/tooling/test_merge_demo.py`.

## Loading the supply sample into a running Keeper

`make example-data` does all of it in one command and merges the result into `main`
([scripts/quickstart.sh](../scripts/quickstart.sh)). By hand: with Keeper up (see
[the quickstart](../docs/quickstart.md#2-five-minute-path)), the `lorebase` CLI creates the Domain, applies the schema as is (CEL Operations and Derivations
included) and loads the CSV files in `supply/data` (it converts the cells to the types of the
schema and sends them in batches). `Contract`, `PurchaseOrder` and `RiskFlag` need a Link when
they are created, so `import batch` loads them with the Link files and keeps each Record with
its Links in one request:

```bash
export LOREBASE_URL=http://localhost:8080 LOREBASE_DOMAIN=supply
uv run lorebase domain create supply --name "Supply chain"
uv run lorebase branch create work
uv run lorebase schema apply examples/supply/schema.yaml -b work
D=examples/supply/data
uv run lorebase import batch -b work \
  --records Supplier=$D/supplier.csv --records Part=$D/part.csv --records Plant=$D/plant.csv \
  --records Contract=$D/contract.csv --records PurchaseOrder=$D/purchase_order.csv \
  --records RiskFlag=$D/risk_flag.csv \
  --links supplied_by=$D/supplied_by.csv --links stocked_at=$D/stocked_at.csv \
  --links with_supplier=$D/with_supplier.csv --links for_part=$D/for_part.csv \
  --links delivered_to=$D/delivered_to.csv --links flags=$D/flags.csv
uv run lorebase record get Supplier S-1001 -b work
```

[The quickstart](../docs/quickstart.md#11-import-the-sample-data-and-read-it-back) loads the same
six Kinds (`import records` and `import links` for three, `import batch` for the three
that need Links), shows `Part.supplier_count` and `Part.single_sourced` computed by the
materializer, and runs `flag_supplier_risk` and `approve_contract` through the Operation
endpoint. The Python client (`lorebase_client`) offers the same calls from code, for example:

```python
from lorebase_client import LorebaseClient

with LorebaseClient() as client:  # LOREBASE_URL, LOREBASE_API_KEY
    for record in client.records.iterate("supply", "Supplier", branch="work"):
        print(record.key, record.title)
```
