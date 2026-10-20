# Examples

Run everything from the repository root after `uv sync --all-packages`.

| Path | What it shows | Needs |
|------|---------------|-------|
| [merge_demo.py](merge_demo.py) | The core engine on the supply Domain, in memory: load the schema and CSV data into `main`, branch `model/risk` and `source/erp`, change both, three-way merge, list the Conflicts by kind, resolve them, merge again, diff two Revisions. Deterministic output. | nothing (no Postgres, no network) |
| [schema_as_code.md](schema_as_code.md) | Editing `schema.yaml` as code: add a Kind, rename an Attribute and keep its id, validate with the loader, write it back. Every snippet runs. | nothing |
| [supply/](supply/README.md) | The sample Domain: schema, hand-sized CSV data, a generator for larger datasets and sample documents. Load it into Keeper with the CLI (below). | nothing to read; Keeper and Postgres to load |

```bash
uv run python examples/merge_demo.py
```

The demo is covered by `tests/tooling/test_merge_demo.py`.

## Loading the supply sample into a running Keeper

With Keeper up (see [the quickstart](../docs/quickstart.md#5-start-postgres-and-keeper)), the
`lorebase` CLI creates the Domain, applies the schema and loads the CSV files in `supply/data`
(it converts the cells to the types of the schema and sends them in batches):

```bash
export LOREBASE_URL=http://localhost:8080 LOREBASE_DOMAIN=supply
uv run lorebase domain create supply --name "Supply chain"
uv run lorebase branch create work
uv run lorebase schema apply supply-schema.yaml -b work     # the schema without CEL elements
uv run lorebase import records Supplier examples/supply/data/supplier.csv -b work
uv run lorebase import links supplied_by examples/supply/data/supplied_by.csv -b work
uv run lorebase record get Supplier S-1001 -b work
```

The schema in `supply/schema.yaml` has two Operations and one Derivation written in CEL, which
Keeper does not accept yet; [the quickstart](../docs/quickstart.md#6-create-a-domain-and-apply-the-supply-schema)
shows how to strip them into `supply-schema.yaml`, and the same steps with curl. The Python
client (`lorebase_client`) offers the same calls from code, for example:

```python
from lorebase_client import LorebaseClient

with LorebaseClient() as client:  # LOREBASE_URL, LOREBASE_API_KEY
    for record in client.records.iterate("supply", "Supplier", branch="work"):
        print(record.key, record.title)
```
