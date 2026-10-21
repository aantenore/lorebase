# Schema inference from tables and CSV files

Point Lorebase at the tables of a client, as CSV files or as a Postgres schema, and get a
proposed ontology to review: Kinds, typed Attributes with constraints, primary keys and
Relations with cardinality. Inference is extraction tier 0 ([design 9.1 and 9.2](design.md#91-structured-sources)):
rules only, deterministic, no model call. It never commits anything: the proposal is a draft
that a modeler edits and applies as a schema change set on a branch.

| Piece | Where |
|-------|-------|
| Inference rules (pure, no I/O) | `lorebase_core.infer`: `infer_schema(tables, domain, options)` |
| CSV/TSV and Postgres profilers | `lorebase_store.profiling`: `profile_file`, `profile_bytes`, `profile_postgres` |
| REST endpoint | `POST /api/v1/domains/{d}/schema:infer` (permission `schema.write`, audited as `schema.infer`) |
| Settings | `keeper.schema_inference` and `store.profiling` in [config/lorebase.example.yaml](../config/lorebase.example.yaml) |

## Using it

Upload CSV or TSV files (the file name is the table name):

```bash
curl -X POST "$LOREBASE/api/v1/domains/supply/schema:infer" \
  -H "Authorization: Bearer $LOREBASE_KEY" \
  -F files=@supplier.csv -F files=@purchase_order.csv -F files=@supplied_by.csv \
  -F 'options={"enum_max_cardinality": 20}'
```

Or name a Postgres source. The connection is a secret reference that an administrator listed in
`keeper.schema_inference.allowed_sources`; callers never send connection strings, and the
tables are read in a read-only transaction with a statement timeout. `allowed_sources` applies to
every Domain, so `schema.write` in any Domain may profile those sources. To bind sources to
Domains, list them in `keeper.schema_inference.domain_sources` (Domain id to secret references):
a listed Domain may name only its own references (`[]` names none) and `allowed_sources` no
longer applies to it. A refused source is reported without naming the sources of other Domains:

```bash
curl -X POST "$LOREBASE/api/v1/domains/supply/schema:infer" \
  -H "Authorization: Bearer $LOREBASE_KEY" -H "Content-Type: application/json" \
  -d '{"postgres": {"dsn": "secret://env/ERP_DSN", "schema": "public", "tables": ["vendors", "orders"]}}'
```

The JSON answer holds `schema` (elements keyed by id), `yaml` (the same schema as a
[schema document](design.md#513-schema-as-code)), `evidence` (one entry per element),
`issues` and `tables` (what each table became). With `Accept: text/yaml` or `?format=yaml` the
answer is the YAML document only. From Python:

```python
from lorebase_core.infer import infer_schema
from lorebase_core.model import dump_schema
from lorebase_store.profiling import profile_file
from lorebase_store.settings import CsvProfilingSettings

tables = [profile_file(path, CsvProfilingSettings()) for path in paths]
proposal = infer_schema(tables, "supply")
print(dump_schema(proposal.schema))
```

## How it works

1. **Profiling.** A profiler reads each table into a `TableProfile`: column names, sampled
   values and the row count, plus what a catalog declares (types, nullability, primary key,
   unique constraints, foreign keys, comments). CSV values stay text; the delimiter is sniffed
   (`.tsv` files use a tab), encodings are tried in order, every row is counted and
   `sample_rows` are kept (reservoir sampling by default, seeded, in file order).
2. **Types.** Each column's non-null values are tested in precedence order: `boolean`,
   `integer`, `decimal` (at most `decimal_max_scale` fractional digits; precision is the largest
   integer part plus headroom plus scale), `float`, `date`, `timestamp` (RFC 3339, or the
   `timestamp_formats` read as UTC), `json`. The first type that all values satisfy (or all but
   `type_tolerance` of them) wins. Otherwise the column is textual: `enum` when it has at most
   `enum_max_cardinality` distinct short values and a distinct ratio of at most
   `enum_max_distinct_ratio` (numbered codes such as `analyst-1` are never enums), `text` when
   values are longer than `text_min_length` or are prose under a name in `text_name_hints`,
   `string` otherwise. Strings get `max_length` (the longest value rounded up to a bucket) and,
   for code-like values sharing one shape, a `pattern` such as `^S-[0-9]{4,}$`; numbers that
   are never negative get `min: 0`. Declared catalog types win over detection (`numeric(p,s)`
   to `decimal(p,s)`, `varchar(n)` to `string` with `max_length n`, `text` to `text`,
   `timestamptz` to `timestamp`, enums to `enum`).
3. **Nullability and uniqueness.** A column with at most `required_max_null_ratio` nulls is
   `required`. Values are unique when no two are equal. A unique non-key column is flagged
   `unique` only when declared so or named like an identifier (`unique_name_patterns`).
4. **Primary keys.** Candidates are required, unique `string`, `integer`, `date` or `uri`
   columns, ranked by `key_name_patterns` (`id`, `key`, `code`, `{table}_id`, `{table}_code`,
   `{table}_number`, then `*_id`, `*_code`...). Without a named candidate the first column may be
   the key; without any single candidate, pairs of key-named or reference-named columns are
   tried. A key column named `key` holding generated ids (ULID or UUID) carries Record keys: the
   Kind uses minted keys. No key at all: minted keys, reported as an issue.
5. **References.** A column references another table when at least `min_inclusion_ratio` of
   its distinct values are values of that table's key, the value families match and, for
   integers, the name looks like a reference. Tables whose key is included in another table's
   key are link or extension tables and are never targets; when two keys hold the same values,
   the table with more columns owns them. Name hints (`supplier_id`, `supplier`, the target's
   key name) raise confidence and break ties.
6. **Relations.** A reference column becomes a `ref` Attribute and a `many_to_one` Relation
   with `foreign_key` backing, named after the column without its suffix (`supplier_id` to
   `supplier`), with the plural of the from Kind as inverse (`purchase_orders`), or `<name>_of`
   for self-references. A table with exactly two required references that form its key is a
   **junction table**: it becomes a Relation named after the table, `many_to_many` when the pair
   is the key, `many_to_one` when one reference alone is the key (that side is the from side),
   with its other columns as link Attributes. `one_to_one` is only proposed from declared
   unique constraints or a reference that is its table's key.
7. **Names and ids.** Table and column names are normalized to `snake_case`; Kinds are the
   `PascalCase` singular of table names (`purchase_orders` to `PurchaseOrder`); reserved
   Attribute names (`key`, `kind`, `ref`, `revision`) become `<kind>_<name>`; collisions get a
   suffix and an issue. Ids are minted from a hash of `id_seed`, the Domain and the source
   names, so the same input gives the same ids and api names in any table order.
8. **Validation.** The proposal is validated statically; any finding is listed as an
   `infer.schema_invalid` issue. Proposed Kinds and Relations are `draft`, so descriptions can
   be written during review.

## Thresholds

Every threshold is an `InferenceOptions` field with a documented default, configured under
`keeper.schema_inference.options` and overridable per request (`options`). The main ones:

| Option | Default | Effect |
|--------|---------|--------|
| `sample_size` | 10000 | values read per column |
| `type_tolerance` | 0.0 | share of values allowed not to parse as the detected type |
| `decimal_max_scale` | 4 | more fractional digits make a `float` |
| `enum_max_cardinality`, `enum_max_distinct_ratio`, `enum_min_values` | 12, 0.5, 10 | when text is an `enum` |
| `text_min_length` | 256 | longer values make `text` |
| `required_max_null_ratio` | 0.0 | share of nulls still `required` |
| `key_name_patterns` | `id`, `key`, `code`, `{table}_id`... | ranked primary key names |
| `min_inclusion_ratio` | 0.95 | share of values found in a key for a Relation |
| `junction_max_attributes` | 8 | link Attributes a junction table may carry |
| `title_names` | `name`, `title`, `label`... | ranked title Attribute names |
| `id_seed` | `lorebase.infer` | seed of the deterministic ids |

Confidence is the share of values supporting a decision, scaled down below
`min_support_values` non-null values and by `sampled_uniqueness_factor` when only a sample of a
larger table was read; a name hint that only has a reference suffix multiplies a reference by
`weak_name_factor`, no hint by `no_name_factor`.

## Reviewing the proposal

Read the issues first, then the elements with the lowest confidence:

- `infer.ambiguous_primary_key`: two or more key candidates (for example `supplier_code` and
  `vat_number`); the first was chosen. Swap `primary_key` if needed.
- `infer.no_primary_key`: Records will get minted keys; add a key if the source has one.
- `infer.mixed_types`: most values parse as a type and some do not (outliers are listed): clean
  the source, raise `type_tolerance`, or keep the text type.
- `infer.ambiguous_reference`: a column matches several keys equally; check the chosen target.
- `infer.sampled`: uniqueness and references were checked on a sample only.
- `infer.name_adjusted`: a name was changed to stay valid or unique.

Then, element by element: confirm enums (low-cardinality integers such as tiers stay
`integer`, and short country codes may become enums), tighten or drop proposed `pattern` and
`max_length` constraints, rename Relations to business verbs (`supplier` to `issued_to`,
`parts` to `supplies`), write descriptions, set `indexed` and `sensitivity`, and activate the
elements. Apply the edited document with `POST /domains/{d}/branches/{b}/schema/changes` on a
work branch and merge it through a Proposal.

On the demo data (`examples/supply/data`) inference recovers all six Kinds and keys
(`RiskFlag` with minted keys), every Attribute name, 44 of 47 Attribute types (`Supplier.tier`
is an integer where the gold schema has an enum, and `country` columns are enums where it has
patterned strings) and all seven Relations with their cardinality; see
`packages/core/tests/infer/test_supply_gold.py`.

## What it cannot do

- Infer meaning: descriptions, sensitivity tags, Traits, Operations and Derivations are left to
  the reviewer. Relation names come from column and table names, not from business verbs.
- Composite foreign keys, references to composite keys and references across schemas.
- Prove uniqueness or inclusion on sampled tables: a sample can miss duplicates or the
  referenced keys; declared constraints are the only certainty.
- Tell apart day-month and month-day dates (`date_formats` is explicit), or values that are
  coincidentally unique in small tables.
- Read Parquet files: that needs `pyarrow`, which is not a dependency yet (follow-up), nor
  spreadsheets other than CSV/TSV exports.
- Produce Source mappings: the proposal is a schema; mapping the tables as Sources is a later
  step.
