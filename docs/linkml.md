# LinkML schema import and export

[LinkML](https://linkml.io) is a modeling language for schemas written in YAML, with generators for
JSON Schema, SHACL, OWL, Pydantic and SQL. The `lorebase` command line translates a Lorebase schema
(`lorebase.schema/v1`) into a LinkML schema and back, so a Domain can reuse a model written in
LinkML, or hand its own model to LinkML tooling.

```console
$ lorebase schema export --format linkml -d supply -o supply.linkml.yaml
$ lorebase schema export --format linkml --file examples/supply/schema.yaml
$ lorebase schema import personinfo.yaml --from linkml -o personinfo.lorebase.yaml
$ lorebase schema import personinfo.yaml --from linkml -d crm -b modeling --dry-run
$ lorebase schema import personinfo.yaml --from linkml -d crm -b modeling --apply --prune
```

The translation runs in the command line, on the YAML documents, and needs PyYAML:
`pip install "lorebase-cli[linkml]"` (the workspace has it). It does not use the LinkML runtime:
the part of the LinkML metamodel it reads and writes is small, and a second dependency tree in
the command line would buy nothing the mapping needs. Validate the exported schema with LinkML's
own tools (`linkml-lint`, `gen-json-schema`) when you need its checks.

## Export

`lorebase schema export --format linkml` reads the schema of a Domain (`-d`, `-b`, `--at`), or a
local document with `--file` (`-` for standard input), and prints the LinkML schema, or writes it
with `-o`. Nothing is lost: what LinkML cannot express travels as annotations whose tag starts with
`lorebase.`, so exporting and importing again gives back the same schema, element ids included.

The `id` of the LinkML schema is `urn:lorebase:schema:{domain}`; `--uri` sets another one
(`{domain}` is replaced by the Domain id). The schema imports `linkml:types` and its default prefix
is the Domain id.

## Import

`lorebase schema import FILE --from linkml` prints the Lorebase schema document, or writes it with
`-o`; `--json` prints the document and the warnings as JSON. Every part of the LinkML schema that
the document does not keep is reported on standard error as a warning with its path in the input,
for example `classes/Person: not supported, ignored: rules`.

`--apply` sends the document to a branch the way [`lorebase schema apply`](reference/cli.md#lorebase-schema-apply)
does, as one Revision; `--dry-run` plans it and writes nothing, and shows the change set the
merge below produces. The Domain id of the document is replaced by the target Domain. The default
branch is protected: apply to another branch and merge through a Proposal.

Applying to a Domain that has a schema merges the import into it rather than replacing it. The
LinkML schema owns Kinds, Attributes, Relations, Traits, enums and path Derivations. Operations,
the other Derivations and Sources are kept, without a warning, and so is every field of an
element that the LinkML schema does not carry (`derivation`, `authority`, `indexed`, `icon`,
...). Elements match by `lorebase.id` when the
schema has it, else by name, and keep their current id, so a rename in LinkML stays a rename.
Kinds, Attributes, Relations and Traits that the LinkML schema leaves out stay too, each reported
as a warning; `--prune` removes them (and the Relations of a removed Kind, with the path
Derivations that use them). A path Derivation the LinkML schema leaves out is treated the same
way, and the steps of a kept one follow the Relations the import renames. An export fails when a
path Derivation has the name of an Attribute or Relation on its starting Kind. `--prune` without `--apply` or `--dry-run` is refused. A Domain or
branch without a schema takes the document as it is.

LinkML schemas rarely give a description to every slot, and never name a title attribute, both of
which an active Kind needs. A class that lacks them is imported as a `draft` Kind with a warning
that lists what to add; set `status: active` once it is complete.

## Mapping

| Lorebase | LinkML | Notes |
|----------|--------|-------|
| Domain `id`, `display_name`, `description` | schema `name`, `title`, `description` | the import makes a valid Domain id from `name` |
| Kind | class | |
| Trait | mixin class (`mixin: true`) | a Kind lists its Traits in `mixins`; a mapping between differently named Attributes travels as `lorebase.traits` |
| Attribute | class attribute (an inline slot) | `display_name` is `title` |
| `string`, `integer`, `float`, `boolean`, `date`, `timestamp`, `uri` | `string`, `integer`, `float`, `boolean`, `date`, `datetime`, `uri` | |
| `enum` | an enum per Attribute, named `KindAttribute` | |
| `array<...>` | `multivalued: true` | |
| `decimal(p,s)`, `text`, `duration`, `json`, `geo_point` | `decimal` or `string`, exact type in `lorebase.type` | |
| `ref(Kind)` | range of the class, exact type in `lorebase.type` | the annotation tells it from a Relation |
| `required` | `required` | |
| primary key of one Attribute | `identifier: true` | `key: true` is read as well |
| primary key of several Attributes | unique key `primary_key` | |
| `unique` | unique key `<attribute>_unique` | |
| `default` | `ifabsent` (`string(...)`, `int(...)`, `float(...)`, `True`, `False`) | other values in `lorebase.default` |
| `min`, `max`, `pattern`, `enum_values` | `minimum_value`, `maximum_value`, `pattern`, `equals_string_in` | |
| `min_length`, `max_length` | `minimum_cardinality`, `maximum_cardinality` on arrays | `lorebase.min_length`, `lorebase.max_length` on text |
| `sensitivity` | `lorebase.sensitivity` (comma separated tags) | |
| Relation | a slot on the `from` class with the `to` class as range, and one on the `to` class, linked by `inverse` | the inverse slot is marked `lorebase.relation: inverse` |
| `cardinality` | `multivalued` of the two slots | `many_to_one`: the forward slot is single valued |
| `required_from` | `required` of the forward slot | |
| element `id` | `lorebase.id` | |
| `indexed`, `searchable`, `search_weight`, `vault`, `valid_time`, `icon`, `color`, `title_attribute`, `search`, `status`, `on_delete`, `backing`, link Attributes and any other field | `lorebase.<field>` | |
| path Derivation | a slot on the class where the path starts, with the class where it ends as range | annotations `lorebase.derivation_variant: path`, `lorebase.steps` (JSON), `lorebase.inverse_api_name`, `lorebase.materialized`, `lorebase.id`; `multivalued` when a step can reach several Records |
| Operations, other Derivations, Sources | schema annotations `lorebase.operations`, `lorebase.derivations`, `lorebase.sources` | export only |

Annotation values are scalars. Structured values (settings, link Attributes, Trait maps,
Operations) are JSON text and are read back as JSON when the text is a JSON object or array.

## What the import leaves out

Each of these is reported as a warning.

- Operations, expression and aggregate Derivations and Sources, and the `derivation` and
  `authority` of Attributes that refer to them: the annotations an export writes are not
  imported. Applying to a Domain keeps the ones it has, without a warning; printing the document,
  or applying to a Domain without them, does not include them.
- Inheritance: `is_a` is flattened, so a class gets copies of its parents' slots, and abstract
  classes are not Kinds. An attribute declared on the class replaces the inherited slot of the
  same name; `slot_usage` refines it.
- `tree_root` container classes, with their slots.
- Inlined objects (`inlined`, `inlined_as_list`): a slot with a class range is a Relation to
  Records of that Kind.
- Slots whose range is an abstract or mixin class, and class ranges on mixins.
- Ranges without a Lorebase type: `time` becomes `string`, `date_or_datetime` becomes `timestamp`,
  unknown ranges and `any_of` unions become `string`. LinkML `decimal` has no precision:
  `decimal(38,9)` is assumed.
- Custom types beyond their `typeof` base (their `pattern` and other constraints), enum meanings
  and dynamic enums, unique keys of several slots other than `primary_key`, `rules`, slot and
  class URIs, mappings, subsets, schema `imports` other than `linkml:types`, and annotations that
  do not start with `lorebase.`.

Names are made to fit Lorebase: classes become `PascalCase`, slots `snake_case`, and a Relation
name already taken in the Domain gets the Kind as prefix. Without an `inverse` slot, the inverse
name of a Relation is `<kind>_<slot>`, such as `person_current_address`.
