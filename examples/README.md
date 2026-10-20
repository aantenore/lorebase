# Examples

Run everything from the repository root after `uv sync --all-packages`.

| Path | What it shows | Needs |
|------|---------------|-------|
| [merge_demo.py](merge_demo.py) | The core engine on the supply Domain, in memory: load the schema and CSV data into `main`, branch `model/risk` and `source/erp`, change both, three-way merge, list the Conflicts by kind, resolve them, merge again, diff two Revisions. Deterministic output. | nothing (no Postgres, no network) |
| [schema_as_code.md](schema_as_code.md) | Editing `schema.yaml` as code: add a Kind, rename an Attribute and keep its id, validate with the loader, write it back. Every snippet runs. | nothing |
| [supply/](supply/README.md) | The sample Domain: schema, hand-sized CSV data, a generator for larger datasets and sample documents. | nothing |

```bash
uv run python examples/merge_demo.py
```

The demo is covered by `tests/tooling/test_merge_demo.py`. To load the supply sample into a
running Keeper through the REST API, follow [the quickstart](../docs/quickstart.md#6-create-a-domain-and-apply-the-supply-schema):
the schema is applied as is, CEL Operations and Derivations included, the six Kinds are imported
with their Links, `Part.supplier_count` and `Part.single_sourced` are computed by the
materializer, and `flag_supplier_risk` and `approve_contract` run through the Operation endpoint.
