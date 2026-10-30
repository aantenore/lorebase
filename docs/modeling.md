# Modeling guide

How to write the schema of a Domain: Kinds and their Attributes, Relations, Traits, lenses, the
words agents may use for them, stewards, and what each choice does to search, reads and merges.
The schema is a YAML document ([design 5.13](design.md#513-schema-as-code)); the Modeler of
the web app edits the same document field by field ([tour](tour.md#modeler)). It is versioned
like data: it changes on a branch and reaches a protected branch through a Proposal.

```bash
uv run lorebase schema validate parts.yaml -b work   # exit code 2 when it is not valid
uv run lorebase schema diff parts.yaml -b work
uv run lorebase schema apply parts.yaml -b work -m "Model suppliers and parts"
```

The step-by-step version against a running Keeper is in
[Manual setup](manual-setup.md#4-create-a-domain-and-apply-the-supply-schema); the full
reference schema is [examples/supply/schema.yaml](../examples/supply/schema.yaml).

## A complete example

One small Domain that uses every feature on this page. It loads and validates as written (ids
are minted on the first apply; see [Ids and renames](#ids-and-renames)).

```yaml
format: lorebase.schema/v1

domain:
  id: parts
  display_name: Parts and suppliers
  description: Suppliers, the parts they supply and the plants that stock them.
  stewards: [supply-steward]
  examples:
    - label: Single-source parts
      question: Which parts depend on a single supplier?

kinds:
  Supplier:
    description: A company that supplies parts.
    aliases: [vendor, seller]
    primary_key: [supplier_code]
    title_attribute: legal_name
    stewards: [procurement]
    search:
      embedding_template: "{legal_name}, {city} ({country}). Tier {tier}."
    attributes:
      supplier_code:
        description: Code issued by the ERP, for example S-1001.
        type: string
        required: true
        constraints:
          pattern: '^S-[0-9]{4,}$'
      legal_name:
        description: Registered legal name.
        type: string
        required: true
        searchable: true
        search_weight: A
      country:
        description: ISO 3166-1 alpha-2 country code.
        type: string
        required: true
        indexed: true
        constraints:
          pattern: '^[A-Z]{2}$'
        value_aliases: {IT: [Italy, Italia], DE: [Germany]}
        resolvable: true
      city:
        description: City of the registered office.
        type: string
      tier:
        description: Sourcing tier, 1 strategic to 3 transactional.
        type: {enum: ['1', '2', '3']}
        default: '3'
        indexed: true
        aliases: [sourcing tier]
        value_aliases: {'1': [strategic], '3': [transactional]}
      risk_score:
        description: Risk from 0 (safe) to 100 (critical).
        type: integer
        indexed: true
        constraints: {min: 0, max: 100}
      payment_terms_days:
        description: Days from invoice to payment, as agreed in the current contract.
        type: integer
        valid_time: true
      notes:
        description: Free notes of the buyers.
        type: text
        searchable: true
        search_weight: C
        constraints: {max_length: 20000}
      contact_email:
        description: E-mail address of the account manager.
        type: string
        sensitivity: [pii]
        vault: true
      vat_number:
        description: VAT registration number.
        type: string
        unique: true
        sensitivity: [confidential]
    traits:
      - trait: Located
        attribute_map: {country: country, city: city}

  Part:
    description: A component bought from suppliers.
    aliases: [component, item]
    primary_key: [part_number]
    title_attribute: name
    attributes:
      part_number:
        description: Part number, for example P-10001.
        type: string
        required: true
      name:
        description: Short name of the part.
        type: string
        required: true
        searchable: true
        search_weight: A
      category:
        description: Product category.
        type: {enum: [bearing, seal, fastener, filter]}
        indexed: true
      unit_cost:
        description: Standard unit cost in EUR.
        type: decimal(12,2)
        constraints: {min: '0'}
      supplier_count:
        description: Suppliers that can supply the part. Derived.
        type: integer
        derivation: supplier_count
      single_sourced:
        description: True when exactly one supplier can supply the part. Derived.
        type: boolean
        derivation: single_sourced

  Plant:
    description: A site that stocks parts.
    primary_key: [plant_code]
    title_attribute: name
    attributes:
      plant_code:
        description: Plant code, for example PL-01.
        type: string
        required: true
      name:
        description: Plant name.
        type: string
        required: true
      country:
        description: ISO 3166-1 alpha-2 country code.
        type: string
        required: true
      city:
        description: City.
        type: string
    traits:
      - trait: Located
        attribute_map: {country: country, city: city}

relations:
  supplied_by:
    description: The supplier can supply the part.
    inverse_api_name: supplies
    from_kind: Part
    to_kind: Supplier
    cardinality: many_to_many
    attributes:
      is_primary:
        description: True for the preferred supplier of the part.
        type: boolean
        default: false
      lead_time_days:
        description: Typical days from order to delivery.
        type: integer
        constraints: {min: 0}
  stocked_at:
    description: The plant keeps the part in stock.
    inverse_api_name: stocks
    from_kind: Part
    to_kind: Plant
    on_delete: restrict

traits:
  Located:
    description: Something with a physical location.
    attributes:
      country:
        description: ISO 3166-1 alpha-2 country code.
        type: string
        required: true
      city:
        description: City.
        type: string

derivations:
  supplier_count:
    description: Count of suppliers linked to a part.
    definition:
      variant: aggregate
      kind: Part
      attribute: supplier_count
      relation: supplied_by
      function: count
  single_sourced:
    description: True when a part has exactly one supplier.
    definition:
      variant: expression
      kind: Part
      attribute: single_sourced
      expression: supplier_count == 1

lenses:
  supplier_risk:
    description: A supplier with the parts it supplies and where they are stocked.
    kinds: [Supplier, Part, Plant]
    relations: [supplied_by, stocked_at]
    depth: 2
    include_recent_changes: true
```

Elements are keyed by api name and refer to each other by api name. Write a `description` on
every element: it is what agents read in `lorebase_schema` and context packs.

## Kinds

A Kind is a type of thing in the Domain ([design 5.3](design.md#53-kind)).

- `primary_key`: the Attributes whose values identify a Record (`Supplier/S-1001`). A Kind
  without one gets minted keys (ULIDs), and document extraction proposes no new Records of it.
- `title_attribute`: what lists, search hits and context packs show for a Record.
- `search.embedding_template`: opt the Kind into semantic search; Scribe embeds the text the
  template renders ([search](search.md)).
- `traits`: the Traits the Kind implements, with `attribute_map` from Trait Attribute to the
  Kind's own.
- `icon`, `color`: how the web app draws the Kind.
- `aliases`, `stewards`: below.

## Attributes

A typed field of a Kind, or of a Relation as a link Attribute
([design 5.4](design.md#54-attribute)).

| Type | Written as | Notes |
|------|------------|-------|
| Text | `string`, `text`, `uri` | `text` is long text: merged by sentence ([text merge](governance.md#text-merge)) |
| Choice | `{enum: [a, b]}` | a closed list of strings |
| Numbers | `integer`, `decimal(12,2)`, `float` | integers and decimals are exact (strings in canonical JSON) |
| Other scalars | `boolean`, `date`, `timestamp`, `duration`, `json`, `geo_point` | |
| Reference | `ref(Supplier)` | the key of a Record of that Kind |
| List | `array<string>`, `{array: {enum: [a, b]}}` | arrays of scalars only; compared as whole values in merges |

- **Rules.** `required`, `unique`, `default`, and `constraints`: `min` and `max` (ordered
  types), `min_length`, `max_length`, `pattern`. Writes that break them are refused with the
  path of the value; a tightened rule meets existing data at merge time as a Conflict.
- **Reads and search.** `indexed: true` keeps a per-Attribute index for filters;
  `searchable: true` puts the value in full-text search, weighted `A` (strongest) to `D` by
  `search_weight`.
- **Sensitivity.** `sensitivity: [pii | confidential | restricted]` masks the value for callers
  without `pii.read`, `confidential.read` or `restricted.read`, in reads and webhooks
  ([design 13.5](design.md#135-personal-data-and-erasure)). A sensitive Attribute is left out of
  full-text search and of the value index. `vault: true` (needs `pii`; not on link, unique,
  indexed, reference or derived Attributes) stores the value encrypted outside history, so
  `lorebase admin erase` can remove it.
- **Derived.** `derivation: NAME` names a Derivation that computes the value
  ([design 5.8](design.md#58-derivation)): `expression` (CEL over the Record's own values),
  `aggregate` (count, sum, min, max, avg over linked Records) or `path` (a virtual Relation
  composed of two or more Relations). Derived values are never written and never conflict;
  a Derivation that reads sensitive values needs the same tags.
- **Ownership.** `authority: SOURCE` names the Source that owns the Attribute. A Source with
  `auto_with_authority` wins value Conflicts on it, and the merge option `schema_authority`
  ranks it first ([merge policies](governance.md#merge-policies)).
- **Validity periods.** `valid_time: true` lets values carry a period on their provenance, so
  the reconciliation queue tells a superseded value from a contradiction
  ([Scribe](scribe.md#reconciliation)).

## Relations

A typed, directional connection between two Kinds ([design 5.5](design.md#55-relation)), keyed
by its forward name, with an `inverse_api_name` for the other direction.

- `cardinality`: `many_to_many` (default), `one_to_many` (each `to` Record has at most one
  incoming Link), `many_to_one` (each `from` Record at most one outgoing Link), `one_to_one`.
- `required_from: true`: every Record of `from_kind` needs a Link.
- `on_delete`: `cascade` (default: deleting a Record removes its Links) or `restrict` (the
  deletion is refused while Links exist).
- `attributes`: link Attributes, as on a Kind (not unique, no vault, no value index).
- `backing: foreign_key` with `foreign_key_attribute`: Links come from an Attribute of the
  `from` Kind that holds the key of the `to` Record, instead of being stored.

There is one Link per Relation and pair of Records, and no Relation crosses Domains.

## Traits

A Trait is a shared set of Attributes that several Kinds implement through `attribute_map`
([design 5.6](design.md#56-trait)). Queries, search (`traits`), context packs and Operations can
target the Trait instead of one Kind: `Located` above stands for `Supplier` and `Plant`, and a
search with `traits: [Located]` covers both ([agents](agents.md#lenses-and-traits)).

## Lenses

A lens is a named read preset: the Kinds and Traits a read is about, the Relations to follow,
how deep, which optional sections and what budget. Agents pass `lens: supplier_risk` to context
packs, search and Herald instead of repeating them. A lens never widens grants: what a caller
may not read is dropped from it ([agents](agents.md#lenses-and-traits)).

## Words for resolution

Agents ground the words of a question with `POST /resolve` (`lorebase_resolve`) before writing
a query ([agents](agents.md#resolve-words-before-querying)). Three schema members help it:

- `aliases` on a Kind or an Attribute: other words for it (`vendor` for `Supplier`, `sourcing
  tier` for `tier`), matched at the `synonym` level.
- `value_aliases` on an Attribute: other words for some stored values, keyed by the value
  (`Italy` for `IT`). On an enum the keys must be values of the enum.
- `resolvable`: `true` puts a `string` or enum Attribute in the value index whatever its number
  of distinct values; `false` keeps it out. Unset, enums are indexed and plain strings while
  their distinct values stay few ([operations](operations.md#value-index)). Sensitive, vault,
  derived and link Attributes are never indexed, and the schema refuses the flags on them.

At most 50 aliases of 128 characters per element, and aliases for at most 1,000 values per
Attribute.

## Stewards and required roles

`stewards` on the Domain and on Kinds name the RBAC roles that look after them
([design 10.7](design.md#107-stewards)). A Kind's list replaces the Domain's for that Kind.
Proposals and reconciliation candidates touching those Kinds list the principals holding the
roles as assignees, and `assigned=me` finds them. A steward role grants nothing by itself, and a
role the installation does not define is a warning (`schema.steward_role_unknown`), not an
error.

Whether stewards must approve, and which roles must, is not part of the schema: it is the
protection rule `required_approvals_from_stewards` and `required_roles`, in `keeper.proposals`
or the Domain settings ([governance](governance.md#approvals)).

## Merge behaviour per Attribute

Two branches that change the same value conflict; what decides it is configured outside the
schema, in the Domain's `merge_policy` setting, by Attribute id (the `id` written in the schema
document once applied): `prefer_attributes`, `authority_attributes`, `newest_wins_attributes`
and `text_merge_attributes`. In the schema itself, the choices that shape merges are the type
(`text` merges by sentence, other types as whole values), `authority` (the owning Source), and
the rules that a merged result must satisfy (`unique`, `required`, constraints, cardinality).
See [merge policies](governance.md#merge-policies).

## Ids and renames

Every element and Attribute has a stable `id`; the api name is a renamable property. A document
without ids gets new ones on its first apply, and `lorebase schema get` writes them back. Keep
them in the file from then on: renaming is changing the key and keeping the `id`, which lets a
rename on one branch merge with an edit on another. A renamed key without its `id` is a new
element.

## Operations and Sources

Writes go through Operations: the built-in ones per Kind and Relation (`operations_builtin`),
and custom ones with CEL preconditions, steps, permissions and a review policy
([agents](agents.md#4-write-through-operations), [design 5.7](design.md#57-operation)). Sources
map external tables and files into Kinds, with their merge policy and `local_edits`
([Scribe](scribe.md#sources)). Both live in the same document, under `operations` and
`sources`; [examples/supply/schema.yaml](../examples/supply/schema.yaml) has two of each.
