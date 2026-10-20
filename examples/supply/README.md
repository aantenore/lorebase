# Demo Domain: supply

The pilot Domain of the proof scenario in [design 3.2](../../docs/design.md): the supply chain of
Corvane Industries, a fictional distributor of industrial components. The questions it is built
to answer are the ones of day 5: which parts depend on a single supplier whose contract ends
within 90 days, and what is the risk behind them. All names are invented; any resemblance to
real companies is a coincidence.

## The story in one paragraph

Corvane buys bearings, seals, filters, motors and other parts from about thirty suppliers in
several countries and keeps them in twenty plants and warehouses. Procurement keeps a CSV list of
approved suppliers, the ERP holds the purchase orders, and contracts live in documents.
Most parts have exactly one supplier. Risk analysts flag suppliers; contract managers approve
contracts. The Domain models this so that an agent can read it in milliseconds and make
governed writes through two Operations.

## Contents

| Path | What it is |
|------|------------|
| `schema.yaml` | The Domain schema in the YAML format of `lorebase_core.model.yaml_schema` |
| `data/*.csv` | Small hand-sized dataset, consistent with the schema and with itself |
| `scripts/make_supply_data.py` | Deterministic generator of the same dataset at any scale |
| `documents/*.md` | Sample contract text, a supplier review and a quality notice for extraction demos |

## Schema at a glance

- **Kinds:** `Supplier`, `Part`, `Plant`, `PurchaseOrder`, `Contract` (natural keys such as
  `S-1001`, `P-10001`, `PL-01`, `PO-200001`, `C-3001`) and `RiskFlag` (minted keys).
- **Trait:** `Located` (`country`, `city`), implemented by `Supplier` and `Plant`.
- **Relations:** `Part supplied_by Supplier` and `Part stocked_at Plant` (many to many, with
  link Attributes), `PurchaseOrder issued_to Supplier` (many to one, derived from the
  `supplier` Attribute), `PurchaseOrder for_part Part`, `PurchaseOrder delivered_to Plant`,
  `Contract with_supplier Supplier` and `RiskFlag flags Supplier` (many to one).
- **Operations:** `flag_supplier_risk` (critical flags go to a Proposal) and `approve_contract`
  (contracts above 500000 go to a Proposal). Preconditions, step values and review policies are
  CEL source strings; static validation needs `SchemaRules(allow_expressions=True)`.
- **Derivations:** `Part.supplier_count` (aggregate over `supplied_by`) and
  `Part.single_sourced` (expression over `supplier_count`).
- **Sources:** `approved.suppliers` (CSV, snapshot, auto-merge when clean) and
  `erp.purchase_orders` (Postgres table, incremental by watermark, auto-merge for Attributes the
  ERP owns). Connections are secret references such as `secret://env/ERP_DSN`, never literals.

## Data

One CSV per Kind and per explicit Relation, between 20 and 50 rows each. Record files use the
Attribute api names as header. Link files start with `from_key,to_key` and then hold the link
Attributes. `RiskFlag` has minted keys, so `risk_flag.csv` carries them in a `key` column.
`issued_to` has no file: its Links derive from the `supplier` column of `purchase_order.csv`.

References are consistent: every order buys a part from one of its suppliers and ships it to a
plant that stocks it, every contract and risk flag points at an existing supplier, and some
contracts end within 90 days of the reference date 2026-01-15.

To regenerate the committed files, or to build a larger dataset with the same shape:

```sh
python examples/supply/scripts/make_supply_data.py                      # rewrite data/
python examples/supply/scripts/make_supply_data.py --scale 200 --out /tmp/supply-large
```

`--scale` multiplies every row count (scale 1 is the committed dataset); `--seed` and `--as-of`
change the random seed and the reference date. The output is a pure function of these flags.

## Using the schema

```python
from pathlib import Path

from lorebase_core.model import load_schema, validate_schema
from lorebase_core.validate import SchemaRules

schema = load_schema(Path("examples/supply/schema.yaml").read_text())
assert not validate_schema(schema, SchemaRules(allow_expressions=True))
```

Elements are keyed by api name and carry their stable `id`. Rename a key and keep the `id` and
history follows the element; delete the `id` of a new element and `load_schema` mints one (or
reuses the one of the same api name when you pass `base=`).
