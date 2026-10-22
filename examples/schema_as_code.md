# Schema as code

A Domain schema is a YAML document ([examples/supply/schema.yaml](supply/schema.yaml)). Elements
are keyed by api name; the `id` next to each one is its stable identity. This walkthrough edits
the supply schema in three ways and checks every step with the loader. Run the snippets from
the repository root, in one Python session or one script (`uv run python`). Nothing is written
to disk until the last step.

## 1. Load and validate

```python
from pathlib import Path

from lorebase_core.model import SchemaYamlError, dump_schema, load_schema, validate_schema_yaml
from lorebase_core.validate import SchemaRules

# The supply schema has Operations and Derivations with CEL expressions.
RULES = SchemaRules(allow_expressions=True)

text = Path("examples/supply/schema.yaml").read_text()
schema = load_schema(text)
print(validate_schema_yaml(text, rules=RULES))  # [] : no issues
```

`load_schema` fails with `SchemaYamlError` when the document cannot be read at all (unknown
keys, unknown Kind or Attribute names). `validate_schema_yaml` loads and then runs the static
rules; each issue has a `path` into the YAML document and a `message`.

## 2. Add a Kind

Write the Kind without ids, insert it before the `relations:` section and pass the old schema
as `base`. Elements without an `id` get a new one; existing elements keep theirs.

```python
carrier = """
  Carrier:
    description: A company that moves goods for Corvane.
    primary_key: [carrier_code]
    title_attribute: name
    attributes:
      carrier_code: {type: string, required: true}
      name: {type: string, required: true}
"""
edited = text.replace("\nrelations:\n", carrier + "\nrelations:\n", 1)

for issue in validate_schema_yaml(edited, base=schema, rules=RULES):
    print(" / ".join(str(part) for part in issue.path), "-", issue.message)
```

```text
kinds / Carrier / attributes / carrier_code / description - attribute 'carrier_code' of an active element needs a description
kinds / Carrier / attributes / name / description - attribute 'name' of an active element needs a description
```

Every active Attribute needs a description. Add them and validate again:

```python
carrier = carrier.replace("carrier_code: {", "carrier_code: {description: Carrier code, ")
carrier = carrier.replace("name: {type", "name: {description: Trading name, type")
edited = text.replace("\nrelations:\n", carrier + "\nrelations:\n", 1)

assert validate_schema_yaml(edited, base=schema, rules=RULES) == []
with_carrier = load_schema(edited, base=schema)
print(with_carrier.kind_by_api_name("Carrier").id)  # a new knd_... id
assert with_carrier.kind_by_api_name("Supplier").id == schema.kind_by_api_name("Supplier").id
```

## 3. Rename an Attribute and keep its id

Rename the key and leave the `id` line in place. History, data and Lineage follow the id, so
`Supplier.legal_name` becomes `Supplier.registered_name` without losing anything. References by
name must follow: the Kind's `title_attribute` and the Source `attribute_mapping` key (the CSV
`column` stays `legal_name`).

```python
renamed = (
    edited.replace("      legal_name:\n        id:", "      registered_name:\n        id:")
    .replace("title_attribute: legal_name", "title_attribute: registered_name")
    .replace(
        "    attribute_mapping:\n      legal_name:",
        "    attribute_mapping:\n      registered_name:",
    )
)
```

Skip one of the references and the loader says where:

```python
try:
    validate_schema_yaml(
        edited.replace("      legal_name:\n        id:", "      registered_name:\n        id:"),
        base=with_carrier,
        rules=RULES,
    )
except SchemaYamlError as error:
    print(error)
```

```text
kinds/Supplier/title_attribute: unknown Attribute 'legal_name' of kind 'Supplier'
sources/approved.suppliers/attribute_mapping/legal_name: unknown Attribute 'legal_name' of kind 'Supplier'
```

With every reference updated the id is the same before and after:

```python
assert validate_schema_yaml(renamed, base=with_carrier, rules=RULES) == []
final = load_schema(renamed, base=with_carrier)
old = with_carrier.kind_by_api_name("Supplier").attribute_by_api_name("legal_name")
new = final.kind_by_api_name("Supplier").attribute_by_api_name("registered_name")
assert old is not None and new is not None
print(old.id, new.id)  # att_01KEZF7C00H73NVMMGX4KPCH9W twice
```

If you rename the key and also delete the `id`, the loader sees a different element: the old
Attribute is removed and a new one with a new id is added. Keep the `id` to rename.

## 4. Write it back

`dump_schema` renders a schema as YAML in the canonical layout, ids included, defaults left out,
and `load_schema(dump_schema(s)) == s`.

```python
assert load_schema(dump_schema(final)) == final
Path("supply-v2.yaml").write_text(dump_schema(final))
```

The same document can be sent to a running Keeper as a schema change set; see
[the quickstart](../docs/quickstart.md#10-create-a-domain-and-apply-the-supply-schema). A change
that two branches make differently is resolved by the merge engine, as in
[merge_demo.py](merge_demo.py).
