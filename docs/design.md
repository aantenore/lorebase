# Lorebase Design

Status: founding design, version 0.1
Owner: Antonio Antenore
Scope: product intent, domain model, versioning engine, storage, read path, ingestion, governance, agent interface, UI, security, operations, delivery plan.

Decisions marked **[Architect call AC-nn]** are settled for v1. They are listed again in section 20 with rationale and the condition under which they may be revisited.

---

## Table of contents

1. Intent and why
2. Goals, non-goals, anti-goals
3. Personas and the 5-day proof scenario
4. Glossary
5. Domain model
6. Versioning engine
7. Data model and Postgres schema outline
8. Read path and search
9. Ingestion: Sources, extraction, Proposals
10. Operations and governance
11. Agent interface: REST and MCP
12. User interface
13. Security and multi-tenancy
14. Observability
15. Deployment
16. Configuration
17. Repository layout
18. Roadmap M1 to M5
19. Risks
20. Architect calls (settled decisions)

---

## 1. Intent and why

### 1.1 Intent

Lorebase is the system of record for company knowledge that AI agents consume and act upon. It lets a team model what the company knows (the kinds of things that exist, their attributes, how they relate, what may be done to them), fill that model from structured and unstructured sources, keep every change versioned with branches and reviews, explore it visually, and expose it to agents through a fast, governed interface.

Agents are not built in Lorebase. They live in any framework, any language, any vendor. Lorebase is what they read from and the only place they may change company knowledge, through governed Operations.

### 1.2 Why now

Teams putting agents in front of real business processes hit the same wall:

- **Knowledge is scattered and untyped.** Retrieval over document chunks answers "what does this text say", not "which suppliers are single-sourced for parts used in plant 3". Agents need typed entities and relations, not paragraphs.
- **Knowledge changes and nobody can say what changed.** When an agent gives a wrong answer, the first question is "what did it know, and since when?". Without revisions and lineage, there is no answer.
- **Agent writes are dangerous.** Letting an agent update a database directly bypasses validation, permissions and audit. Letting it write only to a chat memory makes the knowledge useless to anyone else.
- **LLM-built knowledge graphs are unreviewable.** Pipelines that let a model write straight into a graph produce silent drift, duplicates and invented facts.
- **The proprietary platforms that solve this are closed and expensive.** Operational ontology platforms exist, but they are proprietary, coupled to a large data platform, and not deployable as a small self-hosted service.

### 1.3 Thesis

1. Company knowledge for agents should be **modeled** (typed schema), **versioned** (branches, revisions, merges), **reviewed** (Proposals, never direct LLM writes), and **governed** (Operations with validation, permissions, audit).
2. The read path must be **deterministic and fast**: no LLM call when an agent asks a question of the knowledge base. LLMs are used offline, to propose knowledge, and humans or policies accept it.
3. It must run on **one boring, proven store**: Postgres. One database to operate, back up, secure and scale.
4. It must be **open source** under a permissive license, with enterprise features (RBAC, audit, SSO) in the open core, not behind a paywall.
5. Model inference is a **pluggable, local-first utility**: the whole product runs free on a laptop with open models, and scales to any paid hosted model by configuration, never by code change.

---

## 2. Goals, non-goals, anti-goals

### 2.1 Goals

| ID | Goal | Measure |
|----|------|---------|
| G1 | Model a Domain from zero in the Modeler or as YAML | A new Kind with attributes and Relations is usable via API within one minute of saving |
| G2 | Build a Domain from structured sources (CSV, Parquet, Postgres tables) | Schema inference proposes Kinds, attributes and Relations from source metadata; a sync of 1M rows completes in under 10 minutes on reference hardware |
| G3 | Build and enrich a Domain from documents via offline LLM extraction | Extraction output is always a Proposal with evidence spans; zero direct writes to protected branches |
| G4 | Version schema and data together | Branches, immutable Revisions, diff, three-way merge at attribute level, Conflicts on both schema and data, resolution flows |
| G5 | Explore and visualize | Explorer search and record pages, Canvas graph exploration with up to 5k visible nodes at interactive frame rates |
| G6 | Expose to agents | REST API and MCP server; read p95 under 10 ms server-side for key lookups at 10M records; no LLM in the read path |
| G7 | Govern writes | All data mutations go through Operations with validation, RBAC, idempotency, dry run, audit |
| G8 | Explain every fact | Lineage per record and per attribute value: which Source, document span, Operation or person, in which Revision |
| G9 | Operate simply | Postgres 16 is the only required store; docker compose for local; stateless services; OpenTelemetry |
| G10 | Stay vendor neutral and free to run | One OpenAI-compatible inference abstraction; local by default (Ollama, fastembed, GLiNER and GLiREL); any hosted model through the same abstraction by configuration only; the full demo needs zero API keys; OIDC with any provider; no cloud-specific dependency |

### 2.2 Non-goals (not now, maybe never)

- A general data integration or ETL platform. Sources map data into Kinds; heavy transformation belongs upstream (dbt, Spark, SQL views).
- An agent runtime, orchestration framework, or prompt management tool.
- A chat or exploration assistant inside Keeper. The optional Guide service ([guide.md](guide.md)) runs a small read-only exploration agent outside Keeper, as one more client of its API with the caller's own credential; Keeper and Herald stay model-free and nothing in the read path changes when Guide is deployed or not.
- BI dashboards and report builders.
- Description logic reasoning (OWL entailment, classification). Lorebase validates constraints; it does not infer new classes.
- Real-time CDC streaming ingestion in v1 (incremental sync by watermark is supported; CDC is a later connector).
- Training, fine-tuning or serving models. Lorebase calls model servers through one abstraction; the optional Ollama compose profile is a convenience, not a serving product.
- Multi-region active-active writes.

### 2.3 Anti-goals (things Lorebase refuses to do)

- **No LLM in the agent read path.** Not for query planning, not for answer synthesis, not for reranking. Guide, the optional exploration assistant, is a caller of the read path, never a part of it: Keeper answers its reads exactly as it answers any other client's.
- **No direct LLM writes to protected branches.** Extraction produces Proposals. Always.
- **No schemaless writes.** Every Record conforms to its Kind in the Revision it belongs to.
- **No second required datastore.** No mandatory graph database, search cluster, queue broker or cache.
- **No crippled open core.** Security, audit and governance are part of the open-source product.
- **No mandatory paid API and no vendor SDK in the core.** Every model-backed feature runs on local open models; hosted models are an option selected by configuration.
- **No hidden mutations.** Every change is a Revision with an author, a reason and lineage.

---

## 3. Personas and the 5-day proof scenario

### 3.1 Personas

| Persona | Wants | Primary surfaces |
|---------|-------|------------------|
| Domain Steward (Sofia) | Correct, trusted knowledge in her business area; decides what is accepted | Review, Explorer |
| Modeler (Marco) | Clear schema that reflects the business; safe schema evolution | Modeler, Canvas (schema mode), Review |
| Data Engineer (Dana) | Map existing tables and files without writing glue code; repeatable syncs | Sources, CLI, Review |
| Agent Developer (Alex) | Fast, predictable, typed access for agents; safe write path | MCP (Herald), REST, Operations, dry run |
| Business Explorer (Luca) | Find things and understand how they connect | Explorer, Canvas |
| Platform and Security Admin (Priya) | SSO, roles, audit, isolation, observability, simple operations | Admin, audit log, metrics |

### 3.2 The 5-day proof scenario

The scenario is what a client team sees during a pilot once M4 is delivered. The fictional client is Corvane Industries, an industrial components distributor. The pilot Domain is `supply`: suppliers, parts, plants, purchase orders and supplier contracts. Success is judged on day 5 by an external agent answering supply-risk questions correctly, quickly, and with provenance, and performing one governed write.

**Day 1: install and model from zero.**
- Priya runs `docker compose up` on a VM, connects the company OIDC provider, creates the Domain `supply`, assigns roles.
- Marco opens the Modeler on a branch `model/initial` and creates Kinds `Supplier`, `Part`, `Plant`, and Relations `Part supplied_by Supplier` (many-to-many), `Part stocked_at Plant` (many-to-many with link attribute `quantity`). He defines the Trait `Located` (attributes `country`, `city`) implemented by `Supplier` and `Plant`.
- He opens a Proposal; Sofia reviews the schema diff in Review and merges it into `main`.
- The team sees: the schema graph in Canvas, an empty but typed Explorer, the first Revisions in history, and the audit log entries.

**Day 2: structured ingestion.**
- Dana registers three Sources: ERP Postgres tables `vendors` and `purchase_orders`, a CSV of approved suppliers, and a Parquet inventory snapshot.
- Schema inference proposes a new Kind `PurchaseOrder` and the Relation `PurchaseOrder issued_to Supplier` from the foreign key. Dana adjusts the mapping in the Source mapping editor with live sample preview.
- Each sync runs on its own Source branch and opens a Proposal. Clean syncs auto-merge according to policy; the first one is reviewed manually.
- The team sees: about 40k Records and 120k Links in Explorer, filtered list views, record pages with Lineage per attribute ("value from Source `erp.vendors`, row id 4411, sync run 7"), Canvas neighborhoods around a supplier.

**Day 3: unstructured extraction.**
- Sofia uploads 300 supplier contracts (PDF). Scribe runs a tiered extraction job against the `supply` schema: tier 0 rules read the contract header tables, tier 1 local models (GLiNER and GLiREL on CPU) find supplier, part and contract mentions and their relations, tier 2 asks the configured LLM for attribute values with JSON schema output. On the pilot VM the LLM is a small open model served by Ollama, at zero API cost; the client may point the same job at its hosted model endpoint by changing configuration only.
- The job produces a Proposal: 280 new `Contract` Records linked to Suppliers, 1,100 attribute updates, 40 ambiguous entity matches, and a schema suggestion to add the attribute `penalty_clause` to `Contract`.
- Review shows each proposed item with its evidence span highlighted in the document viewer, a confidence score and the matched Record. Sofia accepts in bulk above a threshold, inspects the ambiguous ones, rejects 12 items.
- The team sees: no change reaches `main` without review; rejected items are kept in the Proposal history; a re-run of the job with the same documents and model costs nothing because outputs are cached by content hash.

**Day 4: versioning and conflicts.**
- Sofia, on branch `cleanup/tiers`, reclassifies 200 suppliers' `tier` values.
- Marco, on branch `model/risk`, changes `risk_score` from `string` to `integer`, makes `Part supplied_by Supplier` require at least one Supplier, and adds a Derivation `single_sourced` (count of `supplied_by` Links equals 1).
- Meanwhile the nightly ERP sync updates `main`.
- Merging `cleanup/tiers` produces 6 attribute-level Conflicts with the sync (same supplier, same attribute, different values). Merging `model/risk` produces 9 type-coercion Conflicts (values like "high" cannot become integers) and 3 cardinality Conflicts.
- In Review they resolve conflicts individually and with bulk policies ("take Source value for attributes the ERP owns"), then merge. They compare `main` today with `main` on day 2 in Canvas diff mode.
- The team sees: schema and data merged together with consistency rules, every resolution recorded, history and time travel reads.

**Day 5: agents.**
- Alex connects an external agent (built in any framework) to Herald via MCP with an API key bound to role `agent-reader` plus the Operation grant `flag_supplier_risk`.
- The agent answers: "Which parts used in plant Turin depend on a single supplier whose contract expires within 90 days?" using `describe_domain`, `query` and `traverse` tools. Server-side latency per call: single-digit milliseconds. Every answer cites Record keys and Lineage.
- The agent calls `execute_operation` for `flag_supplier_risk` in dry-run, then for real; the Operation policy routes flags with `severity = critical` to a Proposal for Sofia, others commit directly.
- Priya shows the audit log, the trace of a full agent call in the tracing backend, and the Grafana panel for read latency.
- The team sees: the full lifecycle, end to end, with no LLM call on the agent read path.

---

## 4. Glossary

Lorebase uses its own vocabulary consistently in code, API, UI and docs. The full glossary with examples is in [glossary.md](glossary.md).

| Term | Meaning |
|------|---------|
| Domain | An ontology workspace for one company area. Isolation boundary. Has branches. |
| Kind | A type of thing. |
| Attribute | A typed field on a Kind. |
| Relation | A typed, directional connection between two Kinds, with cardinality. |
| Trait | A shared attribute set that Kinds implement. |
| Operation | A governed write with validation, permissions and audit. The only way to mutate data. |
| Derivation | A computed attribute or relation. |
| Source | External backing data (table, file, API) mapped into Kinds. |
| Record | An instance of a Kind. |
| Link | An instance of a Relation. |
| Branch | A named, movable pointer to a Revision. |
| Revision | An immutable snapshot of schema and data. |
| Proposal | A request to merge a branch, with review. Also the output of extraction jobs. |
| Conflict | A disagreement found during merge, attribute-level or structural. |
| Lineage | Where a Record and its values came from. |
| Explorer, Canvas, Modeler, Review | The four UI areas: search and navigation, graph visualization, schema editor, proposal and conflict UI. |
| Keeper, Scribe, Herald | Components: API service, ingestion and extraction worker, MCP server. |

---

## 5. Domain model

### 5.1 Identity rules

- Every schema element (Kind, Attribute, Relation, Trait, Operation, Derivation, Source) has a **stable id** minted at creation: a prefixed ULID, for example `knd_01JB8Z...`, `att_01JB8Z...`, `rel_...`, `trt_...`, `opr_...`, `drv_...`, `src_...`.
- Every schema element also has an **api name** (`snake_case` for Attributes, Relations, Operations; `PascalCase` for Kinds and Traits) unique within its scope, and a display name. Api names may be renamed; ids never change. **[Architect call AC-01]** Identity by id makes rename-on-one-side plus edit-on-the-other a clean merge.
- A **Record** is identified by `(kind_id, key)`. `key` is the value of the Kind's primary key Attribute(s), rendered as a canonical string. Kinds without a natural key mint a ULID at creation. **[Architect call AC-02]**
- A **Link** is identified by `(relation_id, from_key, to_key)`. There are no parallel Links between the same two Records for the same Relation; multiplicity is modeled with an intermediate Kind. **[Architect call AC-03]**
- External references use the form `{kind_api_name}/{key}` (for example `Supplier/S-1001`). The API resolves api names to ids per Revision.

### 5.2 Domain

| Field | Type | Notes |
|-------|------|-------|
| id | slug | `supply`; immutable |
| display_name, description | string | |
| default_branch | string | default `main` |
| settings | object | search language, materialization policy, extraction defaults, merge policies |
| created_at, created_by | | |

A Domain is the unit of isolation for data, permissions, storage scoping (object hashes are scoped per Domain) and deletion. Cross-Domain Relations are not supported in v1; cross-Domain reads are done by agents calling both Domains. **[Architect call AC-04]**

### 5.3 Kind

| Field | Notes |
|-------|-------|
| id, api_name, display_name, description | |
| primary_key | list of Attribute ids (one or more); empty means minted ULID key |
| title_attribute | Attribute used as the human label |
| attributes | map `attribute_id -> Attribute` |
| traits | list of `{trait_id, attribute_map: {trait_attribute_id: attribute_id}}` |
| status | `draft`, `active`, `deprecated` |
| search | `{text_attributes: [...weights], embedding_template: string or null, embedding_sensitive: bool or null}`; a Kind with a template is embedded (9.3) |
| icon, color | presentation hints for Explorer and Canvas |
| operations_builtin | which built-in Operations are enabled (`create`, `update`, `delete`) |

### 5.4 Attribute

| Field | Notes |
|-------|-------|
| id, api_name, display_name, description | descriptions are surfaced to agents; required to be non-empty for `active` Kinds |
| type | see type system below |
| required | boolean |
| unique | boolean; enforced per branch |
| default | literal of the type |
| constraints | `min`, `max`, `min_length`, `max_length`, `pattern` (RE2 syntax), `enum_values` |
| sensitivity | list of tags: `pii`, `confidential`, `restricted`; drives masking and vault storage |
| indexed | boolean; creates a partial expression index on the materialized head |
| searchable | boolean plus weight `A` to `D` for full-text |
| derivation_id | set when the value is computed by a Derivation (read-only) |
| authority | optional Source id that owns this Attribute's values (used by merge policies) |

**Type system.** `string`, `text` (long, full-text), `integer` (int64), `decimal` (precision, scale), `float` (float64), `boolean`, `date`, `timestamp` (UTC, microseconds), `duration`, `enum` (closed value list), `json` (opaque, not filterable beyond existence), `geo_point` (lat, lon), `uri`, and `array<T>` of any scalar type. No nested structs in v1; model them as Kinds. **[Architect call AC-05]**

**Type change rules.** Each pair `(from_type, to_type)` has a cast rule: `safe` (always succeeds, e.g. integer to decimal), `checked` (may fail per value, e.g. string to integer), or `forbidden` (e.g. geo_point to boolean). Checked casts are evaluated on every existing value when the schema change is committed, so each Revision stays valid (invariant I1 below).

### 5.5 Relation

| Field | Notes |
|-------|-------|
| id, api_name (forward), inverse_api_name | `supplied_by` and `supplies` |
| from_kind, to_kind | Kind ids; self-relations allowed |
| cardinality | `one_to_one`, `one_to_many`, `many_to_one`, `many_to_many` |
| required_from | boolean: every `from` Record must have at least one Link |
| attributes | Link attributes, same Attribute model (no unique, no primary key) |
| on_delete | what happens to Links when an endpoint Record is deleted: `cascade` (default), `restrict` |
| backing | `explicit` (Links stored) or `foreign_key` (Links derived from an Attribute of the `from` Kind referencing the `to` Kind key, used by Source mappings) |

Cardinality is enforced on every commit and checked again after merges.

### 5.6 Trait

A Trait declares a set of Attributes (name, type, required). A Kind implements a Trait by mapping each Trait Attribute to one of its own Attributes with a compatible type. Queries and search may target a Trait (`Located` returns Suppliers and Plants), and Operations may be declared against a Trait. Traits cannot hold data themselves. A Trait change that a Kind no longer satisfies is a structural Conflict at merge time and a validation error at commit time.

### 5.7 Operation

An Operation is a named, typed, governed write. It is the only way to mutate Records and Links.

| Field | Notes |
|-------|-------|
| id, api_name, display_name, description | description is what agents read |
| parameters | ordered list of `{name, type, required, description, kind_ref?}`; `kind_ref` parameters accept Record references |
| preconditions | list of `{expression, message}` in CEL; all must be true |
| steps | ordered list of edits: `create_record`, `update_record`, `delete_record`, `create_link`, `delete_link`, each with CEL expressions for values |
| review_policy | `never`, `always`, or a CEL expression (true means the edits go to a Proposal instead of committing) |
| permissions | roles or groups allowed to execute; evaluated with Domain RBAC |
| side_effects | snake_case event names; a committed execution writes one `operation.<name>` outbox event per name in its commit transaction, which webhooks subscribe to (10.5) |
| rate_limit | optional per-principal limit |
| status | `draft`, `active`, `deprecated` |

**Built-in Operations.** For every Kind, Lorebase provides `create_{kind}`, `update_{kind}`, `delete_{kind}`, and for every Relation `link_{relation}` and `unlink_{relation}`. They can be disabled or restricted per Kind. UI edits and CLI imports use them. Custom Operations express business intent (`flag_supplier_risk`, `approve_contract`) and are what agents should normally be granted.

**Expression language.** CEL (Common Expression Language) with a Lorebase function library (`record(ref)`, `links(ref, relation)`, `now()`, `count()`, string and date helpers). CEL is non-Turing-complete, side-effect free, has a specification and multiple implementations. Expressions are type-checked against the schema when the Operation is saved. **[Architect call AC-06]**

Example:

```yaml
operation: flag_supplier_risk
description: Flag a supplier as a risk with a reason and severity.
parameters:
  - { name: supplier, type: ref, kind_ref: Supplier, required: true }
  - { name: severity, type: enum, enum_values: [low, medium, high, critical], required: true }
  - { name: reason, type: text, required: true, description: Evidence-based reason }
preconditions:
  - expression: record(supplier).status != "terminated"
    message: Terminated suppliers cannot be flagged.
steps:
  - create_record:
      kind: RiskFlag
      values:
        severity: params.severity
        reason: params.reason
        raised_at: now()
        raised_by: principal.id
  - create_link: { relation: flags, from: $step[0], to: params.supplier }
review_policy: params.severity == "critical"
permissions: [risk-analyst, agent-risk]
side_effects: [risk_flag_raised]
```

### 5.8 Derivation

A Derivation computes an Attribute or a Relation. Derivations are pure and deterministic; they never call external services or models.

| Variant | Definition | Evaluation |
|---------|-----------|------------|
| `expression` | CEL over the Record's own Attributes | on write, stored in the materialized head (`derived` column) |
| `aggregate` | count, sum, min, max, avg over a Relation with optional filter | incrementally on Link and Record changes in the materializer |
| `path` | composed Relation `A -r1-> B -r2-> C` exposed as virtual Relation `A -> C` | at query time via joins; optionally materialized |

Derived values are **not** part of Record objects and do not participate in hashing or merge; they are recomputed from merged inputs. This avoids derived-value conflicts entirely. **[Architect call AC-07]** Lineage of a derived value points to the Derivation and its inputs.

Custom code Derivations (Python functions) are out of scope until after M5; when added they will run only in Scribe as batch jobs.

### 5.9 Source

| Field | Notes |
|-------|-------|
| id, api_name, type | `postgres_table`, `postgres_query`, `mysql_table`, `mysql_query`, `mssql_table`, `mssql_query`, `csv`, `json`, `jsonl`, `parquet`, `http_json` |
| connection | reference to a secret, never a literal: `secret://env/ERP_DSN` or `secret://file/erp.dsn` |
| table, query | `postgres_table`, `mysql_table`, `mssql_table`: table name, optionally schema-qualified (default: last segment of the api name); `postgres_query`, `mysql_query`, `mssql_query`: read-only `SELECT` |
| records_path, flatten_separator, http | `json`, `http_json`: dotted path of the records array; `json`, `jsonl`, `parquet`, `http_json`: nested objects as `parent<sep>child` columns (default: JSON values); `http_json`: headers (credentials as secret references only), pagination (`cursor`, `page`, `offset`, `link`), watermark query parameter, page cap |
| target_kind | Kind id |
| key_mapping | source columns to primary key |
| attribute_mapping | `{attribute_id: {column or expression, transform}}`; transforms from a fixed set: `cast`, `trim`, `lower`, `upper`, `parse_date(format)`, `concat`, `constant`, `map(values)`, `null_if` |
| relation_mapping | foreign key columns to Relation and target Kind key |
| scope_filter | SQL predicate or file filter limiting what the Source owns |
| sync_mode | `snapshot` (Source owns its scope; missing keys are deletions) or `incremental` (by watermark column; no deletions unless tombstone column) |
| schedule | cron expression, or manual |
| target_branch_policy | branch name template (default `source/{source}`) and merge policy: `auto_when_clean`, `proposal`, `auto_with_authority` |

### 5.10 Record and Link

A Record's canonical content is `{kind, key, values}` where `values` maps Attribute ids to canonical values. Null or absent values are omitted. A Link's content is `{relation, from, to, values}`.

### 5.11 Lineage

Lineage answers "where did this value come from". It is attached to changes, not to content.

- A **provenance entry** describes one origin: `{id, origin_type, origin_ref, actor, at}`. Origin types: `operation` (invocation id, Operation id, principal), `source_sync` (Source id, run id, source row locator), `extraction` (job id, document id, span offsets, model id, prompt version), `merge_resolution` (Proposal id, Conflict id, resolver), `schema_migration` (cast applied by a schema change), `import` (CLI bulk import batch).
- Each change in a Revision changeset carries a map `attribute_id -> provenance_id` for the Attributes it changed.
- The materialized head keeps, per Record, the latest provenance per Attribute. Full per-value history is available from the branch history table.
- Provenance is **not** part of content hashes. Two Sources writing the same value produce no diff; Lineage records which change set it last. **[Architect call AC-08]**

### 5.12 Invariants

- **I1. Every Revision is valid.** Schema is internally consistent; every Record conforms to its Kind; Links respect cardinality, required_from and referential integrity; unique Attributes are unique.
- **I2. Revisions are immutable** and identified by content hash.
- **I3. Branch heads move only by compare-and-swap** inside the commit transaction.
- **I4. Materialized head equals the Revision tree** of the branch head (verifiable).
- **I5. Data changes only through Operations.** Schema changes only through schema change sets on a branch. Protected branches change only through Proposal merges and Operations whose policy allows direct commit.
- **I6. Derived values are never stored in content objects.**

### 5.13 Schema as code

A Domain schema is also a YAML document, read and written by `lorebase_core.model.yaml_schema` (`load_schema`, `dump_schema`). The document is the human-facing form of the same `Schema` (goal G1). The demo Domain is [examples/supply/schema.yaml](../examples/supply/schema.yaml).

- **Format marker.** `format: lorebase.schema/v1` is optional on load and written on every dump.
- **Sections.** `domain` (required), then `kinds`, `relations`, `traits`, `operations`, `derivations`, `sources`, each optional. An unknown section or key, a duplicate key and an unknown reference are reported as problems with a path into the document (`SchemaYamlError.problems`); the loader reports everything it finds, not only the first.
- **Keys are api names.** Elements are keyed by api name (a relation by its forward name, a source by its dotted name such as `erp.vendors`) and refer to each other by api name: `primary_key`, `from_kind`, `authority`, trait `attribute_map`, Derivation and Source targets. Model fields keep their model names, except `derivation` and `trait`, which hold api names instead of the ids of `derivation_id` and `trait_id`. Defaults are omitted from dumps. Expressions are CEL source strings.
- **Ids.** Every element and Attribute has a stable `id` (5.1). On load, an explicit `id` wins; an element without one reuses the id of the element with the same api name in the optional `base` schema; otherwise a new id is minted. Dumps always write ids.
- **Rename rule.** Renaming is changing the key and keeping the `id`. A renamed key without an `id` is a new element (new id) unless the `base` schema has that api name, so keep the `id` when renaming. This is what lets a rename on one branch merge cleanly with an edit on another (AC-01).
- **Type syntax.** Scalars by name (`string`, `integer`, `date`); parameters in parentheses (`decimal(12,2)`, `ref(Supplier)`); `array<string>`; and a one-key mapping for enums and for arrays of complex types: `{enum: [low, high]}`, `{array: {enum: [a, b]}}`.

Excerpt of `examples/supply/schema.yaml`:

```yaml
kinds:
  Supplier:
    id: knd_01KEZF7C007ZDX8SK1DZP7MXFF
    description: A company that supplies parts to Corvane Industries.
    primary_key: [supplier_code]
    title_attribute: legal_name
    icon: factory
    color: "#2a6f97"
    attributes:
      supplier_code:
        id: att_01KEZF7C00HDZZHMNS4181SKMX
        description: Supplier code as issued by the ERP, for example S-1001.
        type: string
        required: true
        constraints:
          pattern: '^S-[0-9]{4,}$'
      legal_name:
        id: att_01KEZF7C00H73NVMMGX4KPCH9W
        description: Registered legal name including the legal form.
        type: string
        required: true
        searchable: true
        search_weight: A
        authority: approved.suppliers
      # ...
```

---

## 6. Versioning engine

The versioning engine is the core of Lorebase. It is built in-house on Postgres and is content-addressed.

### 6.1 Objects and canonical form

Every schema element, Record and Link is serialized to **canonical JSON** following RFC 8785 (JSON Canonicalization Scheme) with these Lorebase rules: **[Architect call AC-09]**

- Keys are Attribute and element ids, never api names, so renames do not change hashes.
- `integer` and `decimal` values are encoded as JSON strings in canonical form (`"42"`, `"12.50"` normalized to declared scale) to avoid float precision loss. `float` values are encoded with the RFC 8785 number rules.
- `timestamp` is RFC 3339 UTC with exactly six fractional digits. `date` is `YYYY-MM-DD`.
- Arrays keep order (arrays are ordered values). Sets do not exist as a type.
- Strings are NFC-normalized.
- Null and absent are the same: omitted.
- Values of Attributes tagged `pii` with vault storage enabled are replaced by a vault reference `{"$vault": "<vault_id>"}` (see 13.5). Hashes, diffs and merges compare references: the same value of the same Attribute of the same Record has the same reference, so equal values are equal objects; the same value in two Records has two references.

The object hash is

```
hash = sha256( "lb1\x00" || object_type || "\x00" || canonical_json_bytes )
```

`lb1` versions the hashing scheme so it can evolve. `object_type` is one of `schema.kind`, `schema.relation`, `schema.trait`, `schema.operation`, `schema.derivation`, `schema.source`, `schema.domain`, `record`, `link`. Hashes are 32 raw bytes in storage and lowercase hex in APIs.

Objects are stored once per Domain: `(domain_id, hash)` is the primary key. Scoping per Domain prevents cross-Domain existence probing and allows Domain deletion without reference counting across Domains. **[Architect call AC-10]**

### 6.2 Trees: the Revision manifest

A Revision must map every element key to an object hash. With millions of Records per Domain, storing a flat manifest per Revision is not viable. Lorebase uses a **partitioned, canonical hash array mapped trie** (HAMT) with structural sharing. **[Architect call AC-11]**

**Partitions.** The root of a Revision tree is a small sorted map from partition id to partition root hash:

- `schema`: all schema elements, keyed by element id.
- `k:{kind_id}`: one partition per Kind, keyed by Record key.
- `r:{relation_id}`: one partition per Relation, keyed by `from_key \x1f to_key`.

Partitioning by Kind and Relation gives cheap per-Kind diffs, per-Kind counts and the ability to skip whole partitions during merges.

**Trie shape.** Inside a partition, entries are placed by `h = sha256(element_key)`. A node at depth `d` is either:

- a **leaf** holding up to `L` entries `(element_key, object_hash)` sorted by `h`, or
- a **branch** with up to 256 children, selected by byte `h[d]`, each child being a node hash.

A node is a leaf if and only if its subtree holds `L` or fewer entries. This rule depends only on content, so **the same set of entries always produces the same tree and the same root hash**, regardless of insertion order or history. Equal partitions have equal hashes. This canonicity is what makes diff and merge cheap and correct. `L` defaults to 512 and is fixed per Domain at creation (it is part of the hashing contract).

**Node encoding.** Compact binary: a header (format version, node type, depth, entry count, subtree count) followed by length-prefixed keys and 32-byte hashes (leaves) or a 256-bit occupancy bitmap and child hashes (branches). Node hash = `sha256("lb1\x00node\x00" || bytes)`. Node bodies are stored in a `bytea` column with Postgres `lz4` TOAST compression.

**Cost.** With `L = 512` and 256-way fanout, 10M entries in a partition fit in depth 3. An update to one Record rewrites one leaf and at most three branch nodes. A Revision touching `n` Records writes about `n` leaves in the worst case and far fewer when changes cluster in the same leaves. Unchanged subtrees are shared by hash across Revisions and branches.

**Subtree counts** stored in each node give per-partition and per-Kind Record counts at any Revision in O(1).

### 6.3 Changesets: the delta encoding

In addition to its tree, every Revision stores a **changeset** against its first parent: rows of `(partition, element_key, old_hash, new_hash, provenance)`. The tree gives snapshot semantics and arbitrary-pair diff; the changeset gives O(changes) access to "what changed in this Revision", drives incremental materialization, history and Lineage, and is what the Review UI renders for linear branches.

Changesets of merge Revisions are computed against the first parent (the target branch), so walking the first-parent chain of a branch yields every change visible on that branch, including merged ones.

### 6.4 Revisions

A Revision object:

```json
{
  "parents": ["<rev hash>", "..."],
  "root": "<root node hash>",
  "domain": "supply",
  "author": {"principal": "usr_...", "on_behalf_of": null},
  "message": "Sync erp.vendors run 7",
  "origin": {"type": "source_sync", "ref": "run_01J..."},
  "created_at": "2026-10-20T09:12:44.123456Z",
  "stats": {"partitions_changed": 2, "changes": 1840}
}
```

Revision id = `sha256("lb1\x00revision\x00" || canonical_json)`. Revisions carry a **generation number** (`1 + max(parent generations)`) for fast ancestor queries.

### 6.5 Branches and refs

A ref is a row `(domain_id, name, head, kind, protected, version)`. Kinds: `branch` (movable) and `tag` (immutable pointer, used for releases and audits). Branch names are hierarchical (`model/risk`, `source/erp.vendors`, `extract/job-42`, `agent/{principal}/{task}`).

- Creating a branch is O(1): insert a ref pointing at an existing Revision. Materialization cost is discussed in 6.10.
- Branch head updates are compare-and-swap on `(head, version)` within the commit transaction.
- Writers to the same branch are serialized with a transaction-scoped advisory lock on `hash(domain_id, branch)`. **[Architect call AC-12]** Single writer per branch keeps commits simple and correct; throughput comes from batching (one Revision can hold thousands of changes) and from using multiple branches.
- Protected branches (default: `main`) accept only Proposal merges and Operations whose review policy evaluates to direct commit.
- Branch deletion removes the ref; objects become eligible for garbage collection if unreachable.

### 6.6 The commit pipeline

Every write (Operation, schema change set, sync batch, merge) goes through one pipeline in one Postgres transaction:

1. Acquire the branch advisory lock; read head `H` and ref version.
2. Validate the requested changes against the materialized head of `H` (fast indexed reads): types, constraints, uniqueness, cardinality, referential integrity, Trait conformance. For schema changes, compute required data migrations (casts, defaults) and include them as data changes.
3. Canonicalize and hash new objects; insert into `objects` with `ON CONFLICT DO NOTHING`.
4. Apply changes to the tree by path copying; insert new nodes.
5. Insert the Revision with parent `H`, its changeset and provenance entries.
6. Apply the changeset to the materialized head (records, links, history, search vectors, derived values).
7. Compare-and-swap the ref from `H` to the new Revision.
8. Write the audit entry and outbox events.
9. Commit.

Reads after the commit see the new head immediately (read-your-writes on the primary). If any step fails, nothing is visible.

### 6.7 Diff

`diff(A, B)` walks both trees from the roots: partitions with equal hashes are skipped; within a partition, branch nodes with equal hashes are skipped; differing leaves are compared entry by entry. Cost is proportional to the number of differing leaves, not to the size of the Domain. Output is a stream of `(partition, key, hash_a, hash_b)` that is then expanded into element-level changes (attribute-level for Records and schema elements) for display.

For linear history (`A` is an ancestor of `B` on the first-parent chain), the engine may union changesets instead of walking trees when that is cheaper (small number of Revisions).

### 6.8 Merge algorithm

Merging source branch `S` (theirs) into target branch `T` (ours):

**Step 0. Merge base.** Find the lowest common ancestors of `head(T)` and `head(S)` using generation numbers to prune the search. If `head(S)` is an ancestor of `head(T)`, the merge is a no-op. If `head(T)` is an ancestor of `head(S)` and the target allows it, fast-forward. If there are several lowest common ancestors (criss-cross history), merge them recursively into a virtual base Revision, as git does. **[Architect call AC-13]**

**Step 1. Three-way diff.** Compute `D_ours = diff(base, T)` and `D_theirs = diff(base, S)` per partition, streamed and batched.

**Step 2. Schema merge.** Merge the `schema` partition first. For each schema element changed on either side:
- changed on one side only: take that side;
- changed identically on both: take it;
- changed differently on both: run a structural three-way merge of the element's JSON document. Maps keyed by id (for example a Kind's `attributes`) merge entry by entry; scalars conflict if both sides changed them differently; ordered lists (Operation steps, parameters) conflict if both sides changed them. Each scalar conflict becomes a schema property Conflict.
- deleted on one side and modified on the other: schema delete/modify Conflict.
- api name collisions between different ids in the same scope: naming Conflict.
The result is the merged schema `Σm` plus a list of schema Conflicts.

**Step 3. Data merge.** For each Record and Link key changed on either side:
- one side only: take it;
- same new hash on both: take it;
- both modified: attribute-level three-way merge on `values`. For each Attribute: one side changed it, take it; both changed it to the same value, take it; otherwise an attribute value Conflict. Array values are compared as whole values.
- added on both sides with different content: three-way merge against an empty base; differing Attributes conflict.
- deleted on one side and modified on the other: delete/modify Conflict.
Every taken change keeps its original provenance.

**Step 4. Consistency under the merged schema.** The merged data must satisfy invariant I1 against `Σm`. The engine runs rule checks scoped to what changed, using indexed reads on the materialized target head plus the in-flight merge set:

| Rule | Detected when | Conflict |
|------|---------------|----------|
| Orphaned data | A Kind or Relation removed on one side; Records or Links of it added or modified on the other | Orphaned data Conflict (grouped per Kind, with count). Unmodified Records of a removed Kind are dropped silently (removal intended). |
| Removed Attribute with new values | Attribute removed on one side; the other side set values for it | Orphaned value Conflict (grouped) |
| Type change | Attribute type changed on one side; the other side wrote values | Values are cast with the cast rule; failures become type coercion Conflicts (per Record, grouped) |
| New required Attribute | Attribute became required or was added as required on one side; Records created or modified on the other lack a value | Default applied if defined, otherwise missing value Conflict |
| Constraint tightened | New `max_length`, `pattern`, `enum_values`, range on one side; violating values from the other | Constraint Conflict |
| Cardinality | Relation cardinality tightened on one side and extra Links added on the other; or both sides added a Link that is fine alone but together violate `one_to_*` or `*_to_one` | Cardinality Conflict listing the competing Links |
| Required relation | `required_from` set on one side; the other side created Records without the Link, or removed the last Link | Required relation Conflict |
| Uniqueness | Both sides set the same unique value on different Records; or uniqueness added on one side and duplicates created on the other | Uniqueness Conflict |
| Dangling Link | Link added on one side to a Record deleted on the other | If `on_delete = cascade`, the Link is dropped and recorded as an automatic resolution; if `restrict`, Dangling Link Conflict |
| Trait conformance | Trait changed on one side so that a Kind changed on the other no longer conforms | Trait conformance Conflict |
| Broken reference | Operation, Derivation, Source or Trait mapping references an element removed or retyped on the other side | Broken reference Conflict |

**Step 5. Outcome.** If there are no Conflicts, the engine writes a merge Revision with parents `[head(T), head(S)]` via the commit pipeline. Otherwise it stores a **merge attempt** with base, ours and theirs Revision ids and the Conflict list, attached to the Proposal.

### 6.9 Conflict taxonomy

| Code | Class | Scope | Resolution choices |
|------|-------|-------|--------------------|
| `DV1` | Attribute value | Record or Link Attribute | ours, theirs, base, custom value |
| `DV2` | Delete/modify | Record or Link | keep deleted, keep modified (ours or theirs version) |
| `DV3` | Add/add | Record or Link key | attribute-level choices as DV1 |
| `SC1` | Schema property | Schema element property | ours, theirs, base, custom |
| `SC2` | Schema delete/modify | Schema element | keep deleted, keep modified |
| `SC3` | Naming collision | Api name in scope | rename one side (custom value) |
| `XS1` | Orphaned data | Kind or Relation removed vs data changes | drop data, restore schema element |
| `XS2` | Orphaned value | Attribute removed vs values written | drop values, restore Attribute |
| `XS3` | Type coercion | Value not castable | custom value, null (if not required), drop Record, revert type change |
| `XS4` | Missing value | Required Attribute without value | custom value, set default for all, relax required |
| `XS5` | Constraint | Value violates tightened constraint | custom value, relax constraint |
| `XS6` | Cardinality | Competing Links | choose Links to keep, relax cardinality |
| `XS7` | Required relation | Record without required Link | add Link (custom), relax requirement, drop Record |
| `XS8` | Uniqueness | Duplicate unique values | custom value on one Record, relax uniqueness |
| `XS9` | Dangling Link | Link to deleted Record | drop Link, restore Record |
| `XS10` | Trait conformance | Kind no longer implements Trait | adjust mapping, remove Trait from Kind |
| `XS11` | Broken reference | Schema reference to removed element | edit referencing element, restore referenced element |

Grouped Conflicts (XS classes over many Records) are presented as one Conflict with a member list, and accept bulk resolutions.

### 6.10 Resolution flows

- **Conflict fingerprint.** Each Conflict has a fingerprint `sha256(code, partition, key, attribute, base_hash, ours_hash, theirs_hash)`. Resolutions are stored by fingerprint.
- **Interactive.** Review shows base, ours and theirs side by side; the resolver picks a choice or enters a custom value (validated against the merged schema). Grouped Conflicts offer bulk choices.
- **Policies.** A Proposal or a Source can carry merge policies applied before humans see anything:
  - `prefer: ours | theirs` per Kind or per Attribute;
  - `authority`: a ranked list of origins (Sources, principals, actors, origin types); the side whose change comes from the higher ranked origin wins per Attribute, and the Source that owns an Attribute can rank first (`schema_authority`; Source syncs with `auto_with_authority` prefer the Source's side on those Attributes);
  - `newest_wins` by provenance timestamp, globally or for specified Attributes, ties decided by provenance entry id;
  - `cascade_dangling: true`.
  Policy resolutions are recorded as resolutions with resolver `policy:{name}`.
- **Stale attempts.** If the target head moves while Conflicts are being resolved, the merge attempt is recomputed against the new head. Resolutions whose fingerprints still match are re-applied automatically; new or changed Conflicts are shown as unresolved. This makes long reviews safe.
- **Completion.** When all Conflicts are resolved, the engine applies the resolutions, re-runs consistency checks (resolutions can create new violations, for example a custom value that breaks uniqueness), and commits the merge Revision. Resolutions are stored as provenance `merge_resolution` on the affected values.
- **Abandon.** Closing a Proposal discards the merge attempt; nothing changes on the target.
- **No rebase in v1.** History is append-only; branches are updated by merging the target into them. **[Architect call AC-14]**

### 6.11 Materialized heads

Agents need millisecond reads, which a tree walk cannot provide for filters, traversals and search. Lorebase keeps a **materialized head** per materialized branch: relational rows that mirror the tree at the branch head, maintained incrementally by the commit pipeline in the same transaction (invariant I4).

- Records: one row per Record with values as `jsonb` keyed by **Attribute id**, so rows stay stable across renames; the query compiler maps api names to ids. **[Architect call AC-15]**
- Links: one row per Link, indexed in both directions.
- Search vectors, derived values, Lineage pointers and history are maintained in the same transaction.

**Branch materialization modes.** **[Architect call AC-16]**
- `full`: the branch has its own rows. Creating a full branch copies rows from its parent's materialization with `INSERT ... SELECT` (about 1M rows in seconds). Used for `main`, protected branches, Source branches and long-lived work.
- `overlay` (M5): the branch stores only the difference between its head and the head of its base materialized branch, computed by tree diff and kept current when either side moves. Reads union the base rows with the overlay. Used for short-lived agent and extraction branches.
- M1 to M4 implement `full` only, with a configurable cap on materialized branches per Domain; branches over the cap are readable through the tree-based slow path (6.12).
- Implemented (M5): an overlay records its base branch and fork point; reads resolve "overlay rows, then base rows as of the fork point that the overlay does not shadow" in one statement. Rather than recomputing the difference by tree diff when the base moves, a base commit copies the previous state of every row it changes into the overlays that still read it (copy on write), so the base rows an overlay reads never change after its fork. Overlays are promoted to full copies above `store.materialize.overlay_max_rows` rows or `overlay_max_base_lag_revisions` generations of base lag, before a schema change, and before their base is deleted or rebuilt; a merged overlay is re-forked cheaply. Details in the store schema notes, numbers in `docs/performance.md`.

**Verification.** `lorebase admin verify --branch main` checks that every materialized row's `object_hash` matches the tree at the head and that counts per partition match. Run in CI against fixtures and on a schedule in production.

### 6.12 Reads at an arbitrary Revision (time travel)

`GET ...?at={revision}` reads directly from the tree: key lookups are O(depth) node fetches (about 3 to 4 indexed reads, cached), list operations scan the Kind partition (every implementing Kind for a Trait). No filters on non-key Attributes and no search at arbitrary Revisions in v1; the response states that it was served from the tree. Tags can be materialized on demand for audits.

### 6.13 Garbage collection and retention

- Objects and nodes are immutable and only become garbage when no ref, open Proposal or retained tag reaches them.
- GC is a mark-and-sweep job per Domain run by Scribe: mark from all refs, tags, open merge attempts and Revisions younger than the retention window; sweep unmarked objects and nodes in batches, then the vectors no branch reads (9.3).
- History is never rewritten. Erasure of personal data uses the vault (13.5) so that history keeps its shape while the values disappear.

### 6.14 Performance notes and targets

Reference hardware: 8 vCPU, 32 GB RAM, NVMe, Postgres 16 tuned per the deployment guide. Targets are p95 server-side.

| Path | Target |
|------|--------|
| Record get by key, 10M Records in Domain | under 5 ms |
| Filtered list on indexed Attributes, 50 rows | under 25 ms |
| One-hop traversal, 100 neighbors | under 10 ms |
| Full-text search, 20 results | under 50 ms |
| Hybrid search with caller-supplied vector, 20 results | under 80 ms |
| Neighborhood depth 2, cap 200 nodes | under 50 ms |
| Operation commit, under 10 edits | under 75 ms |
| Bulk commit, 10k Record changes in one Revision | under 5 s |
| Diff of two Revisions with 10k changed Records | under 2 s |
| Merge with 100k changed Records, no Conflicts | under 60 s |

Engineering notes:
- Tree nodes are cached in-process (LRU by hash; immutable, so no invalidation).
- Schema per Revision is cached by schema partition hash; compiled query plans and CEL programs are cached by the same key.
- Bulk paths use `COPY` into staging tables and set-based SQL.
- Long merges run as Scribe jobs and stream through partitions; memory is bounded by batch size.
- Hot `objects` table is hash-partitioned by `domain_id`.

### 6.15 Implementation notes

Decisions taken in `lorebase_core` (`canonical`, `tree`, `revision`, `merge`, `expr`) while implementing 6.1 to 6.10 and 5.7 to 5.8. They refine the text above; where a detail differs, this section describes the code. The engine is pure: it reads and writes through the `NodeStore`, `ObjectStore` and `RevisionGraph` protocols, and persisting merge attempts or committing a merged tree belongs to the store (`lorebase_store.merging`, which commits like the commit pipeline of 6.6).

**Canonical form**
- `null` inside an array is kept, because positions are significant. `null` as an object member is omitted (null and absent are the same).
- The canonical serializer accepts an integer only when its decimal text equals the RFC 8785 text of its double. Every other integer must be passed as a string, which is what 6.1 already requires for `integer` and `decimal` values; there is no silent rounding.

**Trees**
- The root node stores, for each partition, the partition id, the partition root hash and the entry count. Per-partition and per-Kind counts are therefore read from the root without descending.
- Node header: format version (`u8`, currently 1), node type (`u8`: leaf 1, branch 2, root 3), depth (`u8`), entry count and subtree count (unsigned LEB128 varints). Leaf entries are sorted by `sha256(key)`, root partitions by the UTF-8 bytes of their id. The decoder accepts only the canonical encoding, so a node has exactly one byte representation and one hash.

**Revisions and ancestry**
- The generation number is not part of the Revision content and does not enter the Revision id. It is derived from the parents (`1 + max(parent generations)`) and kept next to the Revision.
- Merge base: if there are several lowest common ancestors and no virtual base builder is supplied, `merge_base` raises `MergeBaseError`; it never picks one silently. Histories with no common ancestor merge against an empty base.

**Merge**
- Virtual base (AC-13): `merge` builds it with the same pipeline, run without an index view. In that run consistency checks and Conflict reporting are skipped, policies are not applied, and a change that conflicts keeps the value of its own merge base, so both sides of the outer merge see it as a change. The virtual Revision is never committed; its id is `sha256("lb1\x00virtual-base\x00" || base || ours || theirs || root)` and its root lives in memory for the duration of the merge.
- Merge resolutions: `apply_resolutions` needs a resolution for every Conflict of the attempt and checks each choice against the kinds table below. It then re-runs the whole merge with the resolutions as directives (schema overrides, ignored side changes, data overrides) and repeats until no new directives appear; rounds are bounded by the number of resolutions. A resolution can therefore surface new Conflicts, for example a custom value that breaks uniqueness. Resolutions passed in `MergeInput` are matched to Conflicts by fingerprint, which is how a stale attempt is recomputed against a moved target (6.10).
- Fingerprints: `sha256` over the canonical JSON array `[code, partition, key, attribute, base_hash, ours_hash, theirs_hash]` (hex, `null` when absent), followed for grouped Conflicts by the sorted member keys. A group that gains or loses a member is a different Conflict and does not inherit an earlier resolution.
- Open Conflicts: while a Conflict is open, its location takes the base value as placeholder in the working merge and is excluded from consistency checks. An open schema Conflict on a Kind or Relation suppresses cross-cutting Conflicts in that element's partition, and an open schema delete/modify Conflict (`SC2`) also suppresses data Conflicts there. This keeps one root cause from being reported many times.
- Automatic resolutions are returned with the outcome (`AutoResolution`, for Lineage and review) with resolver `policy:prefer`, `policy:cascade_dangling` or `policy:default`.

**Conflict codes.** `ConflictKind` in `merge/conflicts.py`; the code is stable and is part of the fingerprint. "Row" refers to 6.8 step 4 or 6.9.

| Code | `ConflictKind` | Design row | Choices accepted | Grouped |
|------|----------------|-----------|------------------|---------|
| `DV1` | `ATTRIBUTE_VALUE` | Attribute value | ours, theirs, base, custom | no |
| `DV2` | `DELETE_MODIFY` | Delete/modify (data) | ours, theirs, base, custom | no |
| `DV3` | `ADD_ADD` | Add/add | ours, theirs, base, custom | no |
| `SC1` | `SCHEMA_PROPERTY` | Schema property | ours, theirs, base, custom | no |
| `SC2` | `SCHEMA_DELETE_MODIFY` | Delete/modify (schema) | ours, theirs, base, custom | no |
| `SC3` | `NAMING` | Naming collision | custom | no |
| `XS1` | `ORPHANED_DATA` | Orphaned data | drop, restore | yes |
| `XS2` | `ORPHANED_VALUE` | Removed Attribute with new values | drop, restore | yes |
| `XS3` | `TYPE_COERCION` | Type change | custom, drop, restore | yes |
| `XS4` | `MISSING_VALUE` | New required Attribute | custom, relax | yes |
| `XS5` | `CONSTRAINT` | Constraint tightened | custom, relax | yes |
| `XS6` | `CARDINALITY` | Cardinality | custom, relax | yes |
| `XS7` | `REQUIRED_RELATION` | Required relation | custom, drop, relax | yes |
| `XS8` | `UNIQUENESS` | Uniqueness | custom, relax | yes |
| `XS9` | `DANGLING_LINK` | Dangling Link | drop, restore | yes |
| `XS10` | `TRAIT_CONFORMANCE` | Trait conformance | custom, relax | no |
| `XS11` | `BROKEN_REFERENCE` | Broken reference | custom, restore | no |

Choice meanings: `ours`, `theirs`, `base` take that side's value, element or state (for `DV2` and `SC2`, `ours` or `theirs` decides whether the deleted or the modified version wins); `custom` takes the resolver's value, per member for grouped Conflicts; `drop` removes the data that breaks the merged schema; `restore` brings back what the other side removed or retyped; `relax` weakens the schema rule the data breaks.

**`MergePolicy` defaults.** The policy is a frozen options object of one merge.

| Option | Default | Effect |
|--------|---------|--------|
| `fast_forward` | `true` | Move the target to theirs when ours is an ancestor |
| `allow_checked_casts` | `true` | Cast values written under a changed type with `checked` rules; failures are `XS3`. When `false`, every such value is `XS3` |
| `cascade_dangling` | `true` | Drop a Link to a deleted Record when its Relation has `on_delete = cascade` |
| `apply_defaults` | `true` | A newly required Attribute takes its default when it has one |
| `prefer`, `prefer_elements`, `prefer_attributes` | none | Resolve `DV1` to `DV3`, `SC1`, `SC2` for `ours` or `theirs`, globally, per Kind or Relation id, or per Attribute id |
| `authority`, `authority_attributes` | none | Ranked origin labels, highest first, globally or per Attribute id: on `DV1` and `DV3` the side whose last change of the Attribute comes from the higher ranked origin wins |
| `schema_authority` | `false` | An Attribute's schema `authority` (a Source) ranks first for that Attribute |
| `newest_wins`, `newest_wins_attributes` | off | On `DV1` and `DV3`, globally or for the listed Attribute ids, the side whose last change of the Attribute is the more recent wins |
| `sample_size` | `10` | Member keys listed in a grouped Conflict |
| `atomic_properties` | `type`, `default`, `definition` | Schema properties merged as whole values instead of entry by entry |

**Policy rules.** `MergePolicy.rules` gives the rules tried on one Conflict, from the most specific to the least: per Attribute `prefer`, `authority`, `newest_wins`; per Kind or Relation `prefer`; global `authority`, `newest_wins`, `prefer`. The first rule that decides resolves the Conflict, recorded as an `AutoResolution` with resolver `policy:{name}`; when none decides, the Conflict is reported. A resolution given by a person always comes first, and the virtual base run applies no policy.

- Lineage input: `MergeInput.lineage` is a `LineageLoader`, `(revision, partition, key) -> {attribute id: ChangeStamp}`, the provenance stamp of the last change of each Attribute on `ours` and `theirs`. A `ChangeStamp` holds the provenance entry id, its time and the origin labels a ranking matches: `source:{source id}` (Source syncs), `principal:{principal}` and `operation:{api name}` (Operations), `actor:{actor}` and `origin:{origin type}` (every entry). The core reads nothing else; without a loader `authority` and `newest_wins` never decide. In the store, `MergeLineage` reads the Lineage of both materialized branch heads and the provenance rows, once per merge.
- `authority`: a side whose stamp matches no label ranks below every listed origin. No decision when both sides rank the same, neither is listed, or a side has no stamp (unknown Lineage).
- `newest_wins`: compares the stamp times as instants. Equal times are decided by the greater provenance entry id (a ULID, so normally the later-created entry), which does not depend on which branch is ours; equal ids, or a side without a stamp, give no decision.
- Scope: both apply only to Attribute Conflicts (`DV1`, `DV3`); delete/modify, schema and cross-cutting Conflicts are left to `prefer`, `cascade_dangling` and people.
- Audit: the `AutoResolution` carries `details` (the matched origins, or the compared times and `tie_break`, and both provenance entry ids); it is stored with the merge attempt, and a value taken from theirs gets a `merge_resolution` provenance entry naming `policy:{name}` as resolver. A value kept from ours keeps its own Lineage, so on the next merge `newest_wins` compares the time of the merge resolution when theirs won and of the original change when ours won.

**Expressions** (`expr/`, AC-06)
- cel-python supplies the CEL grammar and parser; type checking and evaluation are Lorebase's own, over a small AST, because cel-python has no static checker, no decimal and no evaluation budget. Regular expressions run on RE2.
- Types: CEL types plus `decimal` (exact; `int` operands promote, `double` does not mix), `date`, `ref(Kind)` and `record(Kind)`. Enum Attributes are closed string types: comparing one with a string outside its values, or assigning such a literal, is a type error.
- Environments: Operation expressions see `params.<name>`, each parameter by name, `principal.id`, `principal.subjects`, `record(ref)`, `links(ref, "relation")` and, in steps, `$step[n]` (rewritten to `_step[n]` before parsing). Derivations and aggregate filters see their Record's Attributes, `key` and `links.<relation>` (one hop).
- Evaluation is deterministic (the caller passes `now`) and bounded by `ExprLimits`: `keeper.operations.expression_limits` for Operations, `schema.expression_limits` for schema validation and Derivations. An absent value is `null`; ordering or arithmetic on it is an error that points to `has()`. `&&` and `||` absorb an error when the other side decides.
- Records come from a caller's `RecordResolver`; one that has not loaded a Record raises `RecordNotLoaded` and the caller loads it and evaluates again. Steps see the Records written by earlier steps.
- Switches: `schema.expressions_enabled` (default on) decides whether schemas may hold CEL; it feeds `SchemaRules.allow_expressions` everywhere a schema is validated (schema change sets, the commit pipeline, merges, schema inference). `keeper.operations.expressions_enabled` (default on) decides whether Keeper evaluates stored Operations; the engine has no I/O and every evaluation is bounded, so both default to on.
- Operations in Keeper: `record()` reads the materialized head of the branch, `links()` reads Links by endpoint (`lorebase_store.reads.links_of_record`, stored and foreign-key backed, at most `store.reads.max_page_rows` per call) and the Records at their far end. A precondition that is false is a 422 `precondition-failed` problem whose `issues` carry the message, the expression and its line and column; an evaluation error is located the same way. A review policy that evaluates to true is a 409 `review-required` problem pointing to Proposals; dry runs report both without failing.
- Not supported: unsigned integers, message construction, three-argument `map`, time zones in timestamp accessors.

**Derived values** (`lorebase_store.materialize.derived`, AC-07)
- `expression` and `aggregate` Derivations are recomputed in the `derived` column of the materialized row (keyed by Attribute id), in the commit transaction of every commit and merge (6.6 step 6) and on a full rebuild. They never enter the tree, so they take no part in hashing, diff or merge.
- Order: a Derivation that reads a derived Attribute runs after the one computing it. `derivation_inputs` lists what a Derivation may read (its Record's derived Attributes named in the expression, and derived Attributes of Records one hop away named after a `links.<relation>` field, or the aggregate's source and filter); schema validation reports a cycle among them as `schema.derivation_cycle`.
- Incremental scope of one Derivation: Records written by the changeset, Records whose Links of a Relation it reads changed, and Records linked to written Records or to Records whose derived values changed earlier in the same commit (one hop). Above `store.materialize.derivation_max_incremental_records` Records it is recomputed for its whole Kind. A schema change recomputes every Derivation and drops values of Attributes that are no longer derived.
- Evaluation: bounded by `schema.expression_limits`; `now()` is the time of the Revision that recomputes the value. A value that cannot be computed is absent. Aggregates: `count`, `sum` (0 without members), `min`, `max`, `avg` (no value without members); decimal results are rounded to the target scale.
- Reads: the read API returns derived values with the Record's values and the query DSL filters on them; a derived Attribute marked `searchable` enters the search vector. `record()` and `links()` in Operations see derived values; Operation steps never write them.
- Verification: `verify(..., derived=True)` recomputes every derived value without writing and reports differences as `derived_mismatch`.
- Switch: `store.materialize.derivations_enabled` (default on), which also covers materialized path Derivations.

**Path Derivations** (`lorebase_core.model.paths`, `lorebase_store.materialize.paths`)
- A `path` Derivation is a virtual Relation from the first step's near Kind to the last step's far Kind, named by its `api_name` and, when given, `inverse_api_name` (unique among Relation names). It has no Attributes; its Links carry no values. A path whose steps name unknown Relations or do not continue is reported by schema validation and ignored by readers.
- Semantics, the same everywhere: the distinct `(a, c)` pairs reached through one Link per step. The first step follows every Link of `a`; every later step follows at most `store.traverse.path_fanout` Links (default 100) from each intermediate Record, those with the smallest far keys. The bound depends on the intermediate Record only, so the pairs of one start Record can be recomputed alone. Paths have a fixed length: a cycle in the data ends the walk like any other step, and a path may lead back to its start (`(a, a)` is a pair, as for "co-supplied parts").
- Reads: wherever a Relation name is accepted from a Kind (Links of a Record, Links of a Relation, `via` and `count` predicates, `include`, traversal), a path name is too. Following one needs read access to every Kind it crosses, intermediate ones included (403 otherwise), since the pairs tell which Records connect through them. Neighborhoods, Link counts of a Record, Operation `links()` and Derivation expressions see stored Relations only.
- Not materialized (default): composed at read time from the materialized rows of the branch (overlays resolved, row-level security applies): every Link of the first step, then one lateral read per intermediate Record of at most the fan-out, distinct pairs. Both directions read the same pairs (the inverse direction filters the forward composition), so inverse reads of a long path are the costly case.
- `materialized: true`: the pairs are stored in `mat_links` with the Derivation id as `relation_id`, no values and a digest of the pair as `object_hash`; reads use them as stored Links (both indexes, incoming Links through the definer function). They are not tree objects: verification's tree comparison leaves them out and `verify(..., derived=True)` recomputes and compares them (`derived_mismatch` on the Derivation's partition).
- Maintenance, in the commit transaction after derived values: a changed Link of step `i` (for a foreign-key step, a `from` Record written or removed, before and after targets) marks its near Record; walking the steps back without bound gives the start Records to recompute from scratch, at most `store.materialize.derivation_max_incremental_records`, above which the path is rewritten whole. A schema change, a rebuild or a change of `path_fanout` (at the next full recompute) rewrites every materialized path; Links of paths that are removed or no longer materialized are deleted. Writes go through the repository, so overlays copy and tombstone path Links like other rows.
- Time travel composes path Links from the tree (6.12); the tree never holds them.

**Not implemented yet**
- Indexes on derived Attributes (`indexed` has no effect on them), aggregates and expressions over path Derivations, and path Derivations in neighborhoods.
- Changes two hops away (a filter reading `links` of the aggregated Records) are seen by a full rebuild or the next change of the aggregated Records, not incrementally.
- Per-Attribute cast rules in merge policies: cast rules are defined per type pair (5.4) and `allow_checked_casts` is global.

---

## 7. Data model and Postgres schema outline

All tables carry `domain_id` and are protected by row-level security policies on `domain_id` as defense in depth (13.3). Types: `h32` is `bytea` of 32 bytes. Names are indicative; Alembic migrations are the source of truth.

### 7.1 Versioning core

```sql
create table domains (
  id text primary key,
  display_name text not null,
  description text,
  default_branch text not null default 'main',
  leaf_size int not null,              -- trie leaf threshold, fixed at creation
  settings jsonb not null default '{}',
  created_at timestamptz not null, created_by text not null
);

create table objects (                 -- content-addressed elements
  domain_id text not null,
  hash bytea not null,                 -- h32
  object_type smallint not null,
  body jsonb not null,                 -- canonical content
  size int not null,
  primary key (domain_id, hash)
) partition by hash (domain_id);

create table tree_nodes (
  domain_id text not null,
  hash bytea not null,
  body bytea not null,                 -- binary node, lz4 TOAST
  primary key (domain_id, hash)
) partition by hash (domain_id);

create table revisions (
  domain_id text not null,
  id bytea not null,
  root bytea not null,
  generation bigint not null,
  author text not null,
  on_behalf_of text,
  message text not null,
  origin jsonb not null,
  stats jsonb not null,
  created_at timestamptz not null,
  primary key (domain_id, id)
);

create table revision_parents (
  domain_id text not null, revision_id bytea not null,
  position smallint not null, parent_id bytea not null,
  primary key (domain_id, revision_id, position)
);
create index on revision_parents (domain_id, parent_id);

create table changesets (
  domain_id text not null, revision_id bytea not null,
  partition text not null, element_key text not null,
  old_hash bytea, new_hash bytea,       -- null means absent
  provenance jsonb,                     -- {attribute_id: provenance_id}
  primary key (domain_id, revision_id, partition, element_key)
);

create table provenance (
  domain_id text not null, id text not null,
  origin_type text not null, origin_ref jsonb not null,
  actor text not null, at timestamptz not null,
  primary key (domain_id, id)
);

create table refs (
  domain_id text not null, name text not null,
  kind text not null,                   -- branch | tag
  head bytea not null,
  protected boolean not null default false,
  materialization text not null,        -- full | overlay | none
  version bigint not null,
  created_at timestamptz not null, created_by text not null,
  primary key (domain_id, name)
);

create table ref_log (                  -- every head movement
  domain_id text not null, ref_name text not null, seq bigint not null,
  old_head bytea, new_head bytea not null, reason text not null,
  actor text not null, at timestamptz not null,
  primary key (domain_id, ref_name, seq)
);
```

### 7.2 Materialized heads

```sql
create table mat_schema (              -- compiled schema per branch head
  domain_id text, branch text, schema_hash bytea, compiled jsonb,
  primary key (domain_id, branch)
);

create table mat_records (
  domain_id text not null, branch text not null,
  kind_id text not null, key text not null,
  values jsonb not null,               -- {attribute_id: value}
  derived jsonb not null default '{}', -- Derivation outputs
  title text,
  search tsvector,
  object_hash bytea not null,
  lineage jsonb not null,              -- {attribute_id: provenance_id}
  updated_revision bytea not null,
  updated_at timestamptz not null,
  primary key (domain_id, branch, kind_id, key)
) partition by hash (domain_id);
create index on mat_records using gin (search);
create index on mat_records using gin (values jsonb_path_ops);
-- (revision 0011 scopes the search index by (domain_id, branch); 0018 drops the title
-- trigram index for the word lexicon below)
-- per indexed Attribute, created by the schema materializer:
-- create index ... on mat_records (domain_id, branch, ((values->>'att_x')::numeric))
--   where kind_id = 'knd_y';
-- per unique Attribute: the same as a unique partial index.

create table mat_links (
  domain_id text not null, branch text not null,
  relation_id text not null, from_key text not null, to_key text not null,
  values jsonb not null default '{}',
  object_hash bytea not null,
  lineage jsonb not null,
  primary key (domain_id, branch, relation_id, from_key, to_key)
) partition by hash (domain_id);
create index on mat_links (domain_id, branch, relation_id, to_key);

create table mat_lexicon (             -- words of a branch's search vectors (revision 0018)
  domain_id text, branch text, kind_id text, word text,
  records bigint not null,             -- rows of the Kind on the branch holding the word
  primary key (domain_id, branch, kind_id, word)
);
create index on mat_lexicon using gin (domain_id, branch, word gin_trgm_ops);
-- kept by statement triggers on mat_records (8.7)

create table mat_history (             -- per branch, per element change log
  domain_id text, branch text, partition text, element_key text,
  revision_id bytea, at timestamptz, old_hash bytea, new_hash bytea,
  provenance jsonb,
  primary key (domain_id, branch, partition, element_key, revision_id)
);

create table embeddings (              -- content-addressed, shared by branches with one template
  domain_id text not null, object_hash bytea not null,
  model text not null, dims int not null,
  fingerprint text not null,           -- embedding template fingerprint (revision 0024)
  embedding vector not null,
  text_hash bytea,                     -- sha256 of the embedded text (revision 0014)
  primary key (domain_id, object_hash, model, fingerprint)
);
create index on embeddings (domain_id, model, text_hash);
-- per configured dimension: create index ... using hnsw ((embedding::vector(384)) vector_cosine_ops)
--   where dims = 384;

create table embedding_cursors (       -- last complete embedding pass per branch and model
  domain_id text, branch text, model text,
  head bytea, fingerprints jsonb,      -- {kind_id: template fingerprint}
  updated_at timestamptz,
  primary key (domain_id, branch, model)
);
```

### 7.3 Proposals and merges

```sql
create table proposals (
  domain_id text, id text, title text, description text,
  source_ref text, target_ref text,
  origin text,                         -- manual | source_sync | extraction | operation
  status text,                         -- open | merged | closed
  author text, created_at timestamptz, merged_revision bytea,
  checks jsonb, policies jsonb,
  primary key (domain_id, id)
);
create table proposal_reviews (domain_id, proposal_id, reviewer, verdict, comment, at);
create table proposal_items (          -- per-item decisions for extraction Proposals
  domain_id, proposal_id, item_id, partition, element_key, attribute_id,
  evidence jsonb, confidence real, decision text, decided_by text, decided_at timestamptz
);
create table merge_attempts (domain_id, id, proposal_id, base bytea, ours bytea, theirs bytea,
  status text, created_at timestamptz);
create table conflicts (domain_id, attempt_id, id, code text, fingerprint bytea,
  partition text, element_key text, attribute_id text, payload jsonb,
  member_count int, resolution jsonb, resolved_by text, resolved_at timestamptz);
create table conflict_resolutions (domain_id, proposal_id, fingerprint bytea, resolution jsonb,
  resolved_by text, at timestamptz);
```

### 7.4 Governance, ingestion, platform

```sql
create table principals (id, type /* user | service | api_key */, display_name, external_subject, created_at);
create table groups (id, display_name, external_group);
create table group_members (group_id, principal_id);
create table grants (id, domain_id, subject /* principal or group */, role, scope_type /* domain|kind|operation|branch */,
  scope_ref, created_by, created_at);
create table api_keys (id, principal_id, prefix, hash bytea, scopes jsonb, expires_at, last_used_at, revoked_at);
create table operation_invocations (domain_id, id, operation_id, principal, params jsonb, status,
  revision_id bytea, proposal_id text, idempotency_key text, created_at, duration_ms);
create unique index on operation_invocations (domain_id, principal, idempotency_key)
  where idempotency_key is not null;
create table audit_log (seq bigserial, domain_id, at, actor, action, target, details jsonb,
  prev_hash bytea, entry_hash bytea);
create table outbox (id, domain_id, event_type, payload jsonb, created_at);
create table outbox_consumers (name, created_at);       -- `webhooks`, plus any other reader
create table outbox_pending (consumer, event_id, domain_id);  -- per-consumer delivery state
create table webhooks (domain_id, id, url, description, event_types jsonb, branches jsonb,
  clearances jsonb, secret_hash bytea, created_by, created_at);            -- secret_hash: SHA-256 of the secret
create table webhook_deliveries (domain_id, id, webhook_id, event_id, event_type, payload jsonb,
  status /* pending | delivered | dead */, attempts, next_attempt_at, last_status, last_error,
  delivered_at, created_at, updated_at);
create table jobs (id, type, domain_id, payload jsonb, status, priority, run_after, attempts,
  locked_by, locked_at, result jsonb, created_at);
create table source_runs (domain_id, id, source_id, status, started_at, finished_at, stats jsonb,
  revision_id bytea, proposal_id text, error jsonb);
create table documents (domain_id, id, filename, media_type, sha256 bytea, size bigint,
  blob_ref text, text_hash bytea, metadata jsonb, created_at, created_by);
create table document_chunks (domain_id, document_id, seq int, start_offset int, end_offset int,
  text text, chunk_hash bytea);
create table extraction_jobs (domain_id, id, documents jsonb, schema_hash bytea, model text,
  prompt_version text, status, budget jsonb, usage jsonb, proposal_id text, created_at);
create table extraction_cache (cache_key bytea primary key, model text, output jsonb, usage jsonb, created_at);
create table vault (domain_id, id, kind_id, attribute_id, record_key, fingerprint bytea,
  value_ciphertext bytea, key_id text, created_at, erased_at);
```

---

## 8. Read path and search

### 8.1 Principles

- Reads hit the materialized head of a branch (default: the Domain's default branch) and never call an LLM.
- Every response carries the Revision it was served from (`revision` field and `Lorebase-Revision` header). Clients may pass `min_revision` to require freshness when reading from replicas; Keeper waits up to a configured bound, then returns `409`.
- Responses use api names; the query compiler resolves them to ids through the cached compiled schema for the branch head.
- Field-level masking for sensitive Attributes is applied by the query compiler based on the caller's grants.
- Every query has a server-side `statement_timeout` and row caps from configuration.

### 8.2 Query model

A JSON query DSL compiled to SQL. It is deliberately smaller than a graph query language: it covers what agents and Explorer need and compiles to predictable plans.

```json
{
  "kind": "Part",
  "where": {"and": [
    {"attr": "category", "op": "eq", "value": "bearing"},
    {"via": "supplied_by", "where": {"attr": "country", "op": "in", "value": ["IT", "DE"]}},
    {"derived": "single_sourced", "op": "eq", "value": true}
  ]},
  "select": ["key", "title", "category", "lead_time_days"],
  "include": {"supplied_by": {"select": ["key", "title", "tier"], "limit": 5}},
  "order_by": [{"attr": "lead_time_days", "dir": "desc"}],
  "limit": 50,
  "cursor": null
}
```

Operators: `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `between`, `contains` (arrays), `prefix`, `exists`, `match` (full-text on the Record), `near` (geo). `via` compiles to `EXISTS` over `mat_links` joined to `mat_records`; maximum `via` depth is configurable (default 3). Targeting a Trait (`"trait": "Located"`) expands to the implementing Kinds with mapped Attributes. Pagination is keyset-based on `(order key, key)`.

### 8.3 Traversal and neighborhoods

- `traverse`: from one Record along a path of Relations (forward or inverse), with per-hop filters and limits.
- `neighborhood`: from seed Records, breadth-first up to depth `d` with node and edge caps, used by Canvas and by agents that need local context. Implemented as one recursive CTE (`lorebase_store.traverse`, 8.7): the per-Record fan-out cap is a `LIMIT` inside the lateral step, and the Link cap is a `LIMIT` on the lazily consumed walk, so caps bound the work per hop without a round trip per hop.

### 8.4 Search

- **Lexical.** `tsvector` per Record built from title (weight A) and searchable Attributes (configured weights) using the Domain's text search configuration. Ranking by `ts_rank_cd`.
- **Typo tolerance (word lexicon).** Each branch keeps, per Kind, the distinct words of its own search vectors and how many of its rows hold each (`mat_lexicon`, revision `0018`), the approach of the pg_trgm documentation for unique words. A query word that the searched Kinds of the branch do not hold (nor its base, for an overlay), not negated, at least `search.typo_min_length` characters long and without digits, is corrected to its `search.typo_candidates` closest lexicon words at least `search.trigram_threshold` similar by trigrams; the search then runs with `(word | correction ...)` in place of the word (`ts_rewrite`, so phrases, `or` and negation keep their meaning). Hits of the query as written rank first; hits found only through a correction follow, at `search.trigram_weight` times their rank. The response lists the corrections applied (`corrections`). Words are lexemes of the text search configuration (lowercased, stemmed where the configuration stems), so corrections are reported in that form. Trigram matching runs on words, never on whole titles: a common word in long titles no longer turns into a large lossy bitmap (see 8.7).
- **Partial matching.** A query matches every term first (`websearch_to_tsquery`: words and phrases joined by AND). Descriptive queries, the kind agents send, rarely have every word in one Record, so when that finds fewer than `search.partial.min_results` Records (1 by default), Records holding some of the terms follow: the query's words and quoted phrases joined by OR, each alternative keeping the query's negations, stop words of the text search configuration dropped as in the original. At most `search.partial.candidates` of them, the best by `ts_rank_cd` of that OR query, are scored `weight * (rank + term_bonus * terms held)`, a term counted when the Record matches it alone (corrections applied per term), and ranked after every full match; full matches keep their order and score. Each hit reports `match`: `all` or `partial` (absent for hits found by vector only). In hybrid search the lexical list carries partial matches into the fusion the same way, so a descriptive query no longer leaves hybrid equal to semantic.
- **Semantic (optional).** Embeddings are computed offline by Scribe (9.3; default fastembed, in-process on CPU) from a per-Kind template over the Record content, keyed by `object_hash` and the template's fingerprint so all branches and Revisions sharing the same content under the same template share the vector. Vectors live in pgvector with HNSW indexes per configured model; the index dimension comes from configuration.
- **Query vectors.** Semantic and hybrid search take a `query_vector` supplied by the caller. Keeper and Herald never call a model, not even an embedding model. `describe_domain` and `GET /schema` advertise the embedding model name and dimension so agents compute compatible vectors themselves (for example with the same fastembed model, locally and for free). Without a vector, search runs in lexical mode. **[Architect call AC-17]**
- **Where query vectors come from (implemented).** The caller embeds its query with the model Scribe uses, on its own side. `lorebase_inference` is the only package that calls models, and the import contracts allow it in Scribe only among the services: Keeper and Herald must not import it, and the CLI and the Python client depend on nothing internal. So the query side is: `lorebase-inference embed TEXT` prints `{model, dims, vector}` from the configured `inference.embeddings` (the same settings as Scribe's, so the same model and dimension), and `lorebase search TEXT --vector-file FILE` (or `-` for standard input) sends it as `query_vector`; Python callers use `create_embedding_model(settings).embed([text])` and pass the vector to `records.search`. Herald takes `query_vector` as given and never embeds text: a Herald that turned text into vectors through an embedding sidecar would put a model call (and its latency, cost and failure modes) back in the agent read path, which AC-17 rules out. Agents that need semantic search over MCP embed on their side, as above. A Keeper with `search.semantic.model` naming a model with no stored vectors returns no semantic hits; a vector of another dimension than `store.embeddings.dims` is refused with `dimension_mismatch`. `GET /schema` (and `lorebase_schema` in Herald) advertises the model: a `search` block with `modes` and `embedding: {model, dims}`, set when a Kind of the schema has an `embedding_template`. Keeper reads the name from `search.semantic.model` and the dimension from `store.embeddings.dims`; it does not import `lorebase_inference`. Models that expect prefixes use `inference.embeddings.query_prefix` (applied by `lorebase-inference embed --query`) and `document_prefix` (applied by Scribe).
- **Hybrid.** Reciprocal rank fusion of lexical and vector candidate lists, computed in SQL, with filters applied before fusion.
- Search results return `{kind, key, title, snippet, score, match, revision}` and the typo `corrections` applied; snippets come from `ts_headline` on searchable Attributes.

### 8.5 Caching

- Compiled schema, CEL programs and query plans cached in-process by schema hash.
- Tree nodes cached by hash (immutable).
- HTTP `ETag` on Record reads = hash of `(object_hash, derived hash, mask profile)`; conditional requests return `304`.
- No external cache is required. A shared cache may be added later behind an interface.

### 8.6 Implementation notes

Decisions taken in `lorebase_core.query` (DSL model and validation) and `lorebase_keeper.reading` (SQL compilation, reads, search, history and diff). They refine 8.1 to 8.4 and 6.7, 6.12.

- **DSL.** Predicates are told apart by their leading member: `and`, `or`, `not`, `attr`, `derived`, `via`, `count` (Link count compared with `eq` to `gte` or `between`, optionally over a `where`), `match` (full-text on the Record). `key` and `title` are pseudo fields (an Attribute named `title` wins). `aggregate: {group_by}` counts matches per Kind and value. Operators per type are listed in `query/resolve.py`; `ne` and the comparisons never match absent values. Every problem of a query is reported at once with a JSON pointer.
- **Compilation.** Values reach SQL as bound parameters only. Equality on a scalar Attribute of one Kind uses `values @> {...}` (GIN); other comparisons cast `values ->> id` to the Attribute's SQL type. Links of `foreign_key` Relations are read from the `from` Records holding the Attribute. Ordering is keyset based on `(value IS NULL, value, [kind], key)`, so absent values sort last in both directions; cursors are bound to the query shape.
- **Authorization** runs after the read is resolved, against what is read (`services/reads.py`, shared by the record, query, search, Revision, log, diff, schema and branch reads): a branch head needs the permission covering that branch; a branch named in `at` or as a diff side needs it on that branch; a Revision named by id or tag needs a grant not narrowed to branches (Domain-wide, or narrowed to the Kind read), or a grant on some branch from whose head the Revision is reachable within `keeper.reads.reachability_max_depth` parent hops. Unknown names answer 403 to callers whose grants are narrowed. A grant narrowed to a Kind reads that Kind on every branch and Revision and nothing else: every Kind a request touches (target, `via`/`count` predicates, includes, the far side of Links, traversal hops) must be granted or the request gets 403; link counts, neighborhoods and searches without explicit Kinds leave other Kinds out, and cached responses are keyed by the readable Kinds. History reads (log, Revisions, diff) need a grant not narrowed to a Kind.
- **Caching.** GET reads answer with `ETag` = the Revision served (both Revisions for a diff) plus a digest of path and canonical query, `Cache-Control: private, must-revalidate`, and 304 on a matching `If-None-Match`; this replaces the per-Record tag of 8.5. Responses are cached in process behind the `ResponseCache` protocol, keyed by `(Domain, Revision, path, query)` and bounded by `keeper.reads.cache` (entries, bytes, time to live, body size, time travel entries). The lookup runs after authorization; keys hold the Revision, so commits need no invalidation. POST reads carry an `ETag` of the Revision and the body but are not cached.
- **Responses** keep canonical values (integers and decimals as strings, 6.1). Values of sensitive Attributes are masked for callers without the clearance (10.2, 13.5 implementation notes). Kind-scoped invisibility in queries (13.3) is not applied yet; Kind-scoped grants are checked on the `kinds/{kind}` routes only.
- **Time travel.** `at` takes a Revision id, tag or branch. At the branch head the materialized rows serve the read; otherwise the tree does: key reads, Kind, Trait and Link listings ordered by key (whole partitions scanned, refused above `read.time_travel.max_scan_entries`). A Trait listing reads the partition of every implementing Kind and orders like the head (by Kind id and key, or by key and Kind id when ordered by `key`), so its cursors are the head's. Links of a path Derivation are composed from the Links of its steps at that Revision, materialized or not. Filters, ordering, includes, aggregates, Link and lineage summaries, traversal and search are refused at other Revisions.
- **Search.** Lexical and hybrid search first ask the store for typo corrections (`search_corrections`, `lb_search_corrections`); lexical candidates then match the query or, apart, the corrected query, up to `store.reads.search_candidates` each, with an `exact` flag that orders them; when full matches are fewer than `search.partial.min_results`, partial matches follow (a second call of `search_records` with the OR query, then one primary-key read of the candidates' vectors to count the terms each holds; compiled searches do the same in one statement, the partial list guarded by the count of full matches). Without `where`, lexical search runs the store primitive `search_records` (`lb_search_corrected`, 8.7) and computes snippets for the returned page only; snippets highlight corrected words too. Semantic search ranks stored embeddings of the configured model by cosine distance to the caller's vector. Hybrid fuses both lists with `w / (rrf_k + rank)` in SQL. Search has no cursor.
- **Neighborhoods and Links.** `POST /neighborhood` runs the store primitive `lorebase_store.traverse.neighborhood` (one statement, `lb_neighborhood` under the definer setting of 8.7): nodes come nearest first, each Link once under its forward name, caps are the smaller of `read.traverse` and `store.traverse`, and `truncated` also reports a Record whose fan-out cap was reached. The inbound side of `GET .../links/{relation}` (an inverse name) runs `links_to_record`. Comparisons on an indexed Attribute of one Kind repeat the partial index predicate and expression (`IndexSpec`), so a role not subject to RLS reads them through the index. GET reads at a branch head answer `If-None-Match` from `head_revision` before the read point is resolved.
- **Diff.** Only differing nodes are loaded, level by level in batched queries, then the core walk runs in memory; a cheap lower bound from partition counts and `read.diff.max_changes` refuse oversized diffs. Pages are offsets into the deterministic diff order; `format=ndjson` streams all changes. The linear-history changeset union of 6.7 is not used.
- **Log.** The first-parent chain (or every ancestor with `all=true`) is walked with a recursive query on each page; pages continue by `(generation, id)`.

### 8.7 Agent read budgets

An external agent reasons in a loop of reads, so every read it makes must cost milliseconds on the materialized head, with no model call in the path. Budgets (p95, one read as one request: a transaction that scopes RLS and runs one statement), on a laptop with 100k Records and 300k Links; `docs/performance.md` holds the measured baseline and `tests/perf/bench.py` regenerates it.

| Read | p95 budget |
|------|-----------:|
| Record get by key | 5 ms |
| List page of 50 | 20 ms |
| Links of a Record | 10 ms |
| Neighborhood, depth 2 | 30 ms |
| Query on an indexed Attribute, page of 50 | 25 ms |
| Full-text search, 20 results | 50 ms |

**Primitives.** `lorebase_store.reads` and `lorebase_store.traverse` give Keeper one statement per read, each shaped for an index scan: `get_record`, `list_records` (keyset on key), `hydrate_records` (batched by `(kind, key)` arrays), `links_of_record`, `query_by_attribute`, `search_records`, `neighborhood`, `head_revision`, `head_schema`. Caps come from `store.reads` and `store.traverse`.

**Index strategy.**
- Records by key and pages: the `mat_records` primary key `(domain_id, branch, kind_id, key)`; Links: the `mat_links` primary key (outgoing) and `(domain_id, branch, relation_id, to_key)` (incoming).
- Attributes flagged `indexed` or `unique`, and `ref` Attributes backing a foreign-key Relation, get a partial expression index per Domain, branch, Kind and Attribute on the Domain's hash partition (`lorebase_store.indexes`; store schema notes). The cast follows the type (`bigint`, `numeric`, `double precision`, `boolean`, canonical text otherwise; hash for long `text`, `md5` for unique long `text`). A query matches the index only when it repeats the predicate literals and the expression, so query compilers take both from `IndexSpec` (`expression`, `value_sql`) and `predicate_sql`. Indexes are created in the commit transaction for small Kinds and queued as concurrent builds above `store.indexes.inline_max_rows`; those that would reject new rows (unique ones, casts of a retyped Attribute) are dropped before the rows are written. Index DDL needs a role that owns `mat_records`: a commit role that does not own it (the application role subject to row-level security) queues the changes for the job worker, which runs them as the maintenance role of `database.maintenance_dsn`; without one it skips index management with a warning, and `python -m lorebase_store.indexes plan` prints the DDL an administrator applies.
- Search: the composite GIN index `ix_mat_records_branch_search` on `(domain_id, branch, search)` (revision `0011`, operator classes from `btree_gin`); typo corrections read `ix_mat_lexicon_branch_word` on `(domain_id, branch, word gin_trgm_ops)` and the lexicon's primary key (revision `0018`, which drops the title trigram index of `0011`). Domain and branch are index conditions, so the bitmap holds the branch's matches only, however many branches share the hash partition. The query finds at most `store.reads.search_candidates` matches with `@@` (Kind filter inside that subquery) and ranks only those with `ts_rank_cd`, so common terms cost a bounded amount; `websearch_to_tsquery` parses agent input safely. Snippets (`ts_headline`) and hybrid fusion apply to the final page or to the bounded candidate lists, never to all matches. `VACUUM ANALYZE` (autovacuum) keeps the GIN pending list and statistics current after bulk loads.

**Neighborhood primitive.** One recursive CTE (8.3): seeds, then per hop a lateral union of stored outgoing and incoming Links and foreign-key Links (through the Records and their per-Attribute index), `LIMIT fanout` per Record, `CYCLE` on `(kind_id, key)`, the walk consumed under a `LIMIT` on Links, nodes ranked by depth up to the node cap and hydrated in the same statement, Links returned only between returned nodes. Depth, seeds, fan-out, Links and nodes are capped by `store.traverse`.

**Prepared statements and plans.** SQL text is stable per shape (literals vary only with Domain, branch, Kind and Attribute for partial-index queries), so the asyncpg driver reuses one prepared statement per connection and shape (`database.prepared_statement_cache_size`). Store tests check the custom and the generic plans of the hot paths for index scans.

**Schema and ETag.** `head_revision` reads the branch head with one primary-key lookup. Keeper derives `ETag` from it (plus the caller's mask profile) for collection, query, search and neighborhood reads, and from the Record's `object_hash` for Record reads (8.5); `If-None-Match` is answered with `304` before any other read. `head_schema` serves the compiled schema from a per-process LRU keyed by Domain and Revision id (`store.reads.schema_cache_entries`), which can never be stale. A request whose response must match the Revision it reports runs its reads in one `REPEATABLE READ` transaction.

**Row-level security.** The `domain_isolation` policies make Postgres apply the policy before any condition built from functions not marked leakproof. Text equality and ordering are leakproof, so key, page and stored-Link reads keep their index plans under a role subject to RLS. `->>`, casts and `@@` are not: under such a role, attribute queries filter the Kind's rows, search scans the Domain's partition, and inbound foreign-key Links (and the neighborhoods that follow them) filter every Record of the from Kind. With plain SQL the baseline measured search, Links of a Record and the neighborhood over budget for such a role. Options were (a) a read-only role with `BYPASSRLS` used through the store primitives, (b) `SECURITY DEFINER` functions for the affected primitives, (c) accepting the slower paths; marking the built-in functions leakproof is not an option, because casts raise errors that reveal values. **Settled: (b).** Revision `0005` adds `lb_search`, `lb_links_in`, `lb_neighborhood` and `lb_query_by_attribute`, owned by the migration role, `STABLE`, with a fixed `search_path`, taking the Domain as an argument and pinning it in every predicate; EXECUTE goes to `database.application_role` only. RLS stays forced on every table and no login role used for reads bypasses it. The store primitives call the functions when `store.reads.use_definer_functions` is true (the default) and run plain SQL otherwise; the security argument is in the store schema notes, and `docs/performance.md` has the numbers for both roles.

**Overlay branches.** Reads on an overlay union its rows with the base rows as of the fork point inside each statement (store schema notes) and keep the index plans of a full branch. Search on an overlay reads both halves through the branch-scoped indexes above and lets the planner hash the overlay's keys when the matches outnumber them (revision `0011`). The overlay neighborhood looks Links and Records up with one index scan over both branches per hop, the overlay's row of an identity winning over the base's (`lb_overlay_lookup`, revision `0012`); inbound foreign-key hops still read both halves through their per-Attribute indexes. `docs/performance.md` compares overlay reads with a copy under the same commits.

**Lexicon maintenance.** Statement-level triggers on `mat_records` (transition tables, `lb_lexicon_maintain`) apply each statement's word count changes in the writing transaction, so every path that writes rows (commits, copy-on-write into overlays, promotion, re-fork, rebuild, branch copy and deletion) keeps the lexicon exact without code of its own; rows whose search vector did not change cost an anti-join and nothing else, and rows of a branch whose ref is gone are skipped. An overlay holds the words of its own rows; corrections on it add its base's (a superset of what it reads, which can only add a candidate that matches nothing). Measured in `docs/performance.md`.

**Deferred.** Read replicas with `min_revision` (8.1); per-relation keyset pagination of Links.

---

## 9. Ingestion: Sources, extraction, Proposals

All ingestion runs in Scribe workers, which pull jobs from the Postgres `jobs` table with `SELECT ... FOR UPDATE SKIP LOCKED`. No external broker. **[Architect call AC-18]** Scribe writes through the same commit pipeline as Keeper, under a service principal, so every ingested change has a Revision, Lineage and an audit entry.

### 9.1 Structured Sources

**Connectors** (M2): Postgres (table or read-only query, via asyncpg with a read-only role), CSV (dialect detection, encoding), Parquet (via `pyarrow`). Each connector implements:

```python
class SourceConnector(Protocol):
    async def describe(self) -> SourceDescription      # columns, types, keys, foreign keys, row estimate
    async def sample(self, n: int) -> list[Row]
    async def read(self, since: Watermark | None) -> AsyncIterator[RowBatch]
```

**Schema inference** (`lorebase source infer`, Modeler wizard). Rule-based (extraction tier 0): structured sources never need a model.
- Column types map to Attribute types with a fixed table (for example `numeric(p,s)` to `decimal`, `varchar` to `string`, `text` to `text`, `timestamptz` to `timestamp`); CSV types are inferred from a sample with confidence and shown for confirmation.
- Primary keys come from constraints or unique non-null columns.
- Foreign keys become Relations with cardinality from uniqueness (`many_to_one`) and join tables with two foreign keys become `many_to_many` Relations with link Attributes.
- Display names are humanized; descriptions come from column comments when present.
- Output: a schema change set on a new branch and a Proposal. Inference never writes to `main`.

**Sync run:**
1. Read source batches; apply mapping and transforms; build canonical Record and Link objects.
2. Look up current hashes for the batch keys on the Source branch (indexed reads on the materialized head).
3. Emit changes only for differing hashes. For `snapshot` mode, keys owned by this Source (by Lineage and scope) and not seen in the run become deletions.
4. Commit in batches (configurable, default 5k changes per Revision) on the Source branch.
5. Open or update the Source's Proposal into the target branch; apply the merge policy (`auto_when_clean`, `proposal`, `auto_with_authority`).
Provenance per changed Attribute: `source_sync` with Source id, run id and row locator.

### 9.2 Unstructured extraction

Documents are untrusted input. Models propose; the schema, validators, entity resolution and reviewers decide. Extraction runs in tiers, from free and deterministic to model-backed; every tier produces Proposals and none writes to a protected Branch. **[Architect call AC-27]**

**Extraction tiers**

| Tier | Technique | Models | API cost | Typical use |
|------|-----------|--------|----------|-------------|
| 0. Deterministic | Rules only: schema inference from tables and CSV (9.1), Source mappings, pattern extractors (regular expressions per Attribute), dictionary matching against titles and aliases of existing Records, header-mapped tables inside documents | none | zero | All structured sources need nothing else. Well-formed documents (forms, tables, identifiers) |
| 1. Local open models | Zero-shot entity extraction with GLiNER (labels and descriptions taken from Kinds), relation extraction with GLiREL (labels taken from Relations), optional spaCy pipelines for sentence splitting and custom entity models | small CPU models, in-process, Apache-2.0 code; weights chosen by configuration | zero | High-recall entity and relation candidates with exact character spans |
| 2. Any LLM | Structured extraction through the inference abstraction (9.4) with output enforced by a JSON schema derived from the Domain schema; small local models (Qwen, Llama, Gemma families served by Ollama) are first-class targets | any OpenAI-compatible endpoint, local or hosted | zero locally; per token when hosted | Attribute values, complex or implicit relations, schema discovery |

A job declares its tiers. Default: `[0, 1, 2]` when an LLM is configured, `[0, 1]` otherwise. Tiers compose: tier 0 and tier 1 candidates are passed to tier 2 as hints (mentions and spans), which keeps prompts small and lets small models focus on values; when tier 2 is disabled, tier 1 candidates go straight to validation and entity resolution. Every Proposal item records the tier, extractor and model that produced it.

**Pipeline** (Scribe job `extraction`):

1. **Intake.** Document stored through the blob store interface (default backend: Postgres `bytea`; optional S3-compatible backend). SHA-256 recorded; duplicates detected.
2. **Parse.** Parser plugins convert to text with structure (headings, pages, tables) and stable character offsets. Default parsers: PDF via `pypdf`, DOCX via `python-docx`, HTML via `beautifulsoup4`, Markdown and plain text natively. OCR is out of scope; scanned PDFs are flagged.
3. **Chunk.** Structure-aware chunks with overlap, each with `(start_offset, end_offset, chunk_hash)`.
4. **Tier 0 and tier 1.** Rule extractors and local span models run per chunk and emit candidates with exact offsets (evidence is the span itself).
5. **Tier 2 prompt.** For each chunk, the prompt includes the target Domain schema subset (Kinds, Attributes, Relations with descriptions) as a JSON Schema for structured output, the tier 0 and 1 hints, and instructions to return only facts present in the text with verbatim evidence quotes. The model has no tools.
6. **Tier 2 model call.** Through the `ChatModel` interface (9.4) with `response_format` of type `json_schema`. If the endpoint lacks JSON schema support, Lorebase falls back to JSON mode plus schema validation and at most one bounded repair attempt; outputs that still fail are discarded and counted.
7. **Cache.** Output cached by `sha256(tier, extractor, model, model_digest, prompt_version, schema_hash, chunk_hash)`. Re-runs on unchanged inputs are free and reproducible.
8. **Validate.** JSON Schema validation, type casting, constraint checks. **Evidence verification**: each extracted value must cite a quote that appears in the chunk (normalized whitespace match); items without verifiable evidence are discarded and counted.
9. **Resolve entities.** Match candidates to existing Records: exact key, normalized title equality, trigram similarity, optional embedding similarity, plus Attribute agreement scoring. Outcomes: `matched` (update proposal), `new` (create proposal), `ambiguous` (top candidates kept for the reviewer). Thresholds configurable per Kind.
10. **Schema discovery (optional mode).** When the model reports facts that do not fit the schema, they are aggregated into schema suggestions (new Attribute, new Kind, new Relation) with frequency and examples.
11. **Propose.** Changes are committed to branch `extract/{job_id}` and a Proposal is opened with one **Proposal item** per proposed Record, Attribute value, Link or schema suggestion, each with evidence (document, page, offsets, quote), confidence and match details.
12. **Review.** Reviewers accept or reject items individually, in bulk, or by threshold. Rejecting an item commits a revert change on the extraction branch. Merging follows the normal merge algorithm; Conflicts with concurrent changes are resolved as usual.

**Budgets and safety.** Per-job limits on tokens, cost estimate, documents and concurrency; jobs stop cleanly when a limit is reached. Budgets are expressed in tokens and in cost; prices per token come from configuration (zero for local models). Prompt injection inside documents can only influence proposed values, which are validated, evidence-checked and reviewed; it cannot trigger actions because the model has no tools and no write access. **[Architect call AC-19]**

**Provenance.** `extraction` with job id, document id, offsets, quote hash, model and prompt version.

### 9.3 Embedding jobs

Scribe computes embeddings for Records whose `object_hash` has no vector for the configured model, through the `EmbeddingModel` interface. Triggered by commits on materialized branches (outbox event) and by backfill commands. **[Architect call AC-26]**

- **Default: fastembed.** ONNX models in-process on CPU: no server, no key, no API cost.
- **Alternatives:** Ollama embeddings, or any OpenAI-compatible embeddings endpoint (local or hosted).
- **Model name and dimension come from configuration.** The pgvector index for a model is created with the configured dimension; at startup Scribe embeds a probe string and fails fast if the returned dimension differs.
- **Switching model** creates new vectors keyed by the new model name next to the old ones; search uses the active model; old vectors are removed by GC once the backfill completes.

**Implementation notes (M4).**

- **What is embedded.** A Kind opts in with `search.embedding_template`, a text with `{attribute}` placeholders (api names) plus `{_kind}` and `{_key}` (`lorebase_core.model.embedding`). Rendering is deterministic (absent values empty, lists joined with `, `, whitespace collapsed). Static validation (`schema.embedding_template_invalid`) refuses unknown Attributes, derived Attributes (their values are not part of the object hash that keys the vector) and sensitive Attributes unless `search.embedding_sensitive` is true.
- **Trigger.** A scheduled sweep rather than the outbox: the scheduler leader queues an `embeddings` job per Domain every `scribe.scheduler.embeddings_interval_s` (an identical queued job is reused). The outbox now keeps a delivery state per consumer (10.5), but the sweep stays: it is self-healing, finding whatever lacks a vector whatever happened before, including Records written before a consumer existed. `lorebase-scribe embeddings DOMAIN [--branch B] [--reindex] [--prune]` runs a pass at once (backfill).
- **A pass** covers the materialized branches of the Domain. A branch with no template is skipped without creating the model; a branch whose head equals the head recorded in `embedding_cursors` for the model is skipped (a schema change moves the head). Templates are resolved once per schema hash and kept for the last `scribe.embeddings.template_cache_size` schemas, so branches sharing a schema read and parse it once. Otherwise the branch's own live rows are read in key order, page by page: rows whose object hash has no vector of the model under their Kind's template fingerprint, and every row of the Kinds whose template fingerprint changed since the last complete pass (all Kinds with `--reindex`). Each text is rendered and hashed (`text_hash`): equal to the stored vector's hash, it is unchanged; equal to another stored text, its vector is copied without a model call; otherwise it is embedded in batches. A complete pass records the head and the fingerprints.
- **Overlays and shared rows.** Vectors are keyed by object hash, model and template fingerprint (revision `0024`), and a branch reads only its own rows, so an overlay's inherited rows (covered by its base) and the unchanged rows of a copied branch cost nothing; a branch adds work only for the Records it changed. Two branches whose templates differ for the same content keep a vector each (copied without a model call when the rendered texts are equal), and Keeper looks vectors up with the fingerprint of the searched branch's template, so a pass on one branch never changes what another branch's search ranks by.
- **Backpressure, rate limits, retries.** A job reads at most `scribe.embeddings.max_records_per_job` rows and queues its continuation (`resume` after the last row) instead of holding a worker slot; model calls are paced by `max_calls_per_second` and retried `retry.attempts` times inside the job after the provider's own retries; then the worker retries the job (`store.jobs`). One pass per Domain runs at a time (advisory lock). Dimension mismatches (the model against `store.embeddings.dims`, or the probe text's vector) fail the job without retries.
- **Model changes.** Vectors and cursors are keyed by the model name (`fake:<model>` for the fake provider), so a new model has no cursor and its first passes embed everything; Keeper searches `search.semantic.model`, switched once the backfill is complete; `--prune` then deletes the vectors of other models. Garbage collection (6.13) deletes the vectors no branch reads, of any model: object hashes no branch holds any more, and fingerprints of templates no branch holding the content uses.
- **Metrics.** `lorebase.scribe.embeddings.records` by outcome (`embedded`, `reused`, `unchanged`, `empty`) and `lorebase.scribe.embeddings.call.duration`, plus the job metrics of type `embeddings` ([metrics](metrics.md)).

### 9.4 Model-agnostic inference

Lorebase never depends on a specific model vendor. All model use lives in one package, `lorebase_inference`, used only by Scribe. **[Architect call AC-25]**

**Interfaces**

```python
class ChatModel(Protocol):
    async def complete_json(self, messages: list[Message], schema: JsonSchema,
                            options: ChatOptions) -> ChatResult      # parsed JSON, usage, model id

class EmbeddingModel(Protocol):
    name: str
    dims: int
    async def embed(self, texts: list[str]) -> list[list[float]]

class SpanExtractor(Protocol):                                      # tier 1
    async def extract(self, text: str, labels: list[Label]) -> list[Span]
```

**One chat implementation: OpenAI-compatible HTTP.** A single client speaks the OpenAI-compatible Chat Completions protocol over plain HTTP (`httpx`). There is no vendor SDK in the core. Three settings select the model:

| Setting | Alias of | Example |
|---------|----------|---------|
| `LLM_BASE_URL` | `inference.llm.base_url` | `http://ollama:11434/v1` |
| `LLM_MODEL` | `inference.llm.model` | any model name served by the endpoint |
| `LLM_API_KEY` (optional) | `inference.llm.api_key` | secret reference; omitted for local servers |

**Local by default.** The docker compose `local-models` profile runs Ollama and pulls the configured model; the same settings work with vLLM, LM Studio and the llama.cpp server. Small open models (Qwen, Llama, Gemma families) are first-class targets: prompts, JSON schemas and evaluation sets are tuned and measured on them, not only on large hosted models. **[Architect call AC-28]**

**Paid hosted models are first-class through the same abstraction.** OpenAI, Azure OpenAI, Anthropic, Google, Amazon Bedrock and Mistral are supported:
- **natively** when the provider exposes an OpenAI-compatible endpoint (set `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`);
- **through a LiteLLM proxy** otherwise, or when an enterprise wants central routing, key management, budgets and fallbacks across providers. The proxy runs as a separate container (compose profile `litellm`); Lorebase talks to it as one more OpenAI-compatible endpoint and never imports a provider library.
Which providers are native and which go through the proxy is kept in a documented compatibility matrix, because provider endpoints evolve. Switching provider is a configuration change only; no code change, no rebuild.

**Per-task routing.** Optional overrides per task (`inference.llm.tasks.extraction`, `inference.llm.tasks.schema_discovery`) let a team use a small local model for bulk extraction and a larger hosted model for schema discovery, each with its own endpoint, key and price.

**Deterministic fake provider.** `provider: fake` implements `ChatModel`, `EmbeddingModel` and `SpanExtractor` without network access: chat answers come from fixture files keyed by prompt hash, or from a rule-based responder that returns schema-valid output built from quoted text in the prompt; embeddings are deterministic unit vectors of the configured dimension derived from `sha256(text)`; spans come from dictionary matching. Unit tests, integration tests and CI use it with network access disabled. **[Architect call AC-29]**

**The read path never calls a model.** Keeper and Herald do not depend on `lorebase_inference`; an import-linter contract fails the build if they do. Semantic search uses caller-supplied vectors (8.4). **[Architect call AC-30]**

**Cost model**

| Setup | Components | API keys | Marginal cost |
|-------|------------|----------|---------------|
| Laptop demo (full 5-day scenario) | Postgres, Keeper, Scribe, Herald, web, Ollama with a small open model, fastembed, GLiNER and GLiREL on CPU | none | zero (local compute only) |
| Team server, local models | Same, with a GPU for Ollama or vLLM for faster tier 2 | none | zero API spend; hardware only |
| Enterprise, hosted models | Same, `LLM_BASE_URL` pointing to a hosted provider or a LiteLLM proxy | provider keys as secret references | per token for tier 2 only; tiers 0 and 1 stay free; budgets per job |
| Agent reads (all setups) | Keeper, Herald, Postgres | none | zero model cost: no model in the read path |

Throughput figures for each tier on reference laptop and server hardware are measured by the M4 benchmark and published with it.

---

## 10. Operations and governance

### 10.1 Execution pipeline

```
request -> authenticate -> resolve principal and grants
        -> authorize (Domain role, Operation grant, Kind scope, branch rules)
        -> idempotency check (principal, key)
        -> validate parameters (types, references exist and are readable)
        -> evaluate preconditions (CEL)
        -> compute edits (steps)
        -> validate edits (schema, constraints, uniqueness, cardinality, Traits)
        -> dry_run? return edits and violations
        -> review_policy? open Proposal on branch op/{invocation} with the edits
        -> else commit pipeline on the target branch
        -> audit entry, outbox events, invocation record
```

Responses always contain `status` (`committed`, `proposed`, `rejected`, `dry_run`), the computed edits, violations if any, and the Revision or Proposal id. A caller that names a `task` gets the Proposal in one 202 response: the Operation runs on `agent/{principal}/{task}`, the Proposal is opened or updated and, where the Domain allows it for the caller's role, merged ([agents](agents.md#writes-that-need-review)).

### 10.2 RBAC

**Roles** (per Domain; custom roles may be composed from permissions):

| Role | Permissions |
|------|-------------|
| `viewer` | read schema and data (subject to sensitivity masking) |
| `explorer` | viewer plus saved views |
| `editor` | execute built-in Operations on unprotected branches; create branches; trigger Source synchronizations (`source.sync`) |
| `operator` | execute granted custom Operations on any branch allowed by policy |
| `modeler` | schema change sets on branches; Source mappings |
| `reviewer` | review Proposals, resolve Conflicts |
| `maintainer` | merge Proposals into protected branches; manage Sources and jobs |
| `admin` | grants, API keys, Domain settings |
| `agent-reader` | viewer for service principals, no saved views |

**Scopes.** Grants apply at `domain`, `kind` (read or write limited to some Kinds), `operation` (execute a specific Operation), or `branch` pattern (`agent/*`). Deny by default.

**Sensitivity.** Attributes tagged `pii`, `confidential` or `restricted` require a matching clearance permission; otherwise values are masked (`"***"`) or omitted per configuration, in REST, MCP, search snippets, exports and embeddings (sensitive Attributes are excluded from embedding templates unless explicitly allowed).

**Groups** map from OIDC claims (configurable claim name). Service principals for agents are created by admins and receive API keys.

### 10.3 Proposals as governance

- Proposal checks: schema validity, merge preview (Conflicts count), size, required reviewers, CI-like custom checks (CEL predicates over the diff, for example "no deletion of more than 1% of Suppliers").
- Branch protection rules per protected branch: required approvals count, required roles, allowed merge origins.
- Self-approval is configurable and off by default for protected branches.

### 10.4 Audit

- Every authenticated mutation, merge, grant change, key creation, export and denied attempt is written to `audit_log`.
- Entries are hash-chained (`entry_hash = sha256(prev_hash || canonical entry)`) for tamper evidence; `lorebase admin audit verify` checks the chain.
- Audit is distinct from application logs and is exportable (JSON lines, OpenTelemetry log export).

### 10.5 Side effects

Operations declare named events. Events are written to the `outbox` in the commit transaction and delivered asynchronously by Scribe to configured webhook targets (HMAC-signed payloads, retries with backoff, dead-letter state). Lorebase does not call external systems synchronously during a commit.

**Events.** A committed execution of an Operation with `side_effects: [risk_flag_raised]` writes an `operation.risk_flag_raised` event in the transaction of its last Revision, invocation record and audit entry: a failure anywhere rolls all of them back, and dry runs, refused executions and idempotent replays write none. An execution sent to review (agent writes on a task branch: their Proposal is the review) defers its events (`operation_deferred_events`, keyed by branch and the head after the execution, migration `0024`): the merge of a Proposal from that branch writes, in its transaction, those whose Revision the merged source head contains and deletes them, so each is written once; they then name the target branch, the merged head, the source branch and the Proposal. Deleting the branch unmerged drops them. Side effect names are unique snake_case names of at most 64 characters, checked by static validation (`schema.api_name_invalid`, `schema.api_name_duplicate`). The payload names the execution (Operation, invocation, principal, branch, Revisions, head), carries the parameters as the invocation records them (values that may be sensitive are `***`) and up to `keeper.operations.side_effect_max_edits` edits. Every commit also writes `revision.committed`.

**Consumers.** Each consumer of the outbox owns its delivery state: a trigger adds an `outbox_pending` row per registered consumer (`outbox_consumers`, `webhooks` by default) in the transaction writing the event, and the consumer deletes its row in the transaction that hands the event off. A position in the id sequence would skip an event whose transaction committed after later ones; pending rows cannot. An event is pruned when no consumer has it pending and it is older than `store.outbox.retention_s`, or when it is older than `store.outbox.max_age_s` whatever is pending (a consumer that stopped), each pruning pass deletes batches of `store.outbox.prune_batch` (one short transaction each) until nothing is left to prune or `store.outbox.prune_budget_s` is spent, and reports what remains in `lorebase.outbox.prune.backlog`. Deleting a consumer's row drops its backlog.

**Delivery.** The webhook worker fans events out into `webhook_deliveries` (one per subscribed webhook: exact types or `operation.*`, branches) and sends them at least once: every attempt of an event carries `Idempotency-Key: evt_<sequence>`, failures are retried with backoff and become `dead` after `webhooks.max_attempts`. Deliveries are not ordered (retries interleave with newer events); payloads carry `sequence`, the outbox id, for receivers that need an order.

**Masking.** Payloads never hold a sensitive value as such: the writer wraps each value of a tagged Attribute with its effective tags (`{"$sensitive": {"tags": [...], "value": ...}}`), a vault Attribute's value being its vault reference. The worker renders the body for each webhook when it sends it: values whose tags the webhook's `clearances` cover are shown (vault values opened then, `[erased]` once erased), others are `***`. Granting a webhook a clearance needs that clearance permission on the whole Domain; it is fixed when the webhook is created (delete and recreate the webhook to change it). A wrapper without tags is never cleared, and JSON written by a caller that holds `$sensitive` or `$vault` keys is wrapped with every tag, so it cannot pose as a wrapper or a reference. Vault values are never in stored payloads; values of sensitive Attributes outside the vault are kept as committed until pruned, like the Revision objects holding them (erasure reaches vault values only, 13.5).

---

## 11. Agent interface: REST and MCP

### 11.1 REST (Keeper)

Base path `/api/v1`. JSON. OpenAPI 3.1 at `/api/v1/openapi.json`. Errors use RFC 9457 problem details. Pagination by opaque cursor. Every read accepts `branch` (default: Domain default branch) and, where supported, `at` (Revision id or tag).

| Area | Method and path | Notes |
|------|-----------------|-------|
| Domains | `GET /domains`, `POST /domains`, `GET /domains/{d}` | |
| Branches | `GET /domains/{d}/branches`, `POST .../branches`, `GET .../branches/{b}`, `DELETE .../branches/{b}` | branch names URL-encoded |
| Revisions | `GET .../branches/{b}/log`, `GET .../revisions/{rev}`, `GET .../diff?from=&to=&kinds=` | |
| Tags | `GET/POST .../tags` | |
| Schema | `GET .../schema?branch=`, `GET .../schema/kinds/{kind}`, `.../relations/{rel}`, `.../traits/{t}`, `.../operations/{op}`, `.../derivations/{drv}`, `.../sources/{src}` | |
| Schema changes | `POST .../branches/{b}/schema/changes` | batch of change commands; `dry_run` supported |
| Records | `GET .../kinds/{kind}/records?where=&order_by=&limit=&cursor=`, `GET .../kinds/{kind}/records/{key}` | `where` accepts the DSL as JSON |
| Links | `GET .../kinds/{kind}/records/{key}/links/{relation}` | forward or inverse api name |
| Lineage and history | `GET .../records/{key}/lineage`, `GET .../records/{key}/history` | |
| Query | `POST .../query` | full DSL (8.2) |
| Traverse | `POST .../traverse` | |
| Neighborhood | `POST .../neighborhood` | |
| Search | `POST .../search` | `{q, kinds, where, mode, query_vector, limit}` |
| Operations | `POST .../operations/{op}/execute` | `{parameters, branch, dry_run, idempotency_key}` |
| Proposals | `GET/POST .../proposals`, `GET/PATCH .../proposals/{p}`, `GET .../proposals/{p}/diff`, `POST .../proposals/{p}/reviews`, `POST .../proposals/{p}/merge`, `POST .../proposals/{p}/close` | merge: 200 with the new target head, or 409 `merge-conflicts` carrying `attempt` |
| Proposal items | `GET .../proposals/{p}/items`, `POST .../proposals/{p}/items:decide` | bulk accept/reject |
| Conflicts | `GET .../proposals/{p}/conflicts`, `POST .../proposals/{p}/conflicts/{c}/resolution`, `POST .../proposals/{p}/conflicts:resolve` | `{c}` is the fingerprint; bulk re-runs the merge |
| Sources | `GET/POST .../sources`, `POST .../sources/{s}/runs`, `GET .../sources/{s}/runs/{run}`, `POST /sources:infer` | |
| Documents and extraction | `POST .../documents`, `GET .../documents/{doc}`, `POST .../extraction-jobs`, `GET .../extraction-jobs/{job}` | |
| Admin | `GET .../audit`, `GET/POST /principals`, `POST /api-keys`, `GET/POST .../grants` | |
| Record history and lineage | `GET .../kinds/{kind}/records/{key}/history?values=&limit=&cursor=`, `GET .../kinds/{kind}/records/{key}/lineage` | read from `mat_history`, the provenance entries and Proposals at the branch head ([agents](agents.md#history-and-lineage)) |
| Agent reads | `POST .../context`, `GET .../kinds/{kind}/records/{key}/explain` | one-call context pack within a token budget; why a Record is what it is ([agents](agents.md)) |
| Change feed | `GET .../branches/{b}/events` (Server-Sent Events), `GET .../branches/{b}/changes?since=` | Revision events; resume with `Last-Event-ID` |
| Webhooks | `GET/POST .../webhooks`, `DELETE .../webhooks/{id}` | HMAC-signed deliveries from the outbox |
| Health | `GET /healthz`, `GET /readyz` | |

**Proposal and merge shapes.** (settled in T22)

- `POST .../proposals` takes `{source_ref, target_ref, title, description}`; the merge policy
  is the Domain's `merge_policy` setting at creation. The response is the Proposal with
  `reviews`, `approval` (`protected`, `required`, `approvals`, `changes_requested`,
  `satisfied`), `checks` (`merge`: Conflict count of the latest attempt, `approvals`) and the
  latest `attempt` (the merge preview).
- `POST .../merge` runs or re-runs the merge with the stored resolutions. When it commits
  (merge Revision with parents `[target head, source head]`, fast-forward, or up to date) it
  answers 200 `{status: "merged", result, revision, proposal, attempt}`. When Conflicts remain
  it answers 409 with the problem `merge-conflicts` whose `attempt` member holds the stored
  attempt with the first page of open Conflicts. Merging into a protected branch without the
  required approvals is 409 `approvals-required`. An `Idempotency-Key` replays a committed
  merge.
- `GET .../conflicts` returns the current attempt (`id`, `status`, `result`, `base`, `ours`,
  `theirs`, counts per code, `resolutions` with `matched`, `unmatched_resolutions`,
  `auto_resolutions`) with one page of `conflicts` (filters `kind`, comma-separated codes, and
  `status` `open` or `resolved`; `next_cursor`). For an open Proposal the attempt is recomputed
  when either head moved since (`recomputed`, `stale`); resolutions whose Conflict disappeared
  stay stored and are reported as unmatched. Each Conflict is the core Conflict document
  (6.15) plus `id` (its fingerprint), `status`, `members` and the applied `resolution`.
- `POST .../conflicts/{c}/resolution` takes `{choice, value, values}`, validates it against
  the Conflict of the current attempt and stores it without running the merge;
  `POST .../conflicts:resolve` takes `{resolutions: [{fingerprint, choice, value, values}]}`,
  stores them, re-runs the merge and returns the new attempt like the listing.
- Reading a Proposal or its Conflicts needs `data.read` on both its source and target branch
  (10.2 read rules). Callers holding only Kind-narrowed grants see the Conflicts of those
  Kinds (and of Relations between them); schema Conflicts need `schema.read` on both
  branches. `GET .../proposals` keeps the Proposals the caller may read, so a page may be
  shorter than `limit` while `next_cursor` is set.

### 11.2 MCP (Herald)

Herald is a separate stateless process built on the official Python MCP SDK. It exposes tools, resources and prompts over stdio and Streamable HTTP. It calls Keeper's REST API through the typed `lorebase-client` package with the agent's credentials, so authorization, masking, audit and rate limits are enforced in one place. **[Architect call AC-20]**

Design rules for tools:
- Few, composable tools with precise descriptions; names prefixed `lorebase_` to avoid collisions in multi-server agents.
- Inputs use api names and `Kind/key` references.
- Outputs are compact JSON with stable shapes, always including `revision`, and truncation markers with `next_cursor` instead of silent truncation.
- Errors are returned as tool errors with a machine-readable `code` and a short human message.
- The Domain and default branch can be fixed by Herald configuration per deployment or passed per call.

**Tools**

| Tool | Input | Output |
|------|-------|--------|
| `lorebase_describe_domain` | `{domain?, branch?, kinds?: [str], detail?: "summary" or "full"}` | `{domain, branch, revision, kinds: [{name, description, key, attributes: [{name, type, description, required}], relations: [{name, target, cardinality, inverse}], traits}], traits: [...], operations: [{name, description}], search: {modes: [str], embedding: {model, dims} or null}}` |
| `lorebase_search` | `{query, kinds?, where?, mode?: "lexical" or "hybrid" or "semantic", query_vector?: [float], limit?: int}` | `{revision, results: [{ref, kind, title, snippet, score}], next_cursor?}` |
| `lorebase_get_record` | `{ref, select?: [str], include_links?: [str], link_limit?: int}` | `{revision, record: {ref, kind, title, attributes: {...}, derived: {...}}, links: {relation: [{ref, title}]}, truncated?: {...}}` |
| `lorebase_query` | `{kind or trait, where?, select?, include?, order_by?, limit?, cursor?}` | `{revision, rows: [...], next_cursor?}` |
| `lorebase_traverse` | `{from: ref, path: [{relation, where?, limit?}], select?}` | `{revision, records: [...], paths?: [[ref, ...]]}` |
| `lorebase_neighborhood` | `{seeds: [ref], depth: 1-3, relations?, kinds?, max_nodes?}` | `{revision, nodes: [{ref, kind, title}], edges: [{from, to, relation}], truncated}` |
| `lorebase_lineage` | `{ref, attributes?: [str]}` | `{revision, lineage: [{attribute, value, origin_type, origin, actor, at, revision}]}` |
| `lorebase_history` | `{ref, limit?}` | `{entries: [{revision, at, actor, message, changed_attributes}]}` |
| `lorebase_list_operations` | `{kind?}` | `{operations: [{name, description, parameters: [...], review_policy: "never" or "always" or "conditional"}]}` |
| `lorebase_execute_operation` | `{operation, parameters, branch?, dry_run?: bool, idempotency_key?: str}` | `{status, edits: [...], violations: [...], revision?, proposal?}` |
| `lorebase_create_branch` | `{name, from?}` | `{branch, head}` (allowed only for permitted branch patterns) |
| `lorebase_open_proposal` | `{branch, title, description}` | `{proposal, url, checks}` |
| `lorebase_diff` | `{from, to, kinds?, limit?}` | `{summary: {kind: {added, modified, removed}}, changes: [...], next_cursor?}` |

**Resources.** `lorebase://{domain}/schema`, `lorebase://{domain}/kinds/{kind}`, `lorebase://{domain}/records/{kind}/{key}`, `lorebase://{domain}/operations/{op}`. Clients that support resources can attach the schema to context once instead of calling `describe_domain`.

**Prompts.** `explore_domain` (orientation over schema), `investigate_record` (a structured walkthrough of a Record, its links and Lineage). Prompts are templates; they contain no model calls.

**Authentication.** Streamable HTTP: `Authorization: Bearer <api key>` or an OIDC access token; stdio: API key from environment. Herald forwards the credential to Keeper.

### 11.3 Clients

- `lorebase-client` (Python, typed, sync and async) built on the OpenAPI document, used by CLI and Herald.
- TypeScript client produced from OpenAPI at build time, used by the web app.

---

## 12. User interface

### 12.1 Principles

- Branch and Revision context is always visible; switching branch changes every view.
- Everything navigable has a URL (TanStack Router state in the URL): a Record, a Canvas view, a diff, a Conflict.
- The UI uses only the public REST API.
- Accessible (keyboard navigation, contrast), responsive down to tablet; Canvas is desktop-first.
- Data fetching through TanStack Query with Revision-aware cache keys.

### 12.2 Shell

- Top bar: Domain switcher, branch switcher with head Revision badge and "behind main by N", global search (Cmd/Ctrl-K), user menu.
- Left navigation: Explorer, Canvas, Modeler, Review, Sources, History, Admin.

### 12.3 Screen inventory

| Area | Screen | Content and actions |
|------|--------|---------------------|
| Explorer | Domain home | Kinds with counts and descriptions, recent Revisions, open Proposals, Source health |
| Explorer | Search results | Mixed-Kind results, facets by Kind and indexed Attributes, snippets |
| Explorer | Kind table | Columns picker, filters builder (DSL), sort, saved views, CSV export (permission-gated) |
| Explorer | Record page | Attributes with type-aware rendering, derived values marked, linked Records grouped by Relation with counts, Lineage popover per value, history timeline, "Run Operation" panel with forms built from parameters, open in Canvas |
| Explorer | Trait view | Records across implementing Kinds |
| Canvas | Graph explorer | Start from Records or search; expand neighbors by Relation; filter by Kind and Relation; ForceAtlas2 layout in a web worker; node size by degree, color by Kind; selection panel; path between two Records; caps with progressive loading |
| Canvas | Schema graph | Kinds as nodes, Relations as edges with cardinality, Traits as groups; click through to Modeler |
| Canvas | Diff overlay | Compare two refs: added, removed and modified nodes and edges colored; filter to changes only |
| Modeler | Schema overview | Kinds, Relations, Traits, Operations, Derivations, Sources lists; validation panel |
| Modeler | Kind editor | Attributes table (type, required, unique, constraints, sensitivity, indexed, searchable), primary key and title, Traits, search and embedding template, built-in Operations toggles |
| Modeler | Relation editor | Endpoints, cardinality, required, link Attributes, delete behavior |
| Modeler | Trait editor | Attributes and implementing Kinds with mappings |
| Modeler | Operation editor | Parameters, preconditions, steps, review policy, permissions; YAML and form views kept in sync; test bench with dry run against the current branch |
| Modeler | Derivation editor | Expression or aggregate definition, preview on sample Records |
| Modeler | Source mapping editor | Connection reference, inferred columns, column-to-Attribute mapping with transforms, live preview of sample rows mapped to Records, sync settings |
| Modeler | Change set summary | Pending schema changes on the branch, data migrations they imply (casts, defaults), "Open Proposal" |
| Review | Proposal list | Filters: open, assigned to me, origin (manual, Source, extraction, Operation), status of checks |
| Review | Proposal detail | Summary, checks, reviewers, schema diff, data diff by Kind with counts, Record-level side-by-side diff, comments, merge button |
| Review | Extraction items | Item list with confidence, match status and evidence; document viewer with highlighted spans; accept or reject per item, bulk by threshold or filter; ambiguous match picker |
| Review | Conflict resolver | Conflict list by code; base, ours, theirs panes; choices and custom value editor; grouped Conflicts with bulk actions; policy application; progress; re-validation status |
| Sources | Source list and detail | Status, schedule, last runs, stats, errors, mapping link, run now |
| Sources | Documents and extraction jobs | Library, upload, jobs with progress, token usage and cost estimate, link to Proposal |
| History | Log and compare | Branch log, Revision detail with diff, compare any two refs, tags |
| Admin | Members and roles | Users, groups, grants by scope |
| Admin | API keys and service principals | Create, scope, expire, revoke; last used |
| Admin | Audit log | Filterable, exportable, chain verification status |
| Admin | Domain settings | Default branch, protection rules, materialization, search language, extraction defaults |

---

## 13. Security and multi-tenancy

### 13.1 Tenancy model

One Lorebase installation serves one organization. Domains are the isolation boundary inside it: separate grants, separate object scopes, separate audit filters. Hosting multiple organizations is done with separate installations (separate databases). Shared multi-organization SaaS is a non-goal for v1. **[Architect call AC-21]**

### 13.2 Authentication

- **Users:** OIDC authorization code flow with PKCE against any compliant provider. Keeper acts as a backend-for-frontend: it holds tokens server-side and issues an `HttpOnly`, `Secure`, `SameSite=Lax` session cookie (`__Host-` prefixed, an opaque id whose SHA-256 keys the `web_sessions` row) to the web app; CSRF protection on state-changing requests (a per-session token in a header, plus `Origin` and `Sec-Fetch-Site` checks). Sessions have idle and absolute timeouts, a new id at every sign-in, refresh-token checks at the provider and RP-initiated logout (`keeper.auth.sessions`, [deployment](deployment.md#web-sign-in)). A session resolves to the same principal as a bearer token of the same subject. Group claims map to Lorebase groups. The issuer and the signing key URL (configured or discovered) must use `https`, except for loopback hosts or with `keeper.auth.oidc.allow_insecure`.
- **Agents and automation:** API keys bound to a service principal, with Domain scopes, roles, expiry and an optional allowlist of client networks (`allowed_networks`, CIDR; a key used from another address is refused like an invalid one). The client address is the socket peer, or the `X-Forwarded-For` hop left of the configured `keeper.trusted_proxies` when the peer is one of them. Format `lbk_{prefix}_{secret}`; only an HMAC-SHA256 of the secret with a server-side pepper is stored. Keys are shown once.
- **Bootstrap:** the first admin key is created by `python -m lorebase_keeper bootstrap` with access to the database; it refuses when an installation administrator already holds a usable key (unless `--force`); no default credentials exist.

### 13.3 Authorization enforcement

- Enforced in Keeper's service layer for every request (REST and, through Keeper, MCP).
- Postgres row-level security on `domain_id` using a transaction-local setting as a second line of defense against query bugs.
- The query compiler applies Kind-level read scopes and sensitivity masking; denied Kinds are invisible (not just empty) to the caller.

### 13.4 Secrets

- Configuration references secrets (`secret://env/NAME`, `secret://file/path`); secret values never enter the database, logs or traces.
- Source credentials use read-only database roles by convention, checked at registration with a warning when write privileges are detected.

### 13.5 Personal data and erasure

- Attributes tagged `pii` can use vault storage: the canonical value in the Record object is a vault reference; the value is encrypted (AES-256-GCM, key from configuration with key ids for rotation) in the `vault` table.
- Erasure (`lorebase admin erase`) deletes the vault value and marks it erased; all Revisions keep their hashes and structure, and reads return `erased`. **[Architect call AC-22]**
- Vault storage costs one extra lookup per sensitive value read; it is opt-in per Attribute.

**Implementation notes (masking and the vault).** Decisions taken where the text above leaves room; where it was silent the more restrictive option was chosen.

- **Clearances.** `pii.read`, `confidential.read` and `restricted.read` clear the matching tag; an Attribute with several tags needs all of them. Among the built-in roles only `admin` holds them; custom roles (`keeper.auth.authz.custom_roles`) grant them to others. A grant counts when it is Domain-wide or narrowed to the Kind holding the value; grants narrowed to a branch do not count (whoever creates branches could name one to match). Kind-narrowed grants are resolved to Kind ids in the schema of the default branch, so renaming Kinds on a branch does not move a clearance. Link Attributes need the clearance on both Kinds of the Relation.
- **Tags.** The tags that decide are effective tags: those of the schema read, plus those the same Attribute (by id) has on the default branch, carried through Derivations (a derived Attribute has the tags of everything it may read; `lorebase_core.expr.sensitivity`). Reading an older Revision, or a branch forked before a tag was added, therefore shows no more than the default branch would. Removing a tag, or a sensitive Attribute, Kind or Relation, on any branch (schema change or merge) needs the clearance for that tag on the Domain (403 `sensitivity_lowered`); the commit pipeline and the merge service refuse it unless Keeper checked the clearance, so no other writer (agent auto-merge, Source merges) can. Commits validate schemas and data, and the materializer, rebuild and verification compute titles and search vectors, with the default branch's tags added; the comparison of tags happens under the branch lock, against the tags the caller is cleared for. A branch forked before the default branch tagged an Attribute therefore cannot commit schema changes until its schema satisfies the rules with those tags (tag the derived Attributes, take the Attribute out of search, change the title): merge the default branch into it or tag them on the branch. A derived Attribute must carry every tag of what its Derivation may read, an Operation step value every tag of the Record Attributes its expression reads, and neither reads a vault Attribute; preconditions and the expressions that address Records (`target`, `from`, `to`) read no sensitive Attribute (their outcome, or a missing Record's key, reaches the caller); an Operation is checked again with the default branch's tags before it runs (422), and evaluation errors of expressions reading values the caller is not cleared for are reported without their message; a sensitive Attribute cannot be a primary key or a foreign key, as Record keys are shown without clearance (`schema.sensitivity_inherited`, `schema.vault_invalid`, `schema.sensitive_attribute`). A uniqueness violation on a sensitive Attribute does not name the Record holding the value.
- **Masking.** Uncleared callers get `keeper.reads.sensitivity.masked_value` (`"***"`), or no value with `omit: true` (Records only; history, lineage, diffs, Conflicts and Operation edits always show the mask). It applies to Records, Links, includes, traversals, neighborhoods, history and lineage values, diffs and changesets (JSON and NDJSON), explain and context packs (JSON and markdown), Conflicts and stored resolutions, extraction items (values the schema does not know are masked too, and evidence and match details of items about masked values are left out), and Operation edits (dry runs and rejections). Operation invocation records and Proposal descriptions keep parameter values only where they cannot hold a sensitive value, and the parameter digest is keyed with `vault.token_key` when the vault is configured; Source run errors, which quote rows, are shown only to callers cleared Domain-wide for every tag. Responses are cached per clearance and per tags of the default branch (`variant` of the cache key and ETag).
- **Search and titles.** Sensitive Attributes are left out of the full-text vectors, hence of the word lexicon, corrections and snippets, for every caller (`schema.sensitive_attribute` warning on `searchable`). A sensitive title Attribute gives Records no title (warning). On a branch forked before the default branch tagged a searchable or title Attribute, the materialized index and titles still hold its words: Keeper leaves such Kinds out of lexical search and corrections for callers not cleared for the tag, and drops their stored titles from responses and snippets; the materializer adds the default branch's tags when it next writes those Records, and verification reports titles it has not rewritten yet as `sensitivity_stale`. Semantic and hybrid search rank by the vectors of a Kind whose template embeds sensitive Attributes (`embedding_sensitive`) only for cleared callers.
- **Filters.** Filtering, ordering, grouping or `include` filters on a sensitive Attribute, or on `title` when the title Attribute is sensitive, need the clearance (403 `sensitive_attribute`); on a vault Attribute they are refused for everyone (422 `vault_attribute`), the stored values being references.
- **Vault Attributes** (`vault: true`) must be tagged `pii`, belong to a Kind, and cannot be derived, unique, indexed, a reference, the title, a primary key or the foreign key of a Relation; templates never embed them (`schema.vault_invalid`, `schema.embedding_template_invalid`). Vault storage is chosen when the Attribute is created: turning it on or off, or retyping a vault Attribute, is refused (add a new Attribute instead).
- **Tokens.** `vlt_` + 128 bits of HMAC-SHA256 under `vault.token_key` over the Domain, Kind, Record key, Attribute, a generation and the canonical value. Writing an erased value again moves to the next generation, so Revisions that held the erased token keep reading `erased`. The commit pipeline validates the plain value, replaces it by its reference before the core rules run (CEL sees references), and inserts the row in the commit's transaction. A reference in a write is accepted only when the Record holds it at the branch head or a vault row binds it to the same Kind, key and Attribute; a Conflict over vault values is resolved by picking a side, never by a custom value.
- **Values.** AES-256-GCM under `vault.encryption_key` (key id `vault.key_id`; `previous_keys` still open), with the Domain and token as associated data. Responses are masked and cached with references; references are replaced on every answer, after the cache, by the value (cleared caller), `keeper.reads.sensitivity.erased_value` (erased), or the mask (uncleared caller, unknown token, or a key not configured). A path that misses masking shows a reference, never a value.
- **Erasure.** `POST /domains/{domain}/vault/erase` and `lorebase admin erase` (`pii.erase`, Domain-wide, `admin` only among built-in roles) erase by Record key (optionally some Attributes) or by value of one Attribute in every Record of a Kind (matched through a keyed fingerprint without the Record key). The ciphertext, key id and fingerprint are cleared and `erased_at` set; the erasure and its audit entry (`vault.erase`: Kind, key or Attribute, count; never the value) commit together. Erasing again erases nothing. Erasure covers vault values only: inline sensitive values stay in Revisions.
- **Exports.** Bundles (`lorebase_store.bundle`) carry objects, hence references, and never the vault: values do not leave the installation through them, references imported elsewhere read as masked (a restore from a bundle loses vault values), and an import refuses a Record whose vault Attribute holds anything but a well-formed reference.

### 13.6 Threats and mitigations

| Threat | Mitigation |
|--------|------------|
| Agent exfiltrates restricted data | Kind scopes, sensitivity masking, rate limits, audit of all reads by service principals (configurable sampling) |
| Agent performs harmful writes | Only granted Operations, preconditions, review policies, dry run, idempotency, per-principal rate limits |
| Prompt injection via documents | Extraction model has no tools; outputs validated and evidence-checked; Proposals require review |
| SSRF via Sources or webhooks | Allowlists for hosts and networks in configuration; private ranges blocked by default for HTTP targets |
| Query abuse (expensive queries) | DSL limits (depth, caps), statement timeouts, per-principal concurrency limits |
| Tampering with history | Content addressing, immutable Revisions, hash-chained audit log |
| Supply chain | Pinned dependencies with lockfiles, dependency review in CI, SBOM per image, signed images, minimal base images, non-root containers |
| Credential leakage | Hashed API keys, secret references, log redaction filters |
| Credential guessing, load through bad credentials | 256-bit key secrets; failed authentications limited per client address and per key prefix, refused before any lookup |

---

## 14. Observability

- **Traces.** OpenTelemetry SDK in Keeper, Scribe and Herald. Spans: HTTP request, authn, authz, query compile, SQL (SQLAlchemy and asyncpg instrumentation), commit pipeline phases (validate, hash, tree, materialize, cas), merge phases (base, diff, schema merge, data merge, checks), Operation execution, extraction steps (parse, prompt, model call, validate, resolve). Herald propagates trace context from MCP calls to Keeper.
- **Metrics.** Request latency histograms by route and status; read path latency by query shape; commit duration and changes per commit; merge duration, Conflicts by code; materialization rows written; job queue depth, age and failures; extraction tokens, cost estimate, cache hit ratio, evidence rejection rate; search latency by mode; DB pool usage.
- **Logs.** Structured JSON to stdout with `trace_id`, `span_id`, `domain`, `principal` (id only), `request_id`; redaction filters for secrets and sensitive values.
- **Health.** `/healthz` (process) and `/readyz` (database reachable, migrations at expected version).
- **Dashboards.** Example Grafana dashboards and alert rules shipped in `deploy/observability`.
- OTLP endpoint and sampling are configuration; no vendor-specific exporter in code.

---

## 15. Deployment

### 15.1 Artifacts

- `ghcr.io/aantenore/lorebase`: one Python image with entrypoints `keeper`, `scribe`, `herald`, `migrate`, `cli`. One codebase, one image, different commands. **[Architect call AC-23]**
- `ghcr.io/aantenore/lorebase-web`: static web app served by an unprivileged web server; can also be served by Keeper for single-container setups.
- Python packages on PyPI: `lorebase-client`, `lorebase-cli`.

### 15.2 Local and small installs

docker compose with: `postgres` (Postgres 16 with pgvector), `migrate` (one-shot Alembic upgrade), `keeper`, `scribe`, `herald`, `web`, and optional profiles: `local-models` (Ollama with the configured model pulled at start), `litellm` (LiteLLM proxy for hosted providers that are not OpenAI-compatible or for central routing), `observability` (OpenTelemetry Collector, Prometheus, Grafana, Jaeger). Configuration via `.env` and a mounted `lorebase.yaml`. The full demo runs on a laptop with `docker compose --profile local-models up` and no API keys.

### 15.3 Production

- Helm chart (M5): Keeper and Herald as Deployments with HPA, Scribe as Deployment scaled by queue depth, migrations as a pre-upgrade Job, external Postgres (managed or operator-based).
- Postgres: primary plus optional read replicas. Keeper can route reads to replicas with the `min_revision` freshness check.
- Backup: standard Postgres backup and PITR (pgBackRest, WAL-G or managed). The whole system state is in Postgres, plus the optional blob store.
- Upgrades: Alembic migrations are forward-only and online-safe (expand, migrate, contract over releases).

---

## 16. Configuration

- Layered: defaults in code (non-functional values only, such as page sizes), then `lorebase.yaml`, then environment variables (`LOREBASE__SECTION__KEY`), validated by Pydantic Settings at startup; invalid configuration fails fast with a clear message.
- No business values, endpoints, model names, limits or credentials hardcoded. Examples in `config/lorebase.example.yaml`.

```yaml
database:
  dsn: secret://env/LOREBASE_DB_DSN
  pool_size: 20
  statement_timeout_ms: 2000
auth:
  oidc:
    issuer: https://idp.example.com
    client_id: lorebase
    client_secret: secret://env/OIDC_CLIENT_SECRET
    groups_claim: groups
  api_keys:
    pepper: secret://env/LOREBASE_KEY_PEPPER
read:
  max_limit: 500
  max_via_depth: 3
  neighborhood_max_nodes: 2000
search:
  language: simple
materialization:
  max_full_branches_per_domain: 20
inference:                               # used by Scribe only
  llm:                                   # tier 2; env aliases LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
    provider: openai_compatible          # openai_compatible | fake
    base_url: http://ollama:11434/v1     # local default; or a hosted endpoint; or http://litellm:4000/v1
    model: qwen2.5:7b-instruct           # any model name the endpoint serves
    api_key: null                        # e.g. secret://env/LLM_API_KEY for hosted providers
    structured_output: json_schema       # json_schema | json_mode
    temperature: 0
    max_concurrency: 2
    timeout_s: 120
    price_per_million_tokens: {input: 0, output: 0}
    tasks: {}                            # optional per-task overrides (extraction, schema_discovery)
  embeddings:
    provider: fastembed                  # fastembed | openai_compatible | fake
    model: BAAI/bge-small-en-v1.5
    dims: 384
    base_url: null                       # for openai_compatible (Ollama or hosted)
    api_key: null
  local_extractors:                      # tier 1; check each checkpoint's weight license
    gliner: {enabled: true, model: urchade/gliner_multi-v2.1, threshold: 0.5}
    glirel: {enabled: true, model: jackboyla/glirel-large-v0, threshold: 0.5}
    spacy: {enabled: false, pipeline: en_core_web_sm}
extraction:
  default_tiers: [0, 1, 2]
  budget:
    max_tokens_per_job: 5000000
    max_cost_per_job: 0                  # 0 means no cost limit beyond tokens
telemetry:
  otlp_endpoint: http://otel-collector:4317
  service_namespace: lorebase
  sample_ratio: 0.1
webhooks:
  targets:
    risk_flag_raised:
      url: https://hooks.example.com/risk
      secret: secret://env/RISK_HOOK_SECRET
network:
  allow_private_targets: false
```

**Implemented so far.** `lorebase_platform.settings` provides the layering above (`LOREBASE_CONFIG` names the YAML file; `LOREBASE__SECTION__KEY` overrides it) and `secret://env/NAME` and `secret://file//path` references. Packages declare their own sections with `register_section`, so a section is validated only where it is used. The sections `logging`, `telemetry`, `keeper`, `inference`, `database`, `store`, `read` and `search` exist in `config/lorebase.example.yaml`; `database.statement_timeout_ms` defaults to 30000 (bulk commits) rather than the 2000 shown above, which is a read-heavy tuning. `store` holds schema-shape values read once by the initial migration (`layout.hash_partitions`, `embeddings.dims`; see [store-schema-notes.md](store-schema-notes.md)). `read` (page sizes, DSL limits, traversal caps, time travel and diff bounds, the 2000 ms statement timeout of read transactions) and `search` (ranking and fusion weights) belong to Keeper's read path (8.6); `keeper.reads` holds the response cache and the reachability bound of read authorization (`reachability_max_depth`). The sections `auth`, `materialization`, `extraction` and `webhooks` arrive with the milestones that use them. The write endpoints read `keeper.domains`, `keeper.branches` (the materialized branch cap of `materialization.max_full_branches_per_domain` lives here as `max_materialized_per_domain`), `keeper.schema_changes` and `keeper.operations` (bulk and dry-run limits, direct commits on protected branches, skipping CEL preconditions while expressions are not enabled).

---

## 17. Repository layout

```
lorebase/
  pyproject.toml              # uv workspace root, shared tooling config
  uv.lock
  packages/
    core/                     # lorebase_core: pure domain logic, no I/O
      src/lorebase_core/
        model/                # Domain, Kind, Attribute, Relation, Trait, Operation, Derivation, Source
        types/                # type system, casts, canonical value encoding
        canonical/            # RFC 8785 canonical JSON, hashing
        tree/                 # HAMT, NodeStore protocol, diff
        revision/             # Revision objects, ancestry, merge base
        merge/                # three-way merge, consistency rules, Conflicts
        validate/             # schema and data validation
        expr/                 # CEL integration and function library
        query/                # query DSL model and validation
    platform/                 # lorebase_platform: settings, logging, telemetry, secrets resolution
    store/                    # lorebase_store: SQLAlchemy models, repositories, migrations, materializer, jobs
      migrations/             # Alembic
    keeper/                   # lorebase_keeper: FastAPI app, auth, services, REST routes
    inference/                # lorebase_inference: ChatModel, EmbeddingModel, SpanExtractor; OpenAI-compatible HTTP, fastembed, GLiNER, GLiREL adapters; fake provider
    scribe/                   # lorebase_scribe: job runner, connectors, extraction, embeddings, GC
    herald/                   # lorebase_herald: MCP server
    guide/                    # lorebase_guide: optional exploration assistant and query embeddings
    client/                   # lorebase_client: typed HTTP client
    cli/                      # lorebase_cli: Typer CLI
  apps/
    web/                      # React 19, TypeScript, Vite, TanStack Query and Router, Sigma.js, Tailwind
  config/
    lorebase.example.yaml
  deploy/
    compose/                  # docker-compose.yaml, profiles
    docker/                   # Dockerfiles
    observability/            # collector config, dashboards, alerts
    helm/                     # M5
  examples/
    supply/                   # demo Domain: schema YAML, CSV and Parquet data, sample documents
  tests/
    integration/              # cross-package tests with Postgres
    perf/                     # benchmarks and data seeders
  docs/
  .github/workflows/
```

Dependency direction: `core` depends on nothing internal; `store` on `core` and `platform`; `inference` on `core` and `platform`; `keeper` and `scribe` on `store`; only `scribe` and the optional `guide` depend on `inference`; `client` on nothing internal; `cli` and `herald` on `client`; `guide` on `platform`, `inference` and `client`, and nothing depends on it. Enforced with import-linter in CI, including the contract that `keeper` and `herald` never import `inference` or `guide`.

---

## 18. Roadmap

### M1: Engine and API

Scope: canonical JSON and hashing; HAMT trees; Revisions, branches, tags; commit pipeline; full materialization; diff; three-way merge with the full Conflict taxonomy and API-level resolution; schema change sets; built-in Operations; query DSL (without `via` on Traits), record reads, links, full-text search; Lineage for Operations and imports; history; API keys and Domain-level roles; audit log; Keeper REST; CLI; docker compose; CI; inference interfaces, settings and the deterministic fake provider (no model calls yet) with the import contract that keeps Keeper model-free. Detailed plan: [plan/m1.md](plan/m1.md).

Acceptance:
- A Domain defined in YAML can be applied, filled by CLI import of 1M Records, branched, changed on two branches, merged with Conflicts resolved via API, and diffed, all via REST and CLI.
- Property tests prove: same content gives the same root hash regardless of insertion order; merge of non-overlapping changes is commutative; diff(A, B) applied to A yields B.
- `admin verify` passes after every integration test.
- Read targets of 6.14 met for key lookup, filtered list and one-hop traversal on the 1M fixture.
- `docker compose up` brings the stack up from clean in under 2 minutes; CI green on lint, types, unit and integration tests.

### M2: Structured ingestion and exploration

Scope: Scribe job runner; Postgres, CSV and Parquet Sources; schema inference; Source branches and merge policies; Lineage for syncs; OIDC login and BFF sessions; web app shell; Explorer (home, search, Kind table, Record page, history); Canvas (graph explorer, schema graph); Review basics (Proposal list and detail, schema and data diff, merge).

Acceptance:
- Days 1 and 2 of the proof scenario run end to end on the `examples/supply` dataset.
- Inference on the example ERP schema proposes the expected Kinds and Relations (golden test).
- Incremental sync of 100k changed rows completes under 2 minutes; a no-change sync writes no Revision.
- Canvas renders 5k nodes and 10k edges with smooth interaction on a reference laptop.

### M3: Governance and agents

Scope: custom Operations with CEL, review policies, outbox and webhooks; full RBAC with scopes, groups and sensitivity masking; branch protection and Proposal checks; Herald MCP server (all tools, resources, prompts); Python client; Modeler (all editors); Review conflict resolver UI; Traits in queries.

Acceptance:
- Day 5 of the proof scenario runs with an external agent connected through MCP over stdio and Streamable HTTP.
- Authorization test matrix (role by scope by endpoint) passes, including MCP paths.
- Operation execution p95 under 75 ms; MCP tool overhead over REST under 5 ms p95.
- Every Conflict code can be produced and resolved from the UI.

### M4: Documents, extraction and semantic search

Scope: documents and blob store interface; parsers; tiered extraction (tier 0 rules, tier 1 GLiNER and GLiREL, tier 2 LLM with JSON schema output) with cache, evidence verification, entity resolution, schema discovery; OpenAI-compatible chat client, fastembed and OpenAI-compatible embeddings; compose profiles `local-models` (Ollama) and `litellm`; provider compatibility matrix; extraction Proposal items and evidence Review UI; embeddings jobs and hybrid search; Derivations (expression, aggregate, path).

Acceptance:
- Day 3 of the proof scenario runs on the sample contracts; zero items without verified evidence reach a Proposal.
- Re-running an unchanged job makes zero model calls (cache).
- Extraction precision and recall measured on a labeled sample set in `examples/supply`, reported in CI as a tracked metric (no fixed threshold gate in M4).
- Hybrid search p95 under 80 ms at 1M Records with caller-supplied vectors.
- The complete 5-day scenario runs on a laptop with Postgres and Ollama, fastembed and tier 1 models on CPU, with zero API keys configured.
- Switching tier 2 to a hosted provider (directly OpenAI-compatible, and through the LiteLLM proxy) requires configuration changes only; verified against an OpenAI-compatible mock server in CI and documented per provider.
- CI runs every extraction and embedding test with the fake provider and network access disabled.

### M5: Enterprise hardening and 1.0

Scope: overlay branch materialization; vault storage and erasure; Helm chart; read replica routing; GC; performance at 10M Records; backup and restore guide; signed images and SBOM; audit chain verification; HTTP JSON Source; LinkML schema import and export; documentation site; API stability guarantees for `/api/v1`.

Acceptance:
- All targets in 6.14 met at 10M Records on reference hardware, published with the benchmark harness.
- Full proof scenario (days 1 to 5) passes as an automated end-to-end test.
- Security review checklist completed; threat model published.
- Upgrade from M4 data with zero downtime migrations.

---

## 19. Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| Custom versioning engine is hard to get right | Data loss or wrong merges | Pure `core` package with in-memory store; property-based tests (hypothesis) for canonicity, diff, merge laws; `admin verify`; conservative first release; tree format versioned |
| Postgres storage growth (objects, nodes, changesets) | Cost, vacuum pressure | Content dedup, lz4 TOAST, hash partitioning, GC, retention windows, measured in perf suite |
| Generic JSONB materialization slower than typed tables | Missed read targets | Partial expression indexes per indexed Attribute, query shapes limited by DSL, benchmarks from M1; fallback option of per-Kind typed tables behind the same materializer interface |
| Many full-materialized branches | Storage and write amplification | Cap per Domain; overlay mode in M5; agents encouraged to use dry run instead of branches |
| CEL library maturity in Python | Bugs or performance limits in Operations | Isolate behind `expr` module interface; compile and cache programs; conformance tests; alternative implementation possible without changing definitions |
| Extraction quality varies by model | Low acceptance rates, reviewer fatigue | Evidence verification, confidence thresholds, entity resolution, labeled evaluation set, cache, configurable models |
| Canvas performance on large graphs | Poor UX | Caps, progressive loading, WebGL rendering, layout in worker, aggregation by Kind |
| MCP specification changes | Herald breakage | Use official SDK, thin Herald over REST, contract tests per MCP version |
| Small local models extract less accurately than large hosted ones | Lower acceptance rates on laptop setups | Tiers 0 and 1 carry the cheap, precise work; hints shrink tier 2 prompts; evaluation set measured per model; per-task routing to a larger model when needed |
| Tier 1 model weights carry different licenses (some checkpoints are non-commercial) | License exposure for users | Default configuration references permissively licensed checkpoints only; license check documented next to each configurable model |
| OpenAI-compatible endpoints differ in JSON schema support | Extraction failures on some servers | JSON mode fallback with validation and one repair attempt; compatibility matrix; contract tests against a mock server |
| CPU-only inference is slow on large document sets | Long extraction jobs on laptops | Budgets, progress and resumable jobs; GPU or hosted endpoint by configuration |
| Scope creep toward a full data platform | Delay | Non-goals in section 2; Sources map, they do not transform |
| Name collision or trademark issue with "Lorebase" | Rename cost | Trademark and package name search before first public release |

---

## 20. Architect calls (settled decisions)

| ID | Decision | Rationale | Revisit when |
|----|----------|-----------|--------------|
| AC-01 | Schema elements identified by stable ids; api names are renamable properties | Renames merge cleanly; hashes stable | Never for v1 |
| AC-02 | Record identity is `(kind_id, key)`; natural key or minted ULID | Deterministic identity across branches and Sources | Composite identity across Kinds needed |
| AC-03 | No parallel Links for the same Relation and endpoints | Simple identity, clear cardinality | Real need for multigraph semantics |
| AC-04 | No cross-Domain Relations in v1 | Domain is the isolation and permission boundary | Strong demand with a federation design |
| AC-05 | No nested struct types; arrays of scalars only | Attribute-level merge stays meaningful | Clear demand with merge semantics defined |
| AC-06 | CEL for preconditions, steps and policies | Safe, specified, typed, portable | Library quality blocks M3 |
| AC-07 | Derived values excluded from content and merge | No derived conflicts; recompute from inputs | Expensive Derivations need caching across Revisions |
| AC-08 | Provenance attached to changes, not content | Identical values do not diff; Lineage still exact per change | Need for multi-origin per value in content |
| AC-09 | RFC 8785 canonical JSON with Lorebase value rules; integers and decimals as strings in canonical form | Deterministic hashing across languages | Hash scheme version bump |
| AC-10 | Objects scoped per Domain | Isolation, simple deletion | Storage pressure from cross-Domain duplicates |
| AC-11 | Partitioned canonical HAMT (256-way, leaf threshold 512) plus first-parent changesets | Canonical roots, cheap diff, structural sharing, O(changes) incremental work | Benchmarks show prolly trees materially better |
| AC-12 | Single writer per branch via advisory lock | Correctness and simplicity | Write throughput per branch insufficient after batching |
| AC-13 | Recursive virtual merge base for criss-cross histories | Correct three-way results | Never |
| AC-14 | No rebase; append-only history | Immutable audit trail | Never for protected branches |
| AC-15 | Materialized values keyed by Attribute id, generic JSONB tables with partial indexes | No DDL churn on schema change; rename-safe | Read targets missed; switch materializer to typed tables |
| AC-16 | Full materialization in M1 to M4; overlay mode in M5; cap per Domain | Ship correctness first | Branch count grows faster than expected |
| AC-17 | No model of any kind in the read path; semantic search uses caller-supplied vectors; the embedding model is advertised so agents can match it | Milliseconds, determinism, zero model cost on reads | Never |
| AC-18 | Postgres-backed job queue with SKIP LOCKED | No extra infrastructure | Job throughput beyond Postgres comfort |
| AC-19 | Extraction model has no tools; outputs only as Proposal items with verified evidence | Safety against injection and invented facts | Never |
| AC-20 | Herald calls Keeper over REST | One enforcement point; Herald needs no DB credentials | MCP latency overhead exceeds budget |
| AC-21 | One organization per installation; Domains as isolation inside | Simpler security model | Managed multi-tenant offering |
| AC-22 | PII vault indirection for erasure without rewriting history | Erasure compatible with immutability | Never |
| AC-23 | One backend image with multiple entrypoints | Simpler release and versioning | Image size or dependency conflicts between roles |
| AC-24 | Postgres 16 is the only required store; blob store optional with Postgres default | Operational simplicity | Document volume requires object storage by default |
| AC-25 | One OpenAI-compatible chat abstraction over HTTP (`LLM_BASE_URL`, `LLM_MODEL`, optional `LLM_API_KEY`); no vendor SDK in the core; providers without a compatible endpoint go through a LiteLLM proxy | Vendor neutrality; switching provider is configuration only | A required capability cannot be expressed through the compatible protocol |
| AC-26 | Embeddings default to fastembed in-process on CPU; Ollama or OpenAI-compatible endpoints as alternatives; model and dimension from configuration | Semantic search with no server, key or spend | Quality needs exceed small CPU models for common Domains |
| AC-27 | Tiered extraction: tier 0 deterministic, tier 1 local GLiNER and GLiREL, tier 2 any LLM with JSON schema output; all tiers produce Proposals | Cheapest sufficient technique first; reviewability unchanged | Never for the Proposal rule |
| AC-28 | Local by default: compose ships an Ollama profile; small open models are first-class; the full demo needs zero API keys | Free to try, private by default | Never |
| AC-29 | Deterministic fake provider for all model interfaces; tests and CI run without network | Reproducible, free, fast tests | Never |
| AC-30 | Read path model-free by construction: Keeper and Herald cannot import the inference package | Enforced by the build, not by convention | Never |

### Implementation status

Architect calls with code in the repository (M1 in progress). Paths are under `packages/core/src/lorebase_core/` unless noted. Calls not listed are not implemented yet, or are policies with nothing to build (AC-04, AC-14).

| ID | Status | Implemented in |
|----|--------|----------------|
| AC-01 | Implemented | `model/ids.py` (prefixed ULIDs), `model/base.py` (`id` plus renamable `api_name`); YAML rename rule in `model/yaml_schema.py` (5.13) |
| AC-02 | Implemented | `model/record.py` (`(kind, key)`, natural key or minted ULID), `validate/data_rules.py` |
| AC-03 | Implemented | `model/record.py` and `tree/root.py` (`link_key`: one Link per Relation and endpoint pair), `validate/changeset.py` |
| AC-05 | Implemented | `types/types.py` (scalar types, arrays of scalars only), `types/casts.py` |
| AC-06 | Implemented | `expr/` (compile, type check, evaluate, plan steps; 6.15); `validate_schema` checks every expression when `SchemaRules.allow_expressions` is set, which `schema.expressions_enabled` (default on) sets wherever a schema is validated; Keeper executes stored Operations (`keeper.operations.expressions_enabled`, default on); the materializer recomputes Derivations |
| AC-07 | Implemented | `model/derivation.py`; `validate/data_rules.py` rejects stored derived values; `lorebase_store.materialize.derived` recomputes them in the `derived` column of the materialized head (6.15) |
| AC-08 | Implemented in the engine | `model/lineage.py`, `revision/changeset.py` (provenance on changes, outside hashes); the merge engine carries provenance through `ProvenanceLoader` |
| AC-09 | Implemented | `canonical/jcs.py`, `canonical/hashing.py`, `types/encoding.py` (6.15) |
| AC-10 | Schema only | `objects` and `tree_nodes` are keyed per Domain in `packages/store` (revision `0001`); no repository code yet |
| AC-11 | Implemented | `tree/` (`node.py`, `hamt.py`, `diff.py`, `root.py`), `revision/changeset.py` (6.15) |
| AC-13 | Implemented | `revision/ancestry.py` (lowest common ancestors, `merge_base`), `merge/engine.py` (virtual base) |
| AC-15, AC-16 | Implemented | `lorebase_store.materialize` (full copies, rebuilds and overlay branches, revision `0010`), `lorebase_store.layout` (overlay reads) |
| AC-12, AC-18 | Schema only | Ref compare-and-swap and `SKIP LOCKED` claim contracts are documented in [store-schema-notes.md](store-schema-notes.md); no commit pipeline or job runner yet |
| AC-30 | Implemented | `.importlinter` forbids `keeper` and `herald` from importing `inference` |
| AC-25, AC-29 | Partial | `packages/inference`: interfaces, settings, registry and the deterministic fake provider; no HTTP provider yet |

Merge (6.8 to 6.10) is implemented in `merge/` on top of AC-09, AC-11 and AC-13; its decisions are in 6.15.

