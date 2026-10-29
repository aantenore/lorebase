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

### 1.4 Principles of record

A value has exactly one current home: the Record, Attribute and branch head that hold it. Copies (Source branches, extraction branches, context packs, caches, exports) are never edited in place; they are rebuilt from that home or proposed back to it. A value changes for one of two reasons only: its system of record changed it upstream (a Source sync, a new version of a document), or a person decided (an Operation, a Conflict resolution, a reconciliation decision). Lorebase never silently drops either: an upstream change that meets a person's correction becomes a Conflict, and a correction is never overwritten by a Source that did not change (9.1, AC-34). Corrections, stale signals and contradictions are recorded as reviewable objects (Conflicts, held Proposal items, reconciliation candidates) with their evidence; they are never guessed by a model and never applied by a score.

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
- Environment and release management. Versions of systems, deployments and releases are modelled as Records (a `Release` Kind, a `deployed_to` Relation), tags on Revisions or `valid_time` periods (5.4, 10.6); release tooling stays the system of record and reaches Lorebase as a Source.
- Key aliases. A Record absorbed by a duplicate merge loses its key; outside references are not redirected (10.6).
- Live federated reads. Lorebase answers from its own materialized heads; it does not query Sources or other Domains at read time (AC-04, AC-17).

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

- Every schema element (Kind, Attribute, Relation, Trait, Operation, Derivation, Source, Lens) has a **stable id** minted at creation: a prefixed ULID, for example `knd_01JB8Z...`, `att_01JB8Z...`, `rel_...`, `trt_...`, `opr_...`, `drv_...`, `src_...`, `lns_...`.
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
| settings | object | search language, materialization policy, extraction defaults (`extraction.mode`, thresholds and `source_key` prefix rules, 9.2 "Ingestion modes"), merge policies |
| stewards | list of role names | optional, in the schema's `domain` section (5.13): the roles that review this Domain's Proposals and reconciliation candidates by default (10.7) |
| examples | list of `{label, question}` | optional sample questions that clients offer as ready-made prompts (the web app's Explore and Retrieval lab); at most 20, labels unique, label up to 80 and question up to 500 characters; absent when empty |
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
| stewards | optional list of role names; overrides the Domain's `stewards` for work touching this Kind (10.7) |

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

**Reserved api names.** A Kind's Attributes cannot be named `key`, `kind`, `ref` or `revision`:
those name the Record envelope in API responses and in the query language
(`DEFAULT_RESERVED_ATTRIBUTE_NAMES` in `lorebase_core.validate.schema_rules`). Validation refuses
them with `schema.api_name_reserved` and proposes a replacement, for example ``"`key` is a
reserved name, choose another (e.g. `code`)"``; `kind` suggests `category`, `ref` `reference`,
`revision` `version`. Relation Attributes may use them. Schema inference renames colliding
columns on its own.
| authority | optional Source id that owns this Attribute's values (used by merge policies) |
| valid_time | optional boolean, absent when off: values carry a validity period on their provenance, and reconciliation tells superseded values from contradictions (10.6); refused on derived Attributes |

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

A Trait declares a set of Attributes (name, type, required). A Kind implements a Trait by mapping each Trait Attribute to one of its own Attributes with a compatible type. Queries and search may target a Trait (`Located` returns Suppliers and Plants), context packs may take Traits as seed targets (8.8), and Operations may be declared against a Trait. Traits cannot hold data themselves. A Trait change that a Kind no longer satisfies is a structural Conflict at merge time and a validation error at commit time.

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

**Built-in Operations.** For every Kind, Lorebase provides `create_{kind}`, `update_{kind}`, `delete_{kind}`, and for every Relation `link_{relation}` and `unlink_{relation}`. They can be disabled or restricted per Kind. The generic bulk built-ins are `import_records`, `import_links` and `import_batch` (upserts, committed in batches of `keeper.operations.import_batch_rows`) and `delete_records` (`kind`, `keys`, optional `skip_missing`): it deletes up to `keeper.operations.max_bulk_delete_keys` Records of one Kind whose `delete` built-in is enabled in one Revision, their Links following each Relation's `on_delete` as for `delete_{kind}`; unknown keys fail the call unless `skip_missing` reports them as warnings. A Kind-scoped grant covers `delete_records` of its Kind. UI edits and CLI imports use them. Custom Operations express business intent (`flag_supplier_risk`, `approve_contract`) and are what agents should normally be granted.

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
| local_edits | `review` (default), `keep` or `source_wins`: what an upstream change does to a value someone else set on the target since this Source last wrote it (9.1 "Corrections that stick") |

### 5.10 Record and Link

A Record's canonical content is `{kind, key, values}` where `values` maps Attribute ids to canonical values. Null or absent values are omitted. A Link's content is `{relation, from, to, values}`.

### 5.11 Lineage

Lineage answers "where did this value come from". It is attached to changes, not to content.

- A **provenance entry** describes one origin: `{id, origin_type, origin_ref, actor, at}`. Origin types: `operation` (invocation id, Operation id, principal), `source_sync` (Source id, run id, source row locator), `extraction` (job id, document id, span offsets, model id, prompt version), `merge_resolution` (Proposal id, Conflict id, resolver), `schema_migration` (cast applied by a schema change), `import` (CLI bulk import batch).
- Each change in a Revision changeset carries a map `attribute_id -> provenance_id` for the Attributes it changed.
- The materialized head keeps, per Record, the latest provenance per Attribute and the Revision of that change (`lineage_revisions`, revision `0032`, folded in the commit transaction like the provenance, and by rebuilds from the branch history, with the Revision that created the Record under `$created`). That Revision is the one on the branch's first-parent chain: for a value merged from another branch, the merge Revision, whose changeset attributes only the values the merge changes, so values it leaves keep their Revision (folds diff a merge step's values against the branch's previous value, so merges made before that restriction read the same). A fast-forward makes the source's first-parent chain the branch's, so a Record it writes takes the source row's lineage, lineage revisions and update Revision, as a rebuild folding that chain does, and the branch history gets one row per change of each fast-forwarded Revision, as a rebuild writes it, when the range is at most `store.materialize.fast_forward_history_max_revisions` (1000) Revisions of the source head's first-parent chain; a longer range, or one reached through a second parent, is recorded as one change per element at the source head, so the history endpoint and the change count of lineage show fewer changes there than after a rebuild. Rows materialized before `0032` are filled by the `store.lineage_backfill` job, queued once by a starting job worker (`"recompute": true`, or `python -m lorebase_store.jobs lineage-backfill --recompute`, also folds again the rows naming a merge Revision, and rewrites the history of earlier fast-forwards one Revision at a time within that bound, folding again the Records they updated and the lineage of the Links they wrote; a fast-forwarded Link takes the source row's lineage too); until then lineage reads the newest `read.history.lineage_scan_changes` changes of the Record for what they lack. Full per-value history is available from the branch history table.
- Provenance is **not** part of content hashes. Two Sources writing the same value produce no diff; Lineage records which change set it last. **[Architect call AC-08]**
- A provenance entry of a value of a `valid_time` Attribute may carry `valid_from` and `valid_to`. The origin type `reconciliation` (candidate id, claim id, resolver) marks a value written by a reconciliation decision (10.6).
- Lineage tells who set the **current** value. What a Source **last wrote** is kept apart, per Source, in its last-written state (`source_written`, 9.1): the two differ exactly when someone else changed a value the Source wrote. The last-written state is bookkeeping for syncs, not provenance: it is outside content and hashes (AC-08) and is never shown as the origin of a value.

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
- **Sections.** `domain` (required), then `kinds`, `relations`, `traits`, `operations`, `derivations`, `sources`, `lenses`, each optional. An unknown section or key, a duplicate key and an unknown reference are reported as problems with a path into the document (`SchemaYamlError.problems`); the loader reports everything it finds, not only the first.
- **Keys are api names.** Elements are keyed by api name (a relation by its forward name, a source by its dotted name such as `erp.vendors`) and refer to each other by api name: `primary_key`, `from_kind`, `authority`, trait `attribute_map`, Derivation and Source targets. Model fields keep their model names, except `derivation` and `trait`, which hold api names instead of the ids of `derivation_id` and `trait_id`. Defaults are omitted from dumps. Expressions are CEL source strings.
- **Ids.** Every element and Attribute has a stable `id` (5.1). On load, an explicit `id` wins; an element without one reuses the id of the element with the same api name in the optional `base` schema; otherwise a new id is minted. Dumps always write ids.
- **Rename rule.** Renaming is changing the key and keeping the `id`. A renamed key without an `id` is a new element (new id) unless the `base` schema has that api name, so keep the `id` when renaming. This is what lets a rename on one branch merge cleanly with an edit on another (AC-01).
- **Domain examples.** The optional `domain.examples` list holds sample questions about the Domain, each a `label` (the button text) and a `question`. Keeper serves them in the `domain` block of `GET /domains/{d}/schema`; clients show them only when present, so the app carries no dataset-specific questions. An empty list is the same as none and is not written. Changing them is a schema change of the Domain element, like its display name.

  ```yaml
  domain:
    id: supply
    display_name: Supply chain
    examples:
      - label: Single-source risk
        question: Which parts depend on a single supplier, and how risky is that supplier?
  ```
- **Stewards.** `domain.stewards` and `kinds.{Kind}.stewards` list role names (10.2 built-in roles or `keeper.auth.authz.custom_roles`). A name the installation does not define is a warning (`schema.steward_role_unknown`), not an error: roles live in configuration and may be added later. An empty list is the same as none and is not written (10.7).
- **Lenses.** `lenses.{name}` holds a named read preset: `description`, `kinds`, `traits`, `relations`, `depth`, `include_schema`, `include_recent_changes`, `include_lineage`, `budget_tokens`, all optional except `description`. Names are snake_case, unique among lenses; references are api names, stored as ids like every other reference, so renames do not break a lens. A lens is a schema element with a stable id (prefix `lns_`): it merges and conflicts like any other (SC1 to SC3, XS11 for a removed Kind, Trait or Relation it names), and static validation refuses unknown names (`schema.lens_reference_unknown`) and a `depth` below 0 (8.8).

  ```yaml
  domain:
    id: supply
    stewards: [supply-steward]
  kinds:
    Supplier:
      stewards: [risk-analyst]
  lenses:
    supplier_risk:
      description: A supplier with its contracts, parts and open risk flags.
      kinds: [Supplier, Contract, Part, RiskFlag]
      relations: [with_supplier, supplied_by, flags]
      depth: 1
      include_recent_changes: true
  ```
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
- both modified: attribute-level three-way merge on `values`. For each Attribute: one side changed it, take it; both changed it to the same value, take it; otherwise an attribute value Conflict. Array values are compared as whole values. Values of `text` Attributes changed differently on both sides are first merged as text (below).
- added on both sides with different content: three-way merge against an empty base; differing Attributes conflict.
- deleted on one side and modified on the other: delete/modify Conflict. While it is open, or when it is resolved for the modified version, the Record stays, and so do its Links: a Link touching it (either end) that the deleting side removed and the other side left as in the base is kept, since its removal (explicit or by `on_delete`) followed the Record's deletion. A Link both sides changed merges as any other. When the deletion wins, the Links go with the Record.

*Text merge.* Two different values of a `text` Attribute that exists in the base are merged by unit, so two Branches editing different parts of a long text no longer conflict. Other types (`string`, `uri`, `ref`, enums, numbers, `json`, arrays) are not merged this way, and neither are add/add values, which have no base. The three values are cut into paragraphs (split at blank lines) or, by default, sentences (split after `.`, `!`, `?` or `…` followed by whitespace); the whitespace between units is a unit of its own, so the units of a value concatenate to the value exactly, no whitespace is normalized and appending after the last sentence is a pure insertion. Each side is aligned with the base (`difflib.SequenceMatcher`) into changes of base units, and the two sides' changes are merged as `diff3` does: a change made on one side is taken, one made identically on both is taken once, and clashing changes make a conflicting hunk. Two changes clash when their base ranges share at least one unit, when both insert at the same place, or when one inserts at the start or end of the other's non-empty change (the order would be ambiguous). Non-empty changes that merely touch merge in base order, so rewriting two neighbouring sentences, one on each side, is a clean merge; the same rules apply to paragraphs. A clean merge needs no winner: it is recorded as an `AutoResolution` with choice `custom`, resolver `policy:text_merge` and `details` `{ours_hunks, theirs_hunks}` (the regions each side changed), the merged value is stored, and the Attribute gets a `merge_resolution` provenance entry naming `policy:text_merge`, as for any resolution that is not a side's own value. It runs after a stored resolution and before the policies of 6.10; when hunks conflict, the Conflict is the same `DV1` (same fingerprint), the policies and people decide it as before, and `details.text_hunks` lists the whole text in order as `{status: clean | conflict, base, ours, theirs, merged}`, `merged` null for conflicting hunks, so a client can show the clean parts merged and the conflicting ones side by side. Text is not merged above three caps: `text_merge_max_chars` (characters over the three values together, default 200,000, configurable), a fixed 20,000 units over the three values, and a fixed cost bound of 4,000,000 on the product of the base's units and the larger of the two sides' (the alignment is quadratic on repetitive text); the Conflict is then a plain `DV1` without `text_hunks`. A conflicting hunk is cut down to the passages that differ: text all three values share at its start and end, cut at whitespace so words are never split, moves to the clean hunks around it. Constraints (`min_length`, `max_length`, `pattern`) are checked on the merged text in step 4.
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

A `DV1` on a `text` Attribute whose sides changed the same part carries `details.text_hunks` (6.8 step 3); its fingerprint does not depend on it.

Grouped Conflicts (XS classes over many Records) are presented as one Conflict with a member list, and accept bulk resolutions.

### 6.10 Resolution flows

- **Conflict fingerprint.** Each Conflict has a fingerprint `sha256(code, partition, key, attribute, base_hash, ours_hash, theirs_hash)`. Resolutions are stored by fingerprint.
- **Interactive.** Review shows base, ours and theirs side by side; the resolver picks a choice or enters a custom value (validated against the merged schema). Grouped Conflicts offer bulk choices.
- **Policies.** A Proposal or a Source can carry merge policies applied before humans see anything:
  - `prefer: ours | theirs` per Kind or per Attribute;
  - `authority`: a ranked list of origins (Sources, principals, actors, origin types); the side whose change comes from the higher ranked origin wins per Attribute, and the Source that owns an Attribute can rank first (`schema_authority`; Source syncs with `auto_with_authority` prefer the Source's side on those Attributes). A Source with `local_edits: keep` (9.1) applies the ranking `[principal:*, operation, merge_resolution, reconciliation, source:{id}]` to its local-edit Conflicts, so the person's value wins and the decision is recorded as resolver `policy:local_edits`;
  - `newest_wins` by provenance timestamp, globally or for specified Attributes, ties decided by provenance entry id;
  - `cascade_dangling: true`;
  - `text_merge`: on by default, merges `text` values edited in different places (6.8 step 3), also settable per Kind, Relation or Attribute and by `paragraph` instead of `sentence`.
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

`GET ...?at={revision}` reads directly from the tree: key lookups are O(depth) node fetches (about 3 to 4 indexed reads, cached), list operations scan the Kind partition (every implementing Kind for a Trait). Filters, search and every other read that needs materialized rows run against a snapshot of the Revision, materialized on first use and shared by later reads (8.6); the response states that it was served from the tree.

### 6.13 Garbage collection and retention

- Objects and nodes are immutable and only become garbage when no ref, open Proposal or retained tag reaches them.
- GC is a mark-and-sweep job per Domain run by Scribe: mark from all refs, tags, open merge attempts, the Revisions the last-written state of Sources names (9.1) and Revisions younger than the retention window; sweep unmarked objects and nodes in batches, then the vectors no branch reads (9.3).
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
- Virtual base (AC-13): `merge` builds it with the same pipeline, run without an index view. In that run consistency checks and Conflict reporting are skipped, policies are not applied (the text merge is not a policy and does run, so the virtual ancestor carries merged text, as in a recursive merge), and a change that conflicts keeps the value of its own merge base, so both sides of the outer merge see it as a change. The virtual Revision is never committed; its id is `sha256("lb1\x00virtual-base\x00" || base || ours || theirs || root)` and its root lives in memory for the duration of the merge.
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
| `text_merge`, `text_merge_elements`, `text_merge_attributes` | `true` | Merge `text` values changed differently on both sides by unit (step 3); globally, per Kind or Relation id, or per Attribute id, the most specific wins. `false` leaves every such value to `DV1` |
| `text_merge_granularity` | `sentence` | `sentence` or `paragraph`: the unit text is compared and merged by |
| `text_merge_max_chars` | `200000` | Characters of the three values together above which text is not merged |
| `sample_size` | `10` | Member keys listed in a grouped Conflict |
| `atomic_properties` | `type`, `default`, `definition` | Schema properties merged as whole values instead of entry by entry |

**Text merge and policies.** The text merge is not a rule of the chain: it runs first on `text` Attribute Conflicts, after a stored resolution. When it merges cleanly no rule is tried; when hunks conflict the Conflict goes to the rules below and then to a person.

**Policy rules.** `MergePolicy.rules` gives the rules tried on one Conflict, from the most specific to the least: per Attribute `prefer`, `authority`, `newest_wins`; per Kind or Relation `prefer`; global `authority`, `newest_wins`, `prefer`. The first rule that decides resolves the Conflict, recorded as an `AutoResolution` with resolver `policy:{name}`; when none decides, the Conflict is reported. A resolution given by a person always comes first, and the virtual base run applies no policy.

- Lineage input: `MergeInput.lineage` is a `LineageLoader`, `(revision, partition, key) -> {attribute id: ChangeStamp}`, the provenance stamp of the last change of each Attribute on `ours` and `theirs`. A `ChangeStamp` holds the provenance entry id, its time and the origin labels a ranking matches: `source:{source id}` (Source syncs), `principal:{principal}` and `operation:{api name}` (Operations), `actor:{actor}` and `origin:{origin type}` (every entry). The core reads nothing else; without a loader `authority` and `newest_wins` never decide. In the store, `MergeLineage` reads the Lineage of both materialized branch heads and the provenance rows, once per merge.
- `authority`: a side whose stamp matches no label ranks below every listed origin. No decision when both sides rank the same, neither is listed, or a side has no stamp (unknown Lineage).
- `newest_wins`: compares the stamp times as instants. Equal times are decided by the greater provenance entry id (a ULID, so normally the later-created entry), which does not depend on which branch is ours; equal ids, or a side without a stamp, give no decision.
- Scope: both apply only to Attribute Conflicts (`DV1`, `DV3`); delete/modify, schema and cross-cutting Conflicts are left to `prefer`, `cascade_dangling` and people.
- Audit: the `AutoResolution` carries `details` (the matched origins, or the compared times and `tie_break`, and both provenance entry ids); it is stored with the merge attempt, and a value taken from theirs gets a `merge_resolution` provenance entry naming `policy:{name}` as resolver. A value kept from ours keeps its own Lineage, so on the next merge `newest_wins` compares the time of the merge resolution when theirs won and of the original change when ours won.

**Expressions** (`expr/`, AC-06)
- cel-python supplies the CEL grammar and parser; type checking and evaluation are Lorebase's own, over a small AST, because cel-python has no static checker, no decimal and no evaluation budget. Regular expressions run on RE2.
- Types: CEL types plus `decimal` (exact; `int` operands promote, `double` does not mix), `date`, `ref(Kind)` and `record(Kind)`. Enum Attributes are closed string types: comparing one with a string outside its values, or assigning such a literal, is a type error.
- Environments: Operation expressions see `params.<name>`, each parameter by name, `principal.id`, `principal.subjects`, `record(ref)`, `links(ref, "relation")` and, in steps, `$step[n]` (rewritten to `_step[n]` before parsing). Derivations and aggregate filters see their Record's Attributes, `key` and `links.<relation>` (one hop). A path Derivation name works wherever a Relation name does (`links()`, `links.<path>`, the `relation` of an aggregate).
- Evaluation is deterministic (the caller passes `now`) and bounded by `ExprLimits`: `keeper.operations.expression_limits` for Operations, `schema.expression_limits` for schema validation and Derivations. An absent value is `null`; ordering or arithmetic on it is an error that points to `has()`. `&&` and `||` absorb an error when the other side decides.
- Records come from a caller's `RecordResolver`; one that has not loaded a Record raises `RecordNotLoaded` and the caller loads it and evaluates again. Steps see the Records written by earlier steps.
- Switches: `schema.expressions_enabled` (default on) decides whether schemas may hold CEL; it feeds `SchemaRules.allow_expressions` everywhere a schema is validated (schema change sets, the commit pipeline, merges, schema inference). `keeper.operations.expressions_enabled` (default on) decides whether Keeper evaluates stored Operations; the engine has no I/O and every evaluation is bounded, so both default to on.
- Operations in Keeper: `record()` reads the materialized head of the branch, `links()` reads Links by endpoint (`lorebase_store.reads.links_of_record`, stored and foreign-key backed; for a path Derivation composed from its steps, `lorebase_store.materialize.links.path_links_of`; at most `store.reads.max_page_rows` per call, and for a path the first step walked is read no further than that) and the Records at their far end. A stored Operation runs with its own grant (`operation.execute_custom`, narrowed by Operation and branch, not by Kind): `record()` and `links()` are not narrowed to the Kinds the caller may read, for Relations and paths alike; what its expressions tell is bounded by the sensitivity rules instead (13.5). A precondition that is false is a 422 `precondition-failed` problem whose `issues` carry the message, the expression and its line and column; an evaluation error is located the same way. A review policy that evaluates to true is a 409 `review-required` problem pointing to Proposals; dry runs report both without failing.
- Source rows (`expr/rows.py`): the `expression` of a Source's Attribute mapping sees the columns of the row (identifiers as variables, `row["name"]` for any column), all of type `dyn`, and nothing else. `POST /sources:preview` evaluates them for the Modeler on rows the caller sends. `RowExpressions` compiles each expression once over the Source's columns (the header, or every key of the rows read; for the preview, every key of the rows sent), so a column a row lacks is null in Scribe and in the preview alike.
- Not supported: unsigned integers, message construction, three-argument `map`, time zones in timestamp accessors.

**Derived values** (`lorebase_store.materialize.derived`, AC-07)
- `expression` and `aggregate` Derivations are recomputed in the `derived` column of the materialized row (keyed by Attribute id), in the commit transaction of every commit and merge (6.6 step 6) and on a full rebuild. They never enter the tree, so they take no part in hashing, diff or merge.
- Order: a Derivation that reads a derived Attribute runs after the one computing it. `derivation_inputs` lists what a Derivation may read (its Record's derived Attributes named in the expression, and derived Attributes of Records one hop away named after a `links.<relation>` field, or the aggregate's source and filter); schema validation reports a cycle among them as `schema.derivation_cycle`.
- Incremental scope of one Derivation: Records written by the changeset, Records whose Links of a Relation it reads changed, and Records linked to written Records or to Records whose derived values changed earlier in the same commit (one hop). Through a path Derivation, a changed Link of any step marks the Records whose pairs it can change (as for materialized paths below, walking the steps back from the near Record of the change, or forward from its far Record for the inverse direction), and the path's Links are composed from its steps, materialized or not. Above `store.materialize.derivation_max_incremental_records` Records it is recomputed for its whole Kind. A schema change recomputes every Derivation and drops values of Attributes that are no longer derived.
- Evaluation: bounded by `schema.expression_limits`; `now()` is the time of the Revision that recomputes the value. A value that cannot be computed is absent. Aggregates: `count`, `sum` (0 without members), `min`, `max`, `avg` (no value without members); decimal results are rounded to the target scale.
- Reads: the read API returns derived values with the Record's values and the query DSL filters on them; a derived Attribute marked `searchable` enters the search vector. `record()` and `links()` in Operations see derived values; Operation steps never write them.
- Verification: `verify(..., derived=True)` recomputes every derived value without writing and reports differences as `derived_mismatch`.
- Preview: `DerivedValues.preview` evaluates one Derivation on a few Records without writing; `POST /derivations:preview` uses it for a Derivation that is not saved yet, after validating the candidate schema like a schema change, with the Record-read rules for Kind permissions and masking (a derived value carries the sensitivity tags of what it reads, `effective_tags`; each tag needs the caller's clearance on the Derivation's Kind and on every Kind it comes from, `tag_origins`, and a masked item reports a failure without its code). Only `expression` and `aggregate` Derivations have values to preview. `read.derivation_preview` bounds the sample.
- Switch: `store.materialize.derivations_enabled` (default on), which also covers materialized path Derivations.

**Path Derivations** (`lorebase_core.model.paths`, `lorebase_store.materialize.paths`)
- A `path` Derivation is a virtual Relation from the first step's near Kind to the last step's far Kind, named by its `api_name` and, when given, `inverse_api_name` (unique among Relation names). It has no Attributes; its Links carry no values. A path whose steps name unknown Relations or do not continue is reported by schema validation and ignored by readers.
- Semantics, the same everywhere: the distinct `(a, c)` pairs reached through one Link per step. The first step follows every Link of `a`; every later step follows at most `store.traverse.path_fanout` Links (default 100) from each intermediate Record, those with the smallest far keys. The bound depends on the intermediate Record only, so the pairs of one start Record can be recomputed alone. Paths have a fixed length: a cycle in the data ends the walk like any other step, and a path may lead back to its start (`(a, a)` is a pair, as for "co-supplied parts").
- Reads: wherever a Relation name is accepted from a Kind (Links of a Record, Links of a Relation, `via` and `count` predicates, `include`, traversal), a path name is too. Following one needs read access to every Kind it crosses, intermediate ones included (403 otherwise), since the pairs tell which Records connect through them. Link counts of a Record include paths starting or ending at its Kind; neighborhoods follow paths named in `relations` (not by default); a caller with Kind-narrowed grants gets neither for a path crossing a Kind it may not read, as for other unreadable Kinds. Operation `links()`, `links.<path>` in expressions and aggregates read path Links composed from the steps at the head.
- Not materialized (default): composed at read time from the materialized rows of the branch (overlays resolved, row-level security applies): every Link of the first step, then one lateral read per intermediate Record of at most the fan-out, distinct pairs. Both directions read the same pairs (the inverse direction filters the forward composition), so inverse reads of a long path are the costly case.
- `materialized: true`: the pairs are stored in `mat_links` with the Derivation id as `relation_id`, no values and a digest of the pair as `object_hash`; reads use them as stored Links (both indexes, incoming Links through the definer function). They are not tree objects: verification's tree comparison leaves them out and `verify(..., derived=True)` recomputes and compares them (`derived_mismatch` on the Derivation's partition).
- Maintenance, in the commit transaction after derived values: a changed Link of step `i` (for a foreign-key step, a `from` Record written or removed, before and after targets) marks its near Record (for derived values reading the inverse direction, a changed Link at an intermediate Record also marks the far keys it can push in or out of that Record's bound); walking the steps back without bound gives the start Records to recompute from scratch, at most `store.materialize.derivation_max_incremental_records`, above which the path is rewritten whole. A schema change or a rebuild rewrites every materialized path; Links of paths that are removed or no longer materialized are deleted. `mat_schema.path_fanout` records the fan-out the branch's materialized paths and derived values reading a path were computed with: a commit that finds another setting rewrites both whole instead of incrementally, and the job workers (`scribe`, `python -m lorebase_store.jobs work`) do the same for every such branch in the background when they start, next to their job loop (`run_path_fanout_sweep`): a branch that fails is logged and left to its next commit, a branch whose background copy is queued or running is left to its next commit or sweep, and a branch with nothing depending on the fan-out only records it. `store.traverse.path_fanout` must be the same in every process of a deployment: reads compose paths with the reader's value and commits recompute with the writer's. Writes go through the repository, so overlays copy and tombstone path Links like other rows.
- Time travel composes path Links from the tree (6.12); the tree never holds them.

**Not implemented yet**
- Indexes on derived Attributes (`indexed` has no effect on them).
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
  kind text not null,                   -- branch | tag | snapshot
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
  path_fanout int,                     -- fan-out path Links were computed with (5.8)
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
  lineage_revisions jsonb,             -- {attribute_id | '$created': revision hex} (0032)
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
  blob_ref text, text_hash bytea, metadata jsonb, created_at, created_by,
  source_key text, version int, replaces text, status /* active | superseded | retired | purged */,
  retired_at, retired_by, purged_at, purged_by);                            -- 9.2 document versions
create table source_written (domain_id, source_id, target_branch, element /* kind_id | relation_id */,
  key text, written jsonb /* {attribute_id: value_hash} */, revision_id bytea,
  value_revisions jsonb /* {attribute_id: revision}, values revision_id lacks */, deleted bool,
  run_id text, updated_at);                                                 -- 9.1 last-written state
create table source_written_pending (domain_id, proposal_id, element, key, source_id, target_branch,
  written jsonb, revision_id bytea, value_revisions jsonb, deleted bool,
  preserve jsonb /* attributes whose merge base keeps the last-written value */,
  run_id text, updated_at);                                                 -- 9.1, carried by a Proposal
create table document_chunks (domain_id, document_id, seq int, start_offset int, end_offset int,
  text text, chunk_hash bytea);
create table extraction_jobs (domain_id, id, documents jsonb, schema_hash bytea, model text,
  prompt_version text, status, budget jsonb, usage jsonb, proposal_id text, created_at);
create table extraction_cache (cache_key bytea primary key, model text, output jsonb, usage jsonb, created_at);
create table vault (domain_id, id, kind_id, attribute_id, record_key, fingerprint bytea,
  value_ciphertext bytea, key_id text, created_at, erased_at);
create table reconciliation_candidates (domain_id, id, branch, type, status, fingerprint bytea,
  kind_id, keys jsonb, attribute_id, claims jsonb, signals jsonb, decision jsonb, ...);   -- 10.6
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
- `neighborhood`: from seed Records, breadth-first up to depth `d` with node and edge caps, used by Canvas and by agents that need local context. Implemented as one recursive CTE (`lorebase_store.traverse`, 8.7): the per-Record fan-out cap is a `LIMIT` inside the lateral step, and the Link cap is a `LIMIT` on the lazily consumed walk, so caps bound the work per hop without a round trip per hop. Materialized path Derivations are walked as stored Relations; a walk following a path composed at read time runs hop by hop (one statement of depth 1 per hop for the other Relations, the path's Links of the frontier composed from its steps) under the same caps.

### 8.4 Search

- **Lexical.** `tsvector` per Record built from title (weight A) and searchable Attributes (configured weights) using the Domain's text search configuration. Ranking by `ts_rank_cd`.
- **Typo tolerance (word lexicon).** Each branch keeps, per Kind, the distinct words of its own search vectors and how many of its rows hold each (`mat_lexicon`, revision `0018`), the approach of the pg_trgm documentation for unique words. A query word that the searched Kinds of the branch do not hold (nor its base, for an overlay), not negated, at least `search.typo_min_length` characters long and without digits, is corrected to its `search.typo_candidates` closest lexicon words at least `search.trigram_threshold` similar by trigrams; the search then runs with `(word | correction ...)` in place of the word (`ts_rewrite`, so phrases, `or` and negation keep their meaning). Hits of the query as written rank first; hits found only through a correction follow, at `search.trigram_weight` times their rank. The response lists the corrections applied (`corrections`). Words are lexemes of the text search configuration (lowercased, stemmed where the configuration stems), so corrections are reported in that form. Trigram matching runs on words, never on whole titles: a common word in long titles no longer turns into a large lossy bitmap (see 8.7).
- **Partial matching.** A query matches every term first (`websearch_to_tsquery`: words and phrases joined by AND). Descriptive queries, the kind agents send, rarely have every word in one Record, so when that finds fewer than `search.partial.min_results` Records (1 by default), Records holding some of the terms follow: the query's words and quoted phrases joined by OR, each alternative keeping the query's negations, stop words of the text search configuration dropped as in the original. At most `search.partial.candidates` of them, the best by `ts_rank_cd` of that OR query, are scored `weight * (rank + term_bonus * terms held)`, a term counted when the Record matches it alone (corrections applied per term), and ranked after every full match; full matches keep their order and score. Each hit reports `match`: `all` or `partial` (absent for hits found by vector only). In hybrid search the lexical list carries partial matches into the fusion the same way, so a descriptive query no longer leaves hybrid equal to semantic.
- **Semantic (optional).** Embeddings are computed offline by Scribe (9.3; default fastembed, in-process on CPU) from a per-Kind template over the Record content, keyed by `object_hash` and the template's fingerprint so all branches and Revisions sharing the same content under the same template share the vector. Vectors live in pgvector with HNSW indexes per configured model; the index dimension comes from configuration.
- **Query vectors.** Semantic and hybrid search take a `query_vector` supplied by the caller. Keeper and Herald never call a model, not even an embedding model. `describe_domain` and `GET /schema` advertise the embedding model name and dimension so agents compute compatible vectors themselves (for example with the same fastembed model, locally and for free). Without a vector, search runs in lexical mode. **[Architect call AC-17]**
- **Where query vectors come from (implemented).** The caller embeds its query with the model Scribe uses, on its own side. `lorebase_inference` is the only package that calls models, and the import contracts allow it in Scribe only among the services: Keeper and Herald must not import it, and the CLI and the Python client depend on nothing internal. So the query side is: `lorebase-inference embed TEXT` prints `{model, dims, vector}` from the configured `inference.embeddings` (the same settings as Scribe's, so the same model and dimension), and `lorebase search TEXT --vector-file FILE` (or `-` for standard input) sends it as `query_vector`; Python callers use `create_embedding_model(settings).embed([text])` and pass the vector to `records.search`. Herald takes `query_vector` as given and never embeds text: a Herald that turned text into vectors through an embedding sidecar would put a model call (and its latency, cost and failure modes) back in the agent read path, which AC-17 rules out. Agents that need semantic search over MCP embed on their side, as above. A Keeper with `search.semantic.model` naming a model with no stored vectors returns no semantic hits; a vector of another dimension than `store.embeddings.dims` is refused with `dimension_mismatch`. `GET /schema` (and `lorebase_schema` in Herald) advertises the model: a `search` block with `modes` and `embedding: {model, dims}`, set when a Kind of the schema has an `embedding_template`. Keeper reads the name from `search.semantic.model` and the dimension from `store.embeddings.dims`; it does not import `lorebase_inference`. Models that expect prefixes use `inference.embeddings.query_prefix` (applied by `lorebase-inference embed --query`) and `document_prefix` (applied by Scribe).
- **Hybrid.** Reciprocal rank fusion of lexical and vector candidate lists, computed in SQL, with filters applied before fusion.
- **Filtered vector search.** Filters (Domain, branch, readable Kinds, `kinds`, `traits`, `lens`, `where`) stay in the statement before its limit, so a vector list never holds a Record the caller may not read; with an approximate index they must also not shorten the list. 8.8 settles how.
- **BM25 ranking (optional).** With `search.backend: bm25` and a server that preloads ParadeDB `pg_search` (AGPL-3.0, opt-in image), lexical search without filters ranks with a BM25 index of the same search vectors (`lb_search_terms`, revision `0037`) through a top-k scan, so very common terms no longer cost their matches' rows; typo corrections, partial matching, Kind filters and masking are unchanged, scores are relative to the best hit. Without the extension or a valid index search keeps `ts_rank_cd` and `/readyz` says so. Details, the licence note and the cross-Domain statistics caveat: [search](search.md#bm25-ranking).
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
- **Authorization** runs after the read is resolved, against what is read (`services/reads.py`, shared by the record, query, search, Revision, log, diff, schema and branch reads): a branch head needs the permission covering that branch; a branch named in `at` or as a diff side needs it on that branch; a Revision named by id or tag needs a grant not narrowed to branches (Domain-wide, or narrowed to the Kind read), or a grant on some branch from whose head the Revision is reachable within `keeper.reads.reachability_max_depth` parent hops. Unknown names answer 403 to callers whose grants are narrowed. A grant narrowed to a Kind reads that Kind on every branch and Revision and nothing else: every Kind a request touches (target, `via`/`count` predicates, includes, the far side of Links, traversal hops) must be granted or the request gets 403; link counts, neighborhoods and searches without explicit Kinds leave other Kinds out (and path Derivations crossing one), and cached responses are keyed by the readable Kinds. With `keeper.auth.authz.invisible_kinds` (the default) those other Kinds are invisible rather than refused (13.3 implementation notes): the caller reads a projection of the schema, and every name it may not read answers like an unknown one. History reads (log, Revisions, diff) need a grant not narrowed to a Kind.
- **Caching.** GET reads answer with `ETag` = the Revision served (both Revisions for a diff) plus a digest of path and canonical query, `Cache-Control: private, must-revalidate`, and 304 on a matching `If-None-Match`; this replaces the per-Record tag of 8.5. Responses are cached in process behind the `ResponseCache` protocol, keyed by `(Domain, Revision, path, query)` and bounded by `keeper.reads.cache` (entries, bytes, time to live, body size, time travel entries). The lookup runs after authorization; keys hold the Revision, so commits need no invalidation. POST reads carry an `ETag` of the Revision and the body but are not cached.
- **Responses** keep canonical values (integers and decimals as strings, 6.1). Values of sensitive Attributes are masked for callers without the clearance (10.2, 13.5 implementation notes). Kinds a Kind-narrowed caller may not read are invisible to it (13.3 implementation notes).
- **Time travel.** `at` takes a Revision id, tag or branch. At the branch head the materialized rows serve the read; otherwise the tree does: key reads, Kind, Trait and Link listings ordered by key (whole partitions scanned, refused above `read.time_travel.max_scan_entries`). A Trait listing reads the partition of every implementing Kind and orders like the head (by Kind id and key, or by key and Kind id when ordered by `key`), so its cursors are the head's. Links of a path Derivation are composed from the Links of its steps at that Revision, materialized or not. Filters, ordering, includes, aggregates, Link and lineage summaries, traversal, neighborhoods, search, Record history and lineage, health reports, context packs and explanations read a snapshot of the Revision (`reading/snapshots.py`, `lorebase_store.materialize.snapshot`): a ref of kind `snapshot` whose rows `materialize_revision` writes like for a branch rebuilt at that Revision (history of its first-parent chain, lineage, derived values, path Links, search vectors with the sensitivity tags of the Revision's own schema), without per-Attribute indexes or ref log entry, so these reads run the head's statements and answer what the head answered then; Record history lists the changes up to that Revision. Requests are validated and authorized before a snapshot is built. The read takes the snapshot's shared advisory lock before building it, so nothing drops it before the read ends; the build runs on a second connection in its own transactions: one drops the oldest unheld snapshots older than `read.time_travel.snapshot_min_age_s` beyond `snapshot_max_per_domain`, the next creates the snapshot under the Domain's snapshot lock (one build per Domain at a time, counted against `snapshot_max_per_domain` with the snapshots being deleted). Bounds: `snapshot_max_entries` Records and Links and `snapshot_max_history_revisions` Revisions of history (generation + 1) are refused above (422, `time_travel`); `snapshot_builds_per_minute` and `snapshot_build_burst` per principal and process (429), `snapshot_max_concurrent_builds` per process and `snapshot_build_wait_ms` of waiting (503 with `Retry-After`, also when the Domain holds its maximum and none can go). Branch listings, bundles, embeddings, index plans and the garbage collector's branch guard ignore snapshot refs; their heads stay collection roots, and `at`, branch and tag names and sources never name one. Explanations at a Revision list the Proposals known now. Semantic search at a Revision finds only the vectors still stored for its Records' objects under the template fingerprints of its schema: garbage collection (6.13) keeps the vectors of the Records at branch heads and in the snapshots still kept, with the fingerprints of their schemas' templates, so once a snapshot is dropped the vectors only it used are deleted, and a past Revision whose content or templates no branch head or kept snapshot shares finds fewer semantic hits, or none, until something embeds that content under that template again (rebuilding the snapshot does not: embeddings ignore snapshot refs).
- **Search.** Lexical and hybrid search first ask the store for typo corrections (`search_corrections`, `lb_search_corrections`); lexical candidates then match the query or, apart, the corrected query, up to `store.reads.search_candidates` each, with an `exact` flag that orders them; when full matches are fewer than `search.partial.min_results`, partial matches follow (a second call of `search_records` with the OR query, then one primary-key read of the candidates' vectors to count the terms each holds; compiled searches do the same in one statement, the partial list guarded by the count of full matches). Without `where`, lexical search runs the store primitive `search_records` (`lb_search_bounded`, 8.7) and computes snippets for the returned page only; snippets highlight corrected words too. The number of matches it ranks comes from the word lexicon: the counts of the query's lexemes bound its matches (`a & b` the fewer, `a | b` the sum, a corrected word its replacements too); within `search.common_term_docs` every match is ranked, otherwise (every term very common) only the first `search.common_term_candidates` matches in index order are, and the response says `truncated_candidates` so that callers narrow the query. `search.common_term_docs: 0` ranks `store.reads.search_candidates` matches, as before. Semantic search ranks stored embeddings of the configured model by cosine distance to the caller's vector. Hybrid fuses both lists with `w / (rrf_k + rank)` in SQL. Search has no cursor.
- **Neighborhoods and Links.** `POST /neighborhood` runs the store primitive `lorebase_store.traverse.neighborhood` (one statement, `lb_neighborhood` under the definer setting of 8.7): nodes come nearest first, each Link once under its forward name, caps are the smaller of `read.traverse` and `store.traverse`, and `truncated` also reports a Record whose fan-out cap was reached. With `values: false` the nodes are Record headers (`kind`, `key`, `title`) and the edges Link endpoints: the same walk, a response without values for callers that need the structure only. Path Derivations named in `relations` are followed too (8.3), their Links reported under the path's forward name. The inbound side of `GET .../links/{relation}` (an inverse name) runs `links_to_record`. Comparisons on an indexed Attribute of one Kind repeat the partial index predicate and expression (`IndexSpec`), so a role not subject to RLS reads them through the index. GET reads at a branch head answer `If-None-Match` from `head_revision` before the read point is resolved.
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
| Filtered semantic search, 20 results | 200 ms |

**Primitives.** `lorebase_store.reads` and `lorebase_store.traverse` give Keeper one statement per read, each shaped for an index scan: `get_record`, `list_records` (keyset on key), `hydrate_records` (batched by `(kind, key)` arrays), `links_of_record`, `query_by_attribute`, `search_records`, `neighborhood`, `head_revision`, `head_schema`. Caps come from `store.reads` and `store.traverse`.

**Index strategy.**
- Records by key and pages: the `mat_records` primary key `(domain_id, branch, kind_id, key)`; Links: the `mat_links` primary key (outgoing) and `(domain_id, branch, relation_id, to_key)` (incoming).
- Attributes flagged `indexed` or `unique`, and `ref` Attributes backing a foreign-key Relation, get a partial expression index per Domain, branch, Kind and Attribute on the Domain's hash partition (`lorebase_store.indexes`; store schema notes). The cast follows the type (`bigint`, `numeric`, `double precision`, `boolean`, canonical text otherwise; hash for long `text`, `md5` for unique long `text`). A query matches the index only when it repeats the predicate literals and the expression, so query compilers take both from `IndexSpec` (`expression`, `value_sql`) and `predicate_sql`. Indexes are created in the commit transaction for small Kinds and queued as concurrent builds above `store.indexes.inline_max_rows`; those that would reject new rows (unique ones, casts of a retyped Attribute) are dropped before the rows are written. Index DDL needs a role that owns `mat_records`: a commit role that does not own it (the application role subject to row-level security) queues the changes for the job worker, which runs them as the maintenance role of `database.maintenance_dsn`; without one it skips index management with a warning, and `python -m lorebase_store.indexes plan` prints the DDL an administrator applies.
- Search: the composite GIN index `ix_mat_records_branch_search` on `(domain_id, branch, search)` (revision `0011`, operator classes from `btree_gin`); typo corrections read `ix_mat_lexicon_branch_word` on `(domain_id, branch, word gin_trgm_ops)` and the lexicon's primary key (revision `0018`, which drops the title trigram index of `0011`). Domain and branch are index conditions, so the bitmap holds the branch's matches only, however many branches share the hash partition. The query finds at most the candidates the store asks for (`CommonTerms` above, or `store.reads.search_candidates`) with `@@` (Kind filter inside that subquery) and ranks only those with `ts_rank_cd`, saying whether the cap was reached (revision `0035`); the bitmap of the GIN index still holds every match of the query, so a very common term costs the posting list of the term, not its matches' rows; `websearch_to_tsquery` parses agent input safely. Snippets (`ts_headline`) and hybrid fusion apply to the final page or to the bounded candidate lists, never to all matches. `VACUUM ANALYZE` (autovacuum) keeps the GIN pending list and statistics current after bulk loads.

**Neighborhood primitive.** One recursive CTE (8.3): seeds, then per hop a lateral union of stored outgoing and incoming Links and foreign-key Links (through the Records and their per-Attribute index), `LIMIT fanout` per Record, `CYCLE` on `(kind_id, key)`, the walk consumed under a `LIMIT` on Links, nodes ranked by depth up to the node cap and hydrated in the same statement, Links returned only between returned nodes. Depth, seeds, fan-out, Links and nodes are capped by `store.traverse`.

**Prepared statements and plans.** SQL text is stable per shape (literals vary only with Domain, branch, Kind and Attribute for partial-index queries), so the asyncpg driver reuses one prepared statement per connection and shape (`database.prepared_statement_cache_size`). Store tests check the custom and the generic plans of the hot paths for index scans.

**Schema and ETag.** `head_revision` reads the branch head with one primary-key lookup. Keeper derives `ETag` from it (plus the caller's mask profile) for collection, query, search and neighborhood reads, and from the Record's `object_hash` for Record reads (8.5); `If-None-Match` is answered with `304` before any other read. `head_schema` serves the compiled schema from a per-process LRU keyed by Domain and Revision id (`store.reads.schema_cache_entries`), which can never be stale. A request whose response must match the Revision it reports runs its reads in one `REPEATABLE READ` transaction.

**Row-level security.** The `domain_isolation` policies make Postgres apply the policy before any condition built from functions not marked leakproof. Text equality and ordering are leakproof, so key, page and stored-Link reads keep their index plans under a role subject to RLS. `->>`, casts and `@@` are not: under such a role, attribute queries filter the Kind's rows, search scans the Domain's partition, and inbound foreign-key Links (and the neighborhoods that follow them) filter every Record of the from Kind. With plain SQL the baseline measured search, Links of a Record and the neighborhood over budget for such a role. Options were (a) a read-only role with `BYPASSRLS` used through the store primitives, (b) `SECURITY DEFINER` functions for the affected primitives, (c) accepting the slower paths; marking the built-in functions leakproof is not an option, because casts raise errors that reveal values. **Settled: (b).** Revision `0005` adds `lb_search`, `lb_links_in`, `lb_neighborhood` and `lb_query_by_attribute`, owned by the migration role, `STABLE`, with a fixed `search_path`, taking the Domain as an argument and pinning it in every predicate; EXECUTE goes to `database.application_role` only. RLS stays forced on every table and no login role used for reads bypasses it. The store primitives call the functions when `store.reads.use_definer_functions` is true (the default) and run plain SQL otherwise; the security argument is in the store schema notes, and `docs/performance.md` has the numbers for both roles.

**Overlay branches.** Reads on an overlay union its rows with the base rows as of the fork point inside each statement (store schema notes) and keep the index plans of a full branch. Search on an overlay reads both halves through the branch-scoped indexes above and lets the planner hash the overlay's keys when the matches outnumber them (revision `0011`). The overlay neighborhood looks Links and Records up with one index scan over both branches per hop, the overlay's row of an identity winning over the base's (`lb_overlay_lookup`, revision `0012`); inbound foreign-key hops still read both halves through their per-Attribute indexes. `docs/performance.md` compares overlay reads with a copy under the same commits.

**Lexicon maintenance.** Statement-level triggers on `mat_records` (transition tables, `lb_lexicon_maintain`) apply each statement's word count changes in the writing transaction, so every path that writes rows (commits, copy-on-write into overlays, promotion, re-fork, rebuild, branch copy and deletion) keeps the lexicon exact without code of its own; rows whose search vector did not change cost an anti-join and nothing else, and rows of a branch whose ref is gone are skipped. An overlay holds the words of its own rows; corrections on it add its base's (a superset of what it reads, which can only add a candidate that matches nothing). Measured in `docs/performance.md`.

**Deferred.** Read replicas with `min_revision` (8.1); per-relation keyset pagination of Links.

### 8.8 Read presets and filtered vector search

Three read features that agents and people reach for repeatedly: Traits as search targets and context seeds, named lenses, and vector search that keeps its limit under filters. None of them calls a model or widens what a caller may read. **[Architect call AC-38]**

**Traits as targets.** `POST /search` and the `search` and `query` seeds of `POST /context` take `traits: [TraitName, ...]` beside `kinds`. A Trait expands to its implementing Kinds **the caller may read** in the schema of the branch read; `kinds` and `traits` together target the union. With `keeper.auth.authz.invisible_kinds`, a Trait none of whose implementing Kinds is readable answers like an unknown Trait (422, the same text as a name that does not exist), and implementing Kinds left out of the projection are silently not searched; this holds whatever schema the read point resolves the Trait in. Without it, implementing Kinds the caller may not read are left out, and a Trait with none left targets nothing (never every Kind). Hits keep their concrete `kind`. Masking is per Kind as for any hit. Herald: `lorebase_search` and `lorebase_context` take `traits`. CLI: `lorebase search TEXT --trait Located`.

**Lenses.** A lens (5.13) is a named, schema-validated preset of a read: which Kinds and Traits it is about, which Relations to follow, how deep, which optional sections to include, and a budget.

- **Context packs.** `POST /context {"lens": "supplier_risk", "refs": [...]}`. The lens supplies defaults; members given in the request win over the lens's (`relations`, `depth`, `include_*`, `budget_tokens`). The lens's `kinds` and `traits` restrict the neighborhood (neighbors of other Kinds and their Links are left out) and the Kinds of `search` and `query` seeds; seeds named by `refs` are kept whatever their Kind. `depth` and `budget_tokens` are still capped by `agents.context.max_depth` and `max_budget_tokens`; a lens over the caps is served at the caps and the pack says `capped: true`.
- **Search.** `POST /search {"q": ..., "lens": "supplier_risk"}` targets the lens's `kinds` and `traits` when the request names neither; a request naming them wins.
- **Discovery.** `GET /schema` (and `lorebase_schema`, `lorebase_describe_domain`) lists lenses with their description and resolved members. MCP resource `lorebase://{domain}/lenses/{lens}`.
- **Grants.** A lens is a preset, not a permission. Every Kind, Trait and Relation it names is projected like any other name: members the caller may not read are dropped from the lens as served, and a lens whose every Kind is invisible answers like an unknown lens (404 on the resource, 422 in a request). Requests through a lens are authorized and masked exactly as the same request written out.
- **Validation.** Static validation at commit time (5.13). At request time an unknown lens is 422 `lens_unknown`; a lens whose references a schema change on the branch broke cannot be committed (I1), so a lens read never meets a dangling name.

**Filtered vector search.** The semantic list (8.6 "Search") joins stored embeddings to the branch's materialized Records and filters them; the HNSW index returns its nearest neighbours first and the filters run after, so a selective filter can leave fewer than `search.candidates` rows, or none, without saying so. Settled:

1. Filters stay in the same statement and before its `LIMIT`: Domain, branch, readable Kinds and fingerprints as today, plus `kinds`, `traits`, `lens` and `where`. Nothing outside the caller's grants is ever ranked, fetched or counted (row-level security and the definer rules of 8.7 are unchanged).
2. With pgvector 0.8 or later (detected at startup and reported by `/readyz`) and `search.semantic.iterative_scan: auto` (default), the semantic statement runs under `SET LOCAL hnsw.iterative_scan = relaxed_order` and `SET LOCAL hnsw.max_scan_tuples = search.semantic.max_scan_tuples` (default 20000) inside the read transaction, in a materialized subquery that the outer query orders again by exact distance, so the relaxed order of the scan never reaches the response.
3. Without it (older pgvector, or `iterative_scan: off`), the index subquery fetches `search.candidates * search.semantic.overfetch_factor` (default 4) nearest rows before the filters, and the outer query filters and limits to `search.candidates`. The transaction raises `hnsw.ef_search` to that count (pgvector caps it at 1000), because an HNSW scan otherwise ends after 40 rows whatever its `LIMIT`.
4. When the list is still shorter than asked because the scan bound was reached, the response says `semantic_truncated: true` (Herald: `truncated`), like `truncated_candidates` for lexical search; a list that is short because fewer Records match is not flagged.

Hybrid search uses the same semantic list. Half-precision indexes (8.6) are unaffected. Performance budgets (8.7) gain a row for filtered semantic search measured with a selective `where`.

**Non-goals.** Lenses as a permission or sharing mechanism; lenses with free text or model-built selections; query-time embedding of text (AC-17); exact (non-approximate) vector search as a default.

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
1. Read source batches; apply mapping and transforms; build the **fresh** values per Record and Link.
2. Look up, for the batch keys, the Source's last-written state (`source_written`) and the current values and their lineage on the Source branch (indexed reads on the materialized head).
3. Per Attribute, compare the fresh value with the **last-written** one, not with the current one (below). Only upstream changes are written; a value someone else set and the Source did not change upstream is left as it is. The Record written is the current Record with the upstream-changed Attributes replaced, so unchanged Attributes keep the current value and produce no diff.
4. For `snapshot` mode, keys in the Source's last-written state (not marked deleted, within scope) and not seen in the run are deletions, subject to the local-edit rule below.
5. Commit in batches (`scribe.sources.commit_batch`, default 5000 changes per Revision) on the Source branch.
6. Open or update the Source's Proposal into the target branch. Local edits that meet an upstream change are Conflicts of its merge attempt (below); then apply the merge policy (`auto_when_clean`, `proposal`, `auto_with_authority`).
7. When the Proposal merges, advance the last-written state of every element it carried to the fresh values of the run, in the merge transaction, whatever the resolutions chose. A Proposal closed unmerged advances nothing: its upstream changes are raised again by the next run.
Provenance per changed Attribute: `source_sync` with Source id, run id and row locator.

**Corrections that stick.** **[Architect call AC-34]** Until now the Source branch was reset to the target head whenever no Proposal was open, and the mapper wrote every Attribute it owned; a person's correction on the target was overwritten by the next clean merge even when the upstream value had not changed, and a snapshot run could delete a Record a person had edited. The rule now is that a Source changes only what changed upstream, and an upstream change that meets someone else's value is a decision for a person.

- **Last-written state.** Per `(source, target branch, element, key)` (element: a Kind for Records, a Relation for Links of `relation_mapping`), `source_written` holds the hash of each Attribute value the Source last wrote (`written: {attribute_id: value_hash}`, canonical value hashes as in 6.1, an owned Attribute without value as `null`, never the values: sensitive and vault values are not copied), the Revision of the Source branch that last wrote the element (`revision_id`, where the value itself is read when needed; `value_revisions` names an older Revision for the values that one does not hold, the values upstream left unchanged while someone else's edit stood on the branch), whether the Source last deleted it, and the run. It is keyed by Source, so two Sources mapping the same Kind keep separate states. A run stages the state it advances to in `source_written_pending`, keyed by its Proposal, with the Attributes whose base is preserved (`preserve`); a later run of the same open Proposal compares against that pending state (what its branch holds) and adds to `preserve`.
- **Comparison per Attribute** (fresh `F`, last-written `W`, current `C` with its lineage):

| Case | Meaning | Action |
|------|---------|--------|
| `F = W` | no upstream change | nothing, whatever `C` is: a correction, an Operation or another Source's value stays |
| `F ≠ W`, `C = F` | upstream caught up with the current value | no change; state advances on merge |
| `F ≠ W`, `C` set by this Source (lineage `source_sync` of this Source) | plain upstream change | write `F` |
| `F ≠ W`, `C ≠ F`, `C` set by anyone else | **local edit** meets an upstream change | per `local_edits` (below) |

- **Local edits.** `local_edits` on the Source (5.9):
  - `review` (default): `F` is written on the Source branch and the Source Proposal's merge attempt carries a real `DV1` Conflict, built with a **preserved base**: the base value of that Attribute is the last-written `W` (read from `revision_id`), ours is `C`, theirs is `F`. The attempt uses the virtual base mechanism of AC-13 (a base Revision built for the attempt, not on any branch) with only those Attributes replaced, so every other change of the run still merges against the real merge base. The fingerprint (6.10) holds the hashes of `W`, `C` and `F`: a stored resolution re-applies while they hold, and the existing choices (ours, theirs, base, custom) and policies (6.10, the authority ranking included) decide it. `auto_when_clean` therefore no longer merges a run that meets a local edit: "clean" means no Conflict, local-edit Conflicts included.
  - `keep`: the same Conflict is raised and resolved at once by the authority ranking `[principal:*, operation, merge_resolution, reconciliation, source:{id}]` (resolver `policy:local_edits`), so the person's value wins, the resolution is recorded on the attempt and the state still advances on merge. While the Proposal stays open (a `proposal` policy, a protected target, another Conflict), a person may edit a kept value again: that makes a new Conflict (its fingerprint holds `C`) which no run meets as a local edit while upstream does not change. Each run of a `keep` Source therefore decides again the open Conflicts of the Attributes and Records the Proposal preserves, with who set the target's current value, so `auto_when_clean` does not stall on them.
  - `source_wins`: the earlier behaviour, as an explicit escape hatch: `F` is written and merges as a one-sided change, overwriting `C`. Each overwrite is still audited (below).
  - Under `auto_with_authority`, Attributes whose `authority` is this Source resolve in the Source's favor, as before: declaring an Attribute's authority is the explicit statement that the Source is its system of record. Every other Attribute follows `local_edits`.
- **Deletions.** A snapshot deletion of a Record whose current values include any set by someone other than this Source is a `DV2` delete/modify Conflict with a preserved base (base: the Record as last written, ours: the current Record, theirs: deleted). `keep` resolves it as "keep modified"; `source_wins` deletes. A kept Record keeps its Links, those of the mapped Relations and those pointing at it (6.8 step 3); when the deletion wins, its Links go with it. A Record with only this Source's values is deleted as before. Deleting a Record whose Links were added by others follows `on_delete` as for any deletion. The other way round, a Record someone else deleted stays deleted while upstream does not change it; an upstream change makes the same `DV2` (base: the Record as last written, ours: deleted, theirs: the new Record with every Link upstream holds, since the deletion took the earlier ones; `keep` keeps it deleted, and Links left dangling follow `cascade_dangling`).
- **Links.** The Links of `relation_mapping` follow the same comparison without Conflicts (a Link is there or not): the run adds the Links upstream added since the Source last wrote them and removes those upstream removed, so a Link someone removed is not added back and a Link someone added is not removed. Before any state, it adds the missing ones and removes only those whose Lineage is this Source.
- **Audit and events.** Each local edit met by a run writes one `source.local_edit_conflict` audit entry when the attempt is stored: Source, run, Proposal, Kind, key, Attribute, the origin type and actor of the current value, the `local_edits` mode and the outcome (`conflict`, `kept`, `overwritten`, `deleted`); never values. The run's stats count `local_edits` per outcome.
- **Branch handling.** The Source branch is still cut fresh from the target head when no Proposal is open, so schema and unrelated target changes are always current; the preserved base, not the branch's history, carries the Source's previous view. An open Proposal keeps its branch, and a new run updates it as before.
- **Upgrade and new mappings.** An element or Attribute with no last-written entry (every Source before this change, a newly mapped Attribute, a Source added over existing data) is seeded on first sight: when `C`'s lineage is this Source, `W := C` and the normal rules apply. Otherwise the Attribute is **compare-only** for that run: `F = C` seeds `W := F` with no change; `F ≠ C` is treated as a local edit (above). Seeding is written with the merge like any state advance, so a seeded run that is closed unmerged seeds again next time; a run that writes nothing and has no open Proposal records its state at once (its branch holds the target's values, so the state is what the target holds). Dry runs report what seeding and local edits would do (`seeded`, `local_edits`). No migration of data is needed beyond creating `source_written` and `source_written_pending`.
- **Equal values.** A current value equal to `W` counts as the Source's whatever its lineage (a resolution that took the Source's side, for example), so it is never a local edit.
- **Extraction.** Documents have no last-written state; the same principle applies through the hold: a value item whose target Attribute's current value was last set by a principal (origin `operation`, or `merge_resolution` and `reconciliation` with a `principal:` resolver) is never accepted at once, whatever its confidence or the Domain's ingestion mode: it stays `pending` with hold reason `local_edit` (counted as `held_local_edit`). A reviewer may still accept it. Agent principals count: their writes are deliberate, governed Operations.
- **Incremental Sources (implemented).** An `incremental` Source sees changed rows only, so it cannot tell a Record removed upstream from one unchanged. Each run records, in the last-written state, the run that last read each Record upstream and when (`source_written.last_seen_run`, `last_seen_at`, migration `0043`), changed or not; a state advanced by a merge counts as seen by the run that staged it, and before any sighting the time of the last state advance stands in. With the Source setting `stale_after` (ISO 8601 duration, days to seconds, default null: off; incremental Sources only, a schema validation error otherwise) the reconciliation sweep (10.6, its last phase) raises a `stale` candidate with signal `source_unseen` for each Record the Source last wrote to the branch, not deleted and still on it, not seen for longer than `stale_after`. The candidate names no Attribute and holds no claim; its fingerprint is scoped by Source and last run, so a dismissal holds while the Record stays unseen. It takes `keep_current` or a dismissal (deleting the Record is an Operation), and the sweep marks it `obsolete` when a run sees the Record again (`source_seen`), the Record is deleted (`record_deleted`) or the check stops applying (`source_unseen_off`). `scribe.reconciliation.stale.source_unseen` (default true, a Domain may override it) turns the check off. Nothing is deleted on that signal.

**Non-goals.** Merging a person's correction back to the upstream system (the Source is read-only). Field-level locks. Guessing whether an upstream change "agrees" with a correction beyond canonical equality.

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

1. **Intake.** Document stored through the blob store interface (default backend: Postgres `bytea`; optional S3-compatible backend). SHA-256 recorded; duplicates detected. An upload may name a stable identity, `source_key`; a keyed upload with new bytes is a new **version** of the document of that key (below, "Document versions and lifecycle"). Retired and purged documents are not extracted.
2. **Parse.** Parser plugins convert to text with structure (headings, pages, tables) and stable character offsets. Default parsers: PDF via `pypdf`, DOCX via `python-docx`, HTML via `beautifulsoup4`, Markdown and plain text natively. OCR is out of scope; scanned PDFs are flagged.
3. **Chunk.** Structure-aware chunks with overlap, each with `(start_offset, end_offset, chunk_hash)`.
4. **Tier 0 and tier 1.** Rule extractors and local span models run per chunk and emit candidates with exact offsets (evidence is the span itself).
5. **Tier 2 prompt.** For each chunk, the prompt includes the target Domain schema subset (Kinds, Attributes, Relations with descriptions) as a JSON Schema for structured output, the tier 0 and 1 hints, and instructions to return only facts present in the text with verbatim evidence quotes. The model has no tools.
6. **Tier 2 model call.** Through the `ChatModel` interface (9.4) with `response_format` of type `json_schema`. If the endpoint lacks JSON schema support, Lorebase falls back to JSON mode plus schema validation and at most one bounded repair attempt; outputs that still fail are discarded and counted.
7. **Cache.** Output cached by `sha256(tier, extractor, model, model_digest, prompt_version, schema_hash, chunk_hash)`. Re-runs on unchanged inputs are free and reproducible.
8. **Validate.** JSON Schema validation, type casting, constraint checks. **Evidence verification**: each extracted value must cite a quote that appears in the chunk (normalized whitespace match); items without verifiable evidence are discarded and counted.
9. **Resolve entities.** Match candidates to existing Records: exact key, normalized title equality, trigram similarity, optional embedding similarity, plus Attribute agreement scoring. Outcomes: `matched` (update proposal), `new` (create proposal), `ambiguous` (top candidates kept for the reviewer). Thresholds configurable per Kind.
10. **Schema discovery (optional mode).** When the model reports facts that do not fit the schema, they are aggregated into schema suggestions (new Attribute, new Kind, new Relation) with frequency and examples.
11. **Propose.** Changes are committed to branch `extract/{job_id}` and a Proposal is opened with one **Proposal item** per proposed Record, Attribute value, Link or schema suggestion, each with evidence (document, page, offsets, quote), confidence and match details. Which items are accepted at once, and whether the Proposal may merge without a person, follows the job's ingestion mode (below); value items over a person's value are held (`local_edit`, 9.1).
12. **Review.** Reviewers accept or reject items individually, in bulk, or by threshold. Rejecting an item commits a revert change on the extraction branch. Merging follows the normal merge algorithm; Conflicts with concurrent changes are resolved as usual.

**Budgets and safety.** Per-job limits on tokens, cost estimate, documents and concurrency; jobs stop cleanly when a limit is reached. Budgets are expressed in tokens and in cost; prices per token come from configuration (zero for local models). Prompt injection inside documents can only influence proposed values, which are validated, evidence-checked and reviewed; it cannot trigger actions because the model has no tools and no write access. **[Architect call AC-19]**

**Ingestion modes.** **[Architect call AC-36]** How much of an extraction a person sees is a Domain choice, within bounds the installation sets. `extraction.mode`:

| Mode | Accepted at once on `extract/{job}` | Proposal merge |
|------|-------------------------------------|----------------|
| `review_all` | nothing (as an `auto_accept_threshold` above 1) | by a person |
| `doubts_only` | complete items at or above the effective `auto_accept_threshold`, not ambiguous and not held (the current rule) | by a person |
| `direct` | as `doubts_only` | by Scribe, automatically, when the conditions below hold; otherwise by a person |

- **Default.** Without a Domain setting the mode is `scribe.extraction.mode` (default `doubts_only`) with the installation's `scribe.extraction.auto_accept_threshold` (0.9) and `min_confidence` (0.3): exactly today's behaviour. The installation's mode may be `review_all` or `doubts_only`; `direct` is refused there at startup, so it is never a default.
- **Domain setting.** `settings.extraction` overrides an allowlist of members: `mode`, `auto_accept_threshold`, `min_confidence` and `rules`. **Tightening** is always allowed: a stricter mode (`review_all` > `doubts_only` > `direct`), a higher `auto_accept_threshold`, a lower `min_confidence` (fewer candidates dropped unseen). **Loosening** below the installation's values applies only to members listed in `scribe.extraction.domain_may_loosen` (default `[mode]`; `[]` forbids loosening altogether); a loosening value for an unlisted member is ignored on its own and reported by `GET /domains/{d}` as `ignored_settings`, as for other allowlisted settings. Changing `settings.extraction` needs `domain.settings` and is audited (`domain.update` with the before and after values of the member).
- **Rules by `source_key` prefix.** `settings.extraction.rules: [{source_key_prefix, mode?, auto_accept_threshold?, min_confidence?}]` sets the members for keyed documents whose key starts with the prefix; the longest prefix wins, members a rule leaves out come from the Domain, keyless documents use the Domain's. Rule values obey the same tightening and loosening rules. A job over several documents runs at the strictest effective mode and the highest effective threshold among them.
- **Tier cap (`direct`).** Only deterministic output may merge without a person: a job whose Proposal holds any item produced by tier 1 or tier 2 (accepted, pending or rejected) is capped at `doubts_only`, and its result says `mode_capped: tier`. Tier 0 rules read the document literally and cannot be steered by its prose, so AC-19 keeps its meaning: text a model read never reaches a branch without a person.
- **Direct merge conditions.** Scribe merges the Proposal through the normal merge (6.8, Proposal checks of 10.3) only when the effective mode is `direct` after the cap, no item is pending (held for `local_edit`, reconciliation or ambiguity, below threshold or not yet decided), the merge attempt has no Conflict, and the `reconciliation` check is not `failed`. Into a protected branch only with `scribe.extraction.direct_merge_protected` (default `true`, as `scribe.sources.auto_merge_protected` for Source merges); otherwise the Proposal waits for its approvals. Schema suggestions never merge this way (they are schema changes with their own review). The merge is audited as `proposal.merge` with `mode: direct` and the job id; a capped or blocked job records why in its result (`direct_blocked: pending_items | conflicts | reconciliation | protected | tier`).
- **Local edits.** Whatever the mode, an item over a value a person set is held (`local_edit`, 9.1).
- **Implementation notes.** The resolution lives in `lorebase_store.extraction.modes`, used by Scribe (jobs) and Keeper (`ignored_settings`). A Domain's `settings.extraction` is written with `PATCH /domains/{d}` (`{"settings": {"extraction": {...} | null}}`; `extraction` is the only member that request replaces), validated for shape (unknown keys, types, ranges, a prefix given twice: 422) and audited as `domain.update` with `settings: [{member, before, after}]`. Keeper reports `ignored_settings` against the `scribe.extraction` values of its own configuration; Scribe applies its own when it runs a job and records what it ignored in the job result. A job over several documents takes the lowest `min_confidence` among them (fewest candidates dropped unseen). An item whose tier is not recorded counts as a model's for the cap. `direct_blocked` holds the first reason that applies, checked in the order `tier`, `pending_items`, `protected`, `reconciliation`, `conflicts`; the result also carries `mode` (after the cap), `direct_merged` and `merged_revision`. The merge Scribe runs keeps the extraction branch.

**Document versions and lifecycle.** **[Architect call AC-35]**

- **Identity.** `POST /documents` takes an optional `source_key` (a path, URL or external id; at most `keeper.documents.max_source_key_chars`, default 1024; compared exactly, after trimming surrounding whitespace). Uniqueness: keyless documents keep `(domain, sha256)` as today (same bytes return the existing document; a purged row no longer counts, so the same bytes uploaded after a purge are a new document); keyed documents are unique by `(domain, source_key, sha256)` among the key's active version, that is, the bytes of the current version are unique per key and at most one version of a key is active. The rule cannot hold over every version, because an upstream revert (below) is a new version with the bytes of an older one. The same bytes under two keys are two documents sharing one blob.
- **Versions.** A keyed upload whose bytes differ from the key's current version creates a new document with `version` = previous + 1 and `replaces` = the previous version's id; the previous one becomes `superseded`. Bytes equal to the current version are a no-op that returns it (200, not 201). Bytes equal to an older version (an upstream revert) are a new version too, reusing the blob. `GET /documents/{id}/versions` lists the versions of its key, newest first; `GET /documents?source_key=` finds them. Each new version is audited (`document.version`: key, version, replaced id, sizes and hashes).
- **Evidence check of a new version.** Besides the normal extraction of the new version, a `document_version_check` job (queued by that extraction job once it finished, failed or not, so that the new version's items are known; queued at upload when the upload does not extract) looks at every value on the branch the previous version's Proposals merged into whose current provenance is `extraction` from a previous version of the key, and that the new version's own extraction does not support with the same value. For each, it checks deterministically whether the old evidence quote still appears verbatim in the new version's parsed text, with the normalized-whitespace match of evidence verification (step 8). Present: nothing changes. Absent, or the quote is no longer readable (purged): a reconciliation `stale` candidate (10.6) with signal `evidence_gone` (`{document, from_version, to_version, quote}`). The value is never retracted, changed or hidden (AC-33); a person decides. The values are found through the accepted Record and value items of the earlier versions' extraction jobs; derived, vault and key Attributes and Link values are not checked (reconciliation candidates name Record Attributes). The new version's text is the text its extraction stored, or the document parsed again. The check follows the Domain's reconciliation options (`enabled` and a third trigger, `triggers.document_versions`, default on). The job is bounded by `scribe.extraction.versions.max_values_per_job` (default 5000) and continues in a continuation job, like sweeps.
- **Retire.** `POST /documents/{id}/retirement {reason}` (permission `document.retire`, held by `maintainer` and `admin`) runs the same check with empty text, so every value whose only evidence was this document raises a `stale` candidate; when the document is the newest version of its key, the values from its earlier versions are checked too (their evidence lived on through it). A retired document is kept and readable, listed with `status: retired`, and never extracted again (409 `document-retired` on a new job). A later upload under its key starts a new active version (its `replaces` names the retired one). Retiring again changes nothing. Audited `document.retire`.
- **Purge.** `POST /documents/{id}/purge {reason, confirm: <document id>}` (permission `document.purge`, held by `admin` only among built-in roles) retires the document first if needed, then deletes its bytes (the blob when no other document shares it), parsed text, chunks and the extraction cache entries of chunk hashes no other document has (`extraction_cache` gains `chunk_hash`; answers cached before it hold none and are not reached). Provenance rows stay (document id, offsets, quote hash), as do Proposal items, whose evidence quotes are replaced by `[erased]`; lineage, explanations and candidates read `[erased]` for the quote, and so do the examples of schema suggestions. Irreversible; audited `document.purge` in the purge's transaction (never with content); purging again erases nothing.
- **Masking.** Evidence quotes in `stale` candidates and their claims follow 10.6: wrapped with the tags of a sensitive Attribute and masked by clearance, absent for `vault` Attributes. The quote of the `evidence_gone` signal is wrapped and masked the same way.
- **Storage.** `documents` gains `source_key`, `version`, `replaces`, `status` (`active`, `superseded`, `retired`, `purged`) and who retired or purged it when (7.4). Indexes: unique `(domain_id, sha256) where source_key is null and status <> 'purged'` (the current unique index, now partial), unique `(domain_id, source_key, version) where source_key is not null`, unique `(domain_id, source_key) where source_key is not null and status = 'active'` (with the no-op rule, the keyed uniqueness of `(domain, source_key, sha256)` above), and a plain index on `(domain_id, source_key, sha256)`. Existing rows read as keyless, version 1, `active`; the migration is metadata only.
- **Surface.** REST as above; Python client `client.documents.versions|retire|purge`; CLI `lorebase documents upload FILE --key KEY`, `lorebase documents versions ID`, `lorebase documents retire ID --reason`, `lorebase documents purge ID --reason --confirm ID`; the Documents screen shows versions, status and the open `stale` candidates of a document (its details carry `open_stale_candidates`, and `can_retire` and `can_purge` for the caller).

**Non-goals.** Automatic retraction of values whose evidence changed; diffing documents beyond the quote check; crawling or watching upstream locations for new versions (a Source or the caller uploads them).

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
- **Model changes.** Vectors and cursors are keyed by the model name (`fake:<model>` for the fake provider), so a new model has no cursor and its first passes embed everything; Keeper searches `search.semantic.model`, switched once the backfill is complete; `--prune` then deletes the vectors of other models. Garbage collection (6.13) deletes the vectors no branch reads, of any model: object hashes no branch holds any more, and fingerprints of templates no branch holding the content uses (kept time-travel snapshots count as branches here; semantic search at a past Revision therefore loses the vectors only a dropped snapshot used, see time travel in 8.6).
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
| `maintainer` | merge Proposals into protected branches; manage Sources and jobs; retire documents (`document.retire`) |
| `admin` | grants, API keys, Domain settings; purge documents (`document.purge`) |
| `agent-reader` | viewer for service principals, no saved views |

**Scopes.** Grants apply at `domain`, `kind` (read or write limited to some Kinds), `operation` (execute a specific Operation), or `branch` pattern (`agent/*`). Deny by default.

**Sensitivity.** Attributes tagged `pii`, `confidential` or `restricted` require a matching clearance permission; otherwise values are masked (`"***"`) or omitted per configuration, in REST, MCP, search snippets, exports and embeddings (sensitive Attributes are excluded from embedding templates unless explicitly allowed).

**Groups** map from OIDC claims (configurable claim name). Service principals for agents are created by admins and receive API keys.

### 10.3 Proposals as governance

- Proposal checks: schema validity, merge preview (Conflicts count), size, required reviewers, CI-like custom checks (CEL predicates over the diff, for example "no deletion of more than 1% of Suppliers").
- Branch protection rules for protected branches: required approvals count (`required_approvals`) and required roles (`required_roles`), set by the installation (`keeper.proposals`) and overridden per Domain by Domain settings of the same names. `required_roles: [role, ...]` (default none) needs, for each listed RBAC role, at least one counted approval from a principal holding the role through a grant covering the Domain (or a Kind the Proposal touches) who may review the Proposal on the target, as steward assignees are found (10.7); it does not raise the approvals count. A merge missing a role answers 409 `approvals-required` naming the roles (`missing_roles`), and the approvals check lists `required_roles` and `missing_roles`.
- Not done yet: allowed merge origins per protected branch (a rule on the Proposal's origin, such as only `source_sync` or `manual`), and rules that differ between two protected branches of one Domain; the rules above apply to every protected branch of the Domain.
- Self-approval is configurable and off by default for protected branches.
- `required_approvals_from_stewards` (protection rule, default 0): approvals that must come from the Proposal's assignees (10.7), counted among the approvals; the Proposal needs `max(required_approvals, required_approvals_from_stewards)` approvals in all.

### 10.4 Audit

- Every authenticated mutation, merge, grant change, key creation, export and denied attempt is written to `audit_log`.
- Entries are hash-chained (`entry_hash = sha256(prev_hash || canonical entry)`) for tamper evidence; `lorebase admin audit verify` checks the chain.
- Audit is distinct from application logs and is exportable (JSON lines, OpenTelemetry log export).

### 10.5 Side effects

Operations declare named events. Events are written to the `outbox` in the commit transaction and delivered asynchronously by Scribe to configured webhook targets (HMAC-signed payloads, retries with backoff, dead-letter state). Lorebase does not call external systems synchronously during a commit.

**Events.** A committed execution of an Operation with `side_effects: [risk_flag_raised]` writes an `operation.risk_flag_raised` event in the transaction of its last Revision, invocation record and audit entry: a failure anywhere rolls all of them back, and dry runs, refused executions and idempotent replays write none. An execution sent to review (agent writes on a task branch: their Proposal is the review) defers its events (`operation_deferred_events`, keyed by branch and the head after the execution, migration `0028`): the merge of a Proposal from that branch writes, in its transaction, those whose Revision the merged source head contains and deletes them, so each is written once; they then name the target branch, the merged head, the source branch and the Proposal. Deleting the branch unmerged drops them. Side effect names are unique snake_case names of at most 64 characters, checked by static validation (`schema.api_name_invalid`, `schema.api_name_duplicate`). The payload names the execution (Operation, invocation, principal, branch, Revisions, head), carries the parameters as the invocation records them (values that may be sensitive are `***`) and up to `keeper.operations.side_effect_max_edits` edits. Every commit also writes `revision.committed`.

**Consumers.** Each consumer of the outbox owns its delivery state: a trigger adds an `outbox_pending` row per registered consumer (`outbox_consumers`, `webhooks` by default) in the transaction writing the event, and the consumer deletes its row in the transaction that hands the event off. A position in the id sequence would skip an event whose transaction committed after later ones; pending rows cannot. An event is pruned when no consumer has it pending and it is older than `store.outbox.retention_s`, or when it is older than `store.outbox.max_age_s` whatever is pending (a consumer that stopped), each pruning pass deletes batches of `store.outbox.prune_batch` (one short transaction each) until nothing is left to prune or `store.outbox.prune_budget_s` is spent, and reports what remains in `lorebase.outbox.prune.backlog`. Deleting a consumer's row drops its backlog.

**Delivery.** The webhook worker fans events out into `webhook_deliveries` (one per subscribed webhook: exact types or `operation.*`, branches) and sends them at least once: every attempt of an event carries `Idempotency-Key: evt_<sequence>`, failures are retried with backoff and become `dead` after `webhooks.max_attempts`. Deliveries are not ordered (retries interleave with newer events); payloads carry `sequence`, the outbox id, for receivers that need an order.

**Masking.** Payloads never hold a sensitive value as such: the writer wraps each value of a tagged Attribute with its effective tags (`{"$sensitive": {"tags": [...], "value": ...}}`), a vault Attribute's value being its vault reference. The worker renders the body for each webhook when it sends it: values whose tags the webhook's `clearances` cover are shown (vault values opened then, `[erased]` once erased), others are `***`. Granting a webhook a clearance needs that clearance permission on the whole Domain; `PATCH` on the webhook replaces its clearances, needing the clearance permission only for those it adds, and is audited with the before and after sets (`webhook.update`). A wrapper without tags is never cleared, and JSON written by a caller that holds `$sensitive` or `$vault` keys is wrapped with every tag, so it cannot pose as a wrapper or a reference. Vault values are never in stored payloads; values of sensitive Attributes outside the vault are kept as committed until pruned, like the Revision objects holding them (erasure reaches vault values only, 13.5).

### 10.6 Reconciliation of Source contradictions

Merge Conflicts (6.9) answer "two branches changed the same value since their base". They do not see two Sources that disagree about one fact: a Source sync, or an accepted extraction item, that replaces a value set by another Source is a one-sided change, merges cleanly, and the newest write wins unnoticed. The reconciliation queue makes these disagreements visible. It is not a Conflict class: it has no merge attempt, it can concern values already on a protected branch, it compares any number of claims, and a candidate lives until a person decides, whatever happens to the Proposal that raised it. **[Architect call AC-31]**

Nothing in it is decided by the system. Every candidate opens as `open` ("to review"); no value is changed, invalidated or hidden because a candidate exists. **[Architect call AC-33]**

**Claims and candidates.**

- A **claim** is one value for one `(kind, key, attribute)` and where it comes from: the provenance entry (5.11) when the value is or was on a branch, or the Proposal item with its evidence (document, offsets, quote) when it is only proposed. Its **source identity** follows from the origin: `source:{id}` for syncs, `document:{id}` for extraction, `principal:{id}` for Operations, `import:{batch}` for imports; `merge_resolution` and `reconciliation` origins carry the identity of the claim they chose (`ReconciliationOrigin(candidate_id, claim_id?, resolver, source?)`, `source` being that identity). Provenance of a plain commit reads as `import:{ref}` or `principal:{actor}`; a claim whose identity cannot be told is skipped, never guessed.
- A **candidate** groups claims that disagree:

| Type | Raised when | Usual decision |
|------|-------------|----------------|
| `duplicate` | two Records of one Kind (different keys) probably describe the same entity | merge into one Record, or dismiss |
| `contradiction` | one Record and Attribute, at least two distinct values from different source identities, periods overlapping or unknown | pick a claim, apply a policy, custom value, or dismiss |
| `superseded` | as above on a `valid_time` Attribute, with periods that do not overlap | confirm the newer period as current (the older stays in history), or dismiss |
| `stale` | the current value is older than the Attribute's maximum age, its period has ended, its source identity has a newer claim pending, or its only evidence is gone from a new version of its document or the document was retired (`evidence_gone`, 9.2); or a Record an incremental Source owns that no run of it has read within its `stale_after` (`source_unseen`, 9.1, no Attribute and no claim) | pick the newer claim, custom value, or dismiss (`source_unseen`: keep current or dismiss) |

Statuses: `open`, `resolved`, `dismissed`, `obsolete`. Only the pipeline sets `obsolete`, and only when the disagreement no longer exists in the data: the Record was deleted, the Attribute was removed (`attribute_removed`), the claims converged, the value a claim stood for was replaced (`value_replaced`), or the Proposal holding the only competing claim was closed or its item rejected. The reason is recorded. A score never makes a candidate obsolete.

**Pipeline.** Detection runs in Scribe, the only service that may call models (AC-30). Pure functions in `lorebase_core.reconcile` (claims, source identity, normalization, classification, fingerprints) serve three triggers:

1. **Extraction** (9.2 step 11). Value items whose `current` value differs are checked while items are assembled, before the auto-accept decision. With `hold_auto_accept` (default on) an item that raises any candidate (a `superseded` one included) stays `pending` whatever its confidence.
2. **Source syncs** (9.1 step 5). Before the merge policy is applied, the changes of the Source branch are compared with the target head and with the lineage of the values they replace.
3. **Document versions** (job `document_version_check`, 9.2). A new version or a retirement of a document raises `stale` candidates for values whose evidence quote is gone; the check is a literal match, no model.
4. **Sweep** (job `reconcile_sweep`, on demand or every `sweep_interval_s`). Walks `mat_history` of a branch for values that replaced a value of another source identity, checks ages for `stale` and Records for `duplicate`, then the Records incremental Sources have not seen within their `stale_after` (`source_unseen`, 9.1). It resumes from a `(kind, key)` or `(source, key)` cursor and caps its work per job, like embedding passes (9.3).

Stages run cheapest first; each sees only what the previous ones kept.

- **a. Identity.** A claim belongs to a Record by key (AC-02) or by the entity resolution of extraction (9.2 step 9); nothing else attaches it. Duplicates come from Records equal on a unique or title Attribute once case and spacing are folded (the rule of the health report, moved from Keeper into `lorebase_store` so that Scribe can run it), titles above `duplicates.trigram_threshold` by trigram similarity (`lorebase_store.reads.similar_titles`), and extraction mentions left `ambiguous` between two existing Records. A fuzzy or semantic match only raises a `duplicate` candidate: it never attaches a claim to another Record and never merges Records.
- **b. Values.** Same Record and Attribute, different canonical values (6.1), different source identities: a candidate. Equal canonical values from several identities are agreement. Arrays compare as whole values, as in merge.
- **c. Normalization** (`string` and `text` Attributes; typed numbers, dates and durations already compare canonically). Quantities with units are read with a small table of units and factors (`normalize.units_file`; the default ships with `lorebase_core`, no new dependency): `5 km` and `5000 m` are equal and the candidate is dropped, `5 km` and `7 km` stay. Numbers within `normalize.numeric_tolerance` (relative, default 0) are equal. Values that differ only in case, spacing or punctuation are equal. Values equal once a negation word (`normalize.negation_words`, per Domain language) is removed are kept and marked `negation`, which ranks them first. Reading is bounded and linear: a value longer than `normalize.max_quantity_length` (64) or with a decimal exponent above 64 is not a quantity, and neither is one that overflows. Normalization filters and ranks; it never changes a stored value.
- **d. Semantic** (optional; off unless `semantic.enabled` and an embedding provider is configured, 9.4). Embeddings of competing `text` values give a similarity: at `semantic.equivalent_similarity` (default 0.9) and above they are marked `equivalent` and ranked last; above `semantic.equivalence_threshold` (default null: always raised) they are not raised at all. Duplicates of Kinds with an `embedding_template` compare the stored Record vectors (9.3, `lorebase_store.reads.similar_vectors`) without any model call, in the sweep only. An optional judge (`semantic.judge`, off, set by the operator only) asks the chat model whether two texts contradict, through the extraction cache and within `scribe.extraction.max_tokens_per_job` (9.2); it adds a label and a score, nothing else. Model failures give no signal and never block detection. Values of `vault` Attributes and of Attributes with any sensitivity tag (the effective tags of the branch, those carried by Derivations included) are never sent to a model: the detector withholds them, the stage checks again, and without the tags nothing is sent.

**Fingerprint and idempotency.** A candidate's fingerprint is `sha256(type, branch, kind_id, keys, attribute_id, sorted distinct value hashes)`, the pattern of the Conflict fingerprint (6.10). It hashes values, not source identities: a new source repeating a known value adds a claim to the existing candidate instead of opening another, so a dismissed contradiction stays dismissed until a new value appears. An `obsolete` candidate detected again reopens. Triggers and sweeps upsert on `(domain, branch, fingerprint)` and can be re-run at will. Claims per candidate are capped (`max_claims_per_candidate`; the newest are kept and the rest counted).

**Valid time (opt-in).** An Attribute declares `valid_time: true` (5.4). Its claims may then carry `valid_from` and `valid_to`:

- Source mappings name them per Attribute (`attribute_mapping.{attribute}.valid_from`, `valid_to`: a column or an expression);
- extraction asks for an optional period per value of such Attributes in the tier 2 JSON Schema, kept only when its quote verifies like the value's;
- the built-in `update_{kind}` takes a separate parameter `valid: {attribute api name: {from, to}}` (RFC 3339 timestamps with an offset), offered only for Kinds with `valid_time` Attributes and no Attribute named `valid`; each distinct period gets its own provenance entry.

A derived Attribute cannot declare `valid_time` (its values have no provenance of their own).

Periods live on the provenance entry, not in content. `ProvenanceEntry` gains optional `valid_from` and `valid_to`, stored in two nullable `timestamptz` columns of `provenance` (migration `0038`: a metadata-only `ALTER TABLE`, no backfill). Content, hashes, merge and materialized values are untouched (AC-08); the materialized lineage already points at the latest provenance per Attribute, so the period of a current value is one join away, and `mat_history` holds the periods of earlier values. Attributes without the flag ignore periods; existing provenance reads as "no period". `Model.content()` drops `None` fields only, so the flag is `bool | None` (absent when off), like `vault`, and existing Attributes keep their hashes. A consequence of AC-08: a Source that repeats the current value with a new period writes no change and the period is not updated; the sweep raises `stale` once the stored period has ended.

Classification: disjoint periods give `superseded` (the newer period should be current; when it is not, the candidate proposes it); overlapping or equal periods, or any claim without one, give `contradiction`. A value is never invalidated because a newer period exists: the older value stays in history and both are listed for confirmation. **[Architect call AC-32]** `stale` uses `stale.max_age` (Domain, Kind or Attribute; default null, off) on the provenance time, an ended `valid_to`, or a newer pending claim of the same source identity.

**Resolution.** A decision is the only way a candidate changes data, and it takes the normal write path.

- **Choices:** one claim; `keep_current`; a custom value (validated against the schema); or a policy over the claims: `authority` (the ranking of 6.10 over source identities, the Attribute's `authority` Source first) or `newest_wins` (by period when present, otherwise by provenance time). Both reuse `decide_authority` and `decide_newest` of `lorebase_core.merge.policies`, folded pairwise over the claims; `prefer_current` and `prefer_incoming` pick the current value or the incoming claim of two-claim candidates. A policy that cannot decide (equal ranks) leaves the candidate open.
- **Write:** the chosen value is written by the Kind's built-in `update_{kind}` through the Operation pipeline (10.1) on the candidate's branch, so authorization, validation, review policies and branch protection apply unchanged. When the branch needs review, the write goes on a fresh branch `reconcile/{candidate}-{16 hex}` cut from the candidate's branch, each write expecting the head the previous one left, and a new Proposal into the candidate's branch; the candidate stays `open`, linked to it, until it merges, and the pending decision records the branch, its base and its head. A decision never reuses a branch or Proposal that exists already (409), and a decision branch that moved before it is proposed answers 409 `reconciliation-branch-moved`. A setup that fails removes the branch and Proposal it created. While a pending decision's Proposal is open, the candidate refuses new decisions (409 `reconciliation-pending`); once that Proposal is closed unmerged, it can be decided again. A claim or policy decision whose Record no longer holds one of the candidate's claims as its current value (a newer value was written since detection) is refused with 409. The changed value gets a provenance entry of the new origin type `reconciliation` (`candidate_id`, `claim_id`, `resolver` `principal:{id}` or `policy:{name}`), which only Keeper sets, never a caller.
- **No content change:** `keep_current` and dismissal write no Revision (Revisions record content changes, AC-08). They are recorded on the candidate (`decided_by`, `decided_at`, the chosen claim and a required `reason`) and in the audit log, like every decision (`reconciliation.resolve`, `reconciliation.dismiss`). A dismissed candidate can be reopened.
- **Evidence:** each decision keeps the claims as they were when decided: value, source identity, provenance entry or Proposal item, and the evidence passage (document, offsets, quote).
- **Duplicates:** the person picks the surviving Record and, per differing Attribute, the value to keep. Keeper writes on a fresh branch `reconcile/{candidate}-{16 hex}` the update of the survivor, the Links of the absorbed Record re-created on the survivor (the explicit-Relation Links through `import_links`), and the deletion of the absorbed Record (`on_delete` as usual), then opens a Proposal into the candidate's branch: review, Conflicts with concurrent changes and merge follow the normal Proposal path. The absorbed key disappears; outside references to it are not redirected (no key aliases in v1).
- **Bulk:** one choice (a policy or a dismissal) over a filtered set of at most `keeper.reconciliation.bulk_max` candidates, each decided and audited on its own.

**Merge check.** Proposal checks (10.3) gain `reconciliation`: `off`, `warn` (default: the Proposal lists its open candidates) or `block` (a Proposal whose changes have open `contradiction` candidates cannot merge: 409 `reconciliation-open`). The mode is the stricter (`off` < `warn` < `block`) of the Domain's flat setting `reconciliation_check` and the installation's (`keeper.reconciliation.merge_check` for merges through Keeper, `scribe.reconciliation.merge_check` for the merges Scribe runs after Source syncs and extraction; default `warn`): a Domain can tighten the check, never loosen it, and its `reconciliation` override cannot change it. The check reads `passed` (no open candidate), `pending` (open ones, none blocking) or `failed` (blocking contradictions), and lists the first open candidates (`CheckOut.candidates`). A merge that commits resolves the candidates whose decision opened the Proposal.

**Storage.**

```sql
create table reconciliation_candidates (
  domain_id text, id text, branch text, type text, status text, fingerprint bytea,
  kind_id text, keys jsonb, attribute_id text,
  claims jsonb,     -- [{claim_id, value_hash, value, source, provenance_id | proposal_item, evidence, valid_from, valid_to, at}]
  signals jsonb,    -- what the stages found: unit, negation, similarity, judge, periods
  claim_count int, proposal_id text,
  decision jsonb, decided_by text, decided_at timestamptz,
  created_at timestamptz, updated_at timestamptz,
  primary key (domain_id, id), unique (domain_id, branch, fingerprint)
);
```

Indexed by `(domain_id, branch, status, type)` and by `(domain_id, branch, kind_id, keys)` for the Record page; migration `0039` adds an index on `proposal_id` (merge check) and a GIN index on `keys` (duplicates). Values of sensitive Attributes in claims are wrapped with their tags as in outbox payloads (10.5) and masked on read; vault Attributes keep the vault reference only. The table is under the Domain's row-level security like every other.

Claim evidence keeps no quote for `vault` Attributes; for sensitive Attributes the evidence and the decided values are wrapped like claim values.

**API** (Keeper, 11.1). Under `/domains/{d}`: `GET /reconciliation` (filters `branch`, `type`, `status`, `kind`, `attribute`, `source`, `assigned` (10.7) and a cursor), `GET /reconciliation/{id}`, `POST /reconciliation/{id}/resolution` (`{choice, claim_id | value | policy, reason, dry_run}`; policies `authority`, `newest_wins`, `prefer_current`, `prefer_incoming`), `POST /reconciliation/{id}/dismissal` (`{reason}`), `POST /reconciliation/{id}/reopen`, `POST /reconciliation:resolve` (a policy or a dismissal over `ids` or a `filter`) and `POST /reconciliation/sweeps` (202, queues `reconcile_sweep`). Reads need `data.read` on the branch and are scoped like Record reads: a Kind-narrowed grant sees the candidates of its Kinds only, and claim values, evidence quotes and decided values are masked by clearance. Decisions need `conflict.resolve` plus whatever the write itself needs (10.1): `operation.execute` for `update_{kind}`, and `branch.create` and `proposal.create` when the write needs review, so a plain `reviewer` can keep the current value, dismiss and reopen only. Choosing the claim of a `vault` Attribute, writing a custom value of a sensitive or `vault` Attribute and merging duplicates with such Attributes need the clearance (403 otherwise); the web app leaves masked values out of its choices. Sweeps need `job.manage`. Every decision is audited (`reconciliation.resolve`, `reconciliation.dismiss`, `reconciliation.reopen`). Herald exposes no governance tools (its tools are reads, `lorebase_execute` and branches), so none is added; the Python client (`client.reconciliation`) and the CLI (`lorebase reconcile list|show|resolve|dismiss|reopen`) follow the REST routes.

**UI** (12.3). A Reconciliation screen in Review (`/$domain/reconcile`): one queue, tabs per type, filters by status, Kind, Attribute, source and signal, counts per tab. A candidate shows every claim side by side with its source, time, period and evidence passage (the document viewer link of extraction items); `text` values reuse the text diff of the Conflict resolver. Actions: pick a claim, keep current, custom value, apply a policy, dismiss with a reason, merge duplicates (survivor and per-Attribute picker, then the Proposal), bulk policy or dismissal over a selection, queue a sweep. The Record page marks values with open candidates and the Proposal page links the candidates of its `reconciliation` check. Limits of the screen: tab counts stop at a count limit (shown as "N+"), the signal filter and the ranking (negations first, equivalents last) apply to the pages loaded, and Record marks look within the count limit.

**Configuration** (16). `scribe.reconciliation` drives detection; `keeper.reconciliation` holds page sizes, the bulk limit and the merge check. A Domain's `reconciliation` setting overrides an allowlist of members only: `enabled`, `stages` (leaving stages out, never adding one), `triggers`, `hold_auto_accept`, `normalize.numeric_tolerance` and `normalize.negation_words`, `duplicates.trigram_threshold`, `stale.max_age`, and the `semantic` thresholds once the operator turned the stage on. `units_file`, the `sweep_*` settings, `max_claims_per_candidate`, `max_pairs_per_kind`, `normalize.max_quantity_length`, `merge_check` and `semantic.judge` stay the operator's. An invalid key is ignored on its own; the others still apply.

```yaml
scribe:
  reconciliation:
    enabled: true
    stages: [identity, values, normalize, semantic]   # fixed order; leave one out to disable it
    triggers: {extraction: true, source_sync: true, document_versions: true}
    hold_auto_accept: true          # extraction items raising any candidate stay pending
    sweep_interval_s: 0             # scheduled sweep of every Domain; 0 disables
    sweep_max_changes_per_job: 50000
    max_claims_per_candidate: 20
    merge_check: warn               # merges Scribe runs; the stricter of this and reconciliation_check applies
    duplicates: {trigram_threshold: 0.8, max_pairs_per_kind: 1000}
    normalize: {units_file: null, numeric_tolerance: 0.0, max_quantity_length: 64,
                negation_words: [not, no, never, without]}
    semantic: {enabled: false, equivalence_threshold: null, equivalent_similarity: 0.9,
               duplicate_threshold: 0.92, judge: false}
    stale: {max_age: null}          # ISO 8601 duration; per Kind and Attribute in Domain settings
keeper:
  reconciliation: {default_limit: 50, max_limit: 500, bulk_max: 1000, merge_check: warn}
```

**Metrics.** `lorebase.scribe.reconciliation.candidates` by type and outcome (`raised`, `filtered`, `claim_added`, `obsolete`), sweep duration, and open candidates per Domain and type.

**Non-goals.** Automatic resolution or invalidation by any stage or score. Queries by valid time (periods classify claims; reads stay at Revisions). Key aliases after a duplicate merge. New dependencies: the unit table is in-house and similarity goes through the existing inference interfaces. Any model call in Keeper or Herald.

### 10.7 Stewards

Stewards say who looks after which part of a Domain, so Proposals and reconciliation candidates reach the people who can decide them. They reuse RBAC roles (10.2): there is no new principal, group or ownership model. **[Architect call AC-37]**

- **Declaration.** `stewards: [role, ...]` in the schema, on the Domain (`domain.stewards`) and per Kind (`kinds.{Kind}.stewards`, 5.3, 5.13). A Kind's list replaces the Domain's for that Kind; a Kind without one falls back to the Domain's. Stewards are schema, so they are versioned, branched and reviewed like the rest of it.
- **Steward roles of an object.** A Proposal: the union of the steward roles of the Kinds its diff touches (Records, and both ends of Links); schema-only Proposals use the Domain's. A reconciliation candidate: those of its Kind. Proposals record the Kinds they touch (`proposals.kinds`, refreshed with the summary) so the computation is an index lookup.
- **Assignees.** Computed when read, never stored: the principals holding one of the object's steward roles through a grant that covers the Domain or the Kind, that may read every Kind the object touches (with `keeper.auth.authz.invisible_kinds`, a principal who could not see one of them is not an assignee), and that hold the permission to act on it (`proposal.review` for Proposals, `conflict.resolve` for candidates). Group grants count through the OIDC groups claim as for any grant.
- **Filters.** `GET /proposals` and `GET /reconciliation` take `assigned=me|any|none` (`me`: the caller is an assignee; `any`: the object has steward roles; `none`: it has none). Each item carries `steward_roles` and, for callers with `principal.read`, `assignees` (principal ids, at most `keeper.stewards.max_assignees_listed`, default 50, with `assignees_truncated`). CLI: `lorebase proposal list --mine`, `lorebase reconcile list --mine`. Web: an "Assigned to me" toggle on Review and Reconciliation, and the assignees on each item.
- **Approvals.** `required_approvals_from_stewards` (protection rule, 10.3) counts approvals by assignees only. A Proposal whose Kinds declare no stewards satisfies it trivially; one that has steward roles but fewer assignees than required fails its approvals check with `stewards-unavailable`, so a missing steward is visible instead of silently skipped. Self-approval rules are unchanged.
- **No stewards, no change.** A Domain whose schema declares none behaves exactly as before; `assigned=me` returns nothing and the protection rule defaults to 0.
- **Audit.** Stewards change through schema change sets (audited as schema changes); assignment writes nothing, since it is derived.
- **Implementation notes.** `lorebase_keeper.services.stewards`. Grants narrowed to a Kind name the Kind by api name, so assignees are checked against the api names of the touched Kinds; the permission to act is checked on the target branch (Proposals) or the candidate's branch, as the routes check it, so a principal whose only grants are narrowed to Kinds is not an assignee of Proposals. A listing of assignees reads the Domain's grants (at most `keeper.stewards.max_grants_scanned`) and expands groups through stored memberships; members known only from a provider's groups claim are assignees for their own `assigned=me` but are not listed. `proposals.kinds` (migration `0042`) is refreshed by every merge attempt from the partitions that differ between the merge base and the source head (Links name both ends; a virtual base uses the merged changes and Conflicts); rows from before it are null until their next attempt, and their Kinds are computed when read (steward roles, assignees, the approval rule) from the partitions that differ between the branches' lowest common ancestors and the source head. The `assigned` filter applies to each page read, so a page may hold fewer items while `next_cursor` is set, as for the other read filters of these listings. Unknown steward roles are reported as `warnings` by `schema:validate`. The web app shows the ingestion mode and the ignored settings on the Domain settings screen read-only: it has no signal yet of whether the caller holds `domain.settings`.

**Non-goals.** Notifications and reminders (webhooks and the change feed carry events; delivery to people belongs to the tools that receive them). Workload balancing or round-robin assignment. Stewards as a permission: a steward role grants nothing beyond what its grants already give.

---

## 11. Agent interface: REST and MCP

### 11.1 REST (Keeper)

Base path `/api/v1`. JSON. OpenAPI 3.1 at `/api/v1/openapi.json`. Errors use RFC 9457 problem details. Pagination by opaque cursor. Every read accepts `branch` (default: Domain default branch) and, where supported, `at` (Revision id or tag).

| Area | Method and path | Notes |
|------|-----------------|-------|
| Domains | `GET /domains`, `POST /domains`, `GET /domains/{d}`, `PATCH /domains/{d}` | `PATCH` changes display metadata and the `extraction` setting (9.2); `GET` reports `ignored_settings` |
| Branches | `GET /domains/{d}/branches`, `POST .../branches`, `GET .../branches/{b}`, `DELETE .../branches/{b}` | branch names URL-encoded |
| Revisions | `GET .../branches/{b}/log`, `GET .../revisions/{rev}`, `GET .../diff?from=&to=&kinds=` | |
| Tags | `GET/POST .../tags` | |
| Schema | `GET .../schema?branch=`, `GET .../schema/kinds/{kind}`, `.../relations/{rel}`, `.../traits/{t}`, `.../operations/{op}`, `.../derivations/{drv}`, `.../sources/{src}` | |
| Schema changes | `POST .../branches/{b}/schema/changes` | batch of change commands; `dry_run` supported |
| Previews of unsaved edits | `POST .../derivations:preview`, `POST .../sources:preview` | read-only; a schema change set plus a Derivation or Source name; the Derivation is evaluated on sample Records (`data.read` of every Kind it touches and `schema.read`, masking as for Record reads), the Source's expression mappings on sample rows sent by the caller (`schema.read`, or a grant of it narrowed to the Source's target Kind; rows, columns and evaluations bounded by `read.source_preview_*`) |
| Records | `GET .../kinds/{kind}/records?where=&order_by=&limit=&cursor=`, `GET .../kinds/{kind}/records/{key}` | `where` accepts the DSL as JSON |
| Links | `GET .../kinds/{kind}/records/{key}/links/{relation}` | forward or inverse api name |
| Lineage and history | `GET .../records/{key}/lineage`, `GET .../records/{key}/history` | |
| Query | `POST .../query` | full DSL (8.2) |
| Traverse | `POST .../traverse` | |
| Neighborhood | `POST .../neighborhood` | |
| Search | `POST .../search` | `{q, kinds, traits, lens, where, mode, query_vector, limit}`; `semantic_truncated` when a filtered vector scan hit its bound (8.8) |
| Operations | `POST .../operations/{op}/execute` | `{parameters, branch, dry_run, idempotency_key}` |
| Proposals | `GET/POST .../proposals`, `GET/PATCH .../proposals/{p}`, `GET .../proposals/{p}/diff`, `POST .../proposals/{p}/reviews`, `POST .../proposals/{p}/merge`, `POST .../proposals/{p}/close` | merge: 200 with the new target head, or 409 `merge-conflicts` carrying `attempt` |
| Proposal items | `GET .../proposals/{p}/items`, `POST .../proposals/{p}/items:decide` | bulk accept/reject |
| Conflicts | `GET .../proposals/{p}/conflicts`, `POST .../proposals/{p}/conflicts/{c}/resolution`, `POST .../proposals/{p}/conflicts:resolve` | `{c}` is the fingerprint; bulk re-runs the merge |
| Reconciliation | `GET .../reconciliation`, `GET .../reconciliation/{id}`, `POST .../reconciliation/{id}/resolution`, `POST .../reconciliation/{id}/dismissal`, `POST .../reconciliation/{id}/reopen`, `POST .../reconciliation:resolve`, `POST .../reconciliation/sweeps` | contradictions between Sources (10.6); decisions write through Operations (on a `reconcile/{candidate}-{16 hex}` branch and a Proposal when the branch needs review); a sweep answers 202 |
| Sources | `GET/POST .../sources`, `POST .../sources/{s}/runs`, `GET .../sources/{s}/runs/{run}`, `POST /sources:infer` | |
| Documents and extraction | `POST .../documents`, `GET .../documents?source_key=`, `GET .../documents/{doc}`, `GET .../documents/{doc}/versions`, `POST .../documents/{doc}/retirement`, `POST .../documents/{doc}/purge`, `POST .../extraction-jobs`, `GET .../extraction-jobs/{job}` | upload takes `source_key`; a keyed upload with new bytes is a new version (9.2); retire needs `document.retire`, purge `document.purge` and `confirm` |
| Admin | `GET .../audit`, `GET/POST /principals`, `POST /api-keys`, `GET/POST .../grants` | |
| Record history and lineage | `GET .../kinds/{kind}/records/{key}/history?values=&limit=&cursor=`, `GET .../kinds/{kind}/records/{key}/lineage` | read from `mat_history`, the provenance entries and Proposals at the branch head; lineage in one statement, and also at `at` ([agents](agents.md#history-and-lineage)) |
| Agent reads | `POST .../context`, `GET .../kinds/{kind}/records/{key}/explain` | one-call context pack within a token budget, optionally through a `lens` and with `traits` on `search` and `query` seeds (8.8); why a Record is what it is ([agents](agents.md)) |
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
| `lorebase_search` | `{query, kinds?, traits?, lens?, where?, mode?: "lexical" or "hybrid" or "semantic", query_vector?: [float], limit?: int}` | `{revision, results: [{ref, kind, title, snippet, score}], next_cursor?}` |
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

**Resources.** `lorebase://{domain}/schema`, `lorebase://{domain}/kinds/{kind}`, `lorebase://{domain}/records/{kind}/{key}`, `lorebase://{domain}/operations/{op}`, `lorebase://{domain}/lenses/{lens}` (8.8). `lorebase_context` takes `lens` and `traits` like `POST /context`. Clients that support resources can attach the schema to context once instead of calling `describe_domain`.

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
| Review | Reconciliation | One queue with tabs for duplicates, contradictions, stale and superseded values; every claim with its source and evidence passage; pick a claim, keep current, custom value, apply a policy, dismiss with a reason, merge duplicates, bulk decisions, sweeps; Record marks and the Proposal's `reconciliation` check link here (10.6) |
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

**Implementation notes (Kind-scoped invisibility).** A caller whose grants on a Domain are narrowed to Kinds (no Domain-wide or branch grant covers the read) reads those Kinds on every branch and Revision. With `keeper.auth.authz.invisible_kinds: true` (the default) the other Kinds do not exist for it; `false` refuses them with 403 naming the Kind, as before.

- **One projection.** The read point's schema is the projection of the schema read to the caller's Kinds (`lorebase_core.expr.visibility.visible_schema`, remembered per schema object in `reading/visibility.py`). It leaves out the other Kinds; Relations with an end among them and path Derivations with a step left out; Derivations of their Attributes, aggregating over a Relation left out or reading (an expression's `links.<name>`, an aggregate's source Attribute and filter) anything left out, transitively, with the derived Attributes they compute; `ref` Attributes to them; Traits implemented by them only; Operations whose parameters, steps or expressions name anything left out; Sources writing anything left out. A Kind keyed by a reference to an invisible Kind, or whose titles, full-text index or embeddings come from an Attribute left out (a searchable derived count of Suppliers), is left out too: its keys, stored titles and search vectors would tell. The projection is conservative: whatever may tell about an invisible Kind goes.
- **Same answers as unknown names.** Every read resolves names against the projection, so a Kind, Relation, path, Attribute, Derivation, Operation or Source the caller may not read gets the 404 or 422 of a name that does not exist, word for word (record, link, history, lineage, explain and health routes, the query DSL, traversal, neighborhoods, search, context packs, schema elements, Derivation and Source previews). Nothing names a Kind in a 403. A Conflict outside the caller's Kinds answers like a Conflict that is not part of the attempt.
- **What is shown.** `GET /schema` (JSON and YAML) and schema elements return the projection; the Domain, its branches and tags are readable with such grants (`require(..., kind_limited=True)`) so that clients find their way, without the progress of background branch copies (row counts of every Kind). Record values, link summaries, neighborhoods, search hits, typo corrections and the lexicon counts behind `truncated_candidates`, health reports and context packs count and show the visible Kinds only. Derivation previews refuse a candidate reusing an id the projection left out, so stored rows of an invisible Kind cannot be read under a name the caller chose. Agent traces of other principals keep refs of the visible Kinds, without result counts, query plans or a `truncated` flag that would tell refs were left out. Proposals keep their Conflicts, counts and extraction checks to the visible Kinds, leave out Conflicts and automatic resolutions about an Attribute left out (a reference to an invisible Kind), and drop such Attributes from the value maps of Record-level Conflicts. Record history, lineage, explanations and context packs leave out provenance origins, Revision messages and Proposal titles that name an id or, as a word, the name of something left out (an Operation or Source of an invisible Kind, the Kind itself), and the changes of a visible Record that touched only Attributes left out.
- **Caching.** The cache key and `ETag` variant hold the mode and the Kinds (`KindScope.variant`), so a projected response is never served to a wider caller, nor the reverse, and a wider caller's tag does not revalidate.
- **Not covered.** Free text written by people is shown as written unless it names something left out word for word: Proposal descriptions, comments and reviews, Domain descriptions always, Revision messages and Proposal titles outside Record history, lineage, explanations and context packs. Whether a Proposal has open Conflicts (its merge check status) and the status of its merge attempt are properties of the whole Proposal. Revisions are Domain-wide: every response names the Revision read and branches their ahead and behind counts, so a caller sees that something changed, never what. History reads (log, Revisions, diffs, the change feed) stay refused to Kind-narrowed callers. Schema writes and validation need grants not narrowed to a Kind.

### 13.4 Secrets

- Configuration references secrets (`secret://env/NAME`, `secret://file/path`); secret values never enter the database, logs or traces.
- Source credentials use read-only database roles by convention, checked at registration with a warning when write privileges are detected.

### 13.5 Personal data and erasure

- Attributes tagged `pii` can use vault storage: the canonical value in the Record object is a vault reference; the value is encrypted (AES-256-GCM, key from configuration with key ids for rotation) in the `vault` table.
- Erasure (`lorebase admin erase`) deletes the vault value and marks it erased; all Revisions keep their hashes and structure, and reads return `erased`. **[Architect call AC-22]**
- Vault storage costs one extra lookup per sensitive value read; it is opt-in per Attribute.

**Implementation notes (masking and the vault).** Decisions taken where the text above leaves room; where it was silent the more restrictive option was chosen.

- **Clearances.** `pii.read`, `confidential.read` and `restricted.read` clear the matching tag; an Attribute with several tags needs all of them. Among the built-in roles only `admin` holds them; custom roles (`keeper.auth.authz.custom_roles`) grant them to others. A grant counts when it is Domain-wide or narrowed to the Kind holding the value; grants narrowed to a branch do not count (whoever creates branches could name one to match). Kind-narrowed grants are resolved to Kind ids in the schema of the default branch, so renaming Kinds on a branch does not move a clearance. Link Attributes need the clearance on both Kinds of the Relation. A derived value whose tags come from Attributes of other Kinds (a Part count of Suppliers filtered on a `pii` Supplier Attribute) tells about their Records: each tag needs the clearance on the Kind holding the value and on every Kind it comes from (`effective_origins`, transitively through Derivations). This holds on every read path that renders, filters, orders or groups by values (`Masking.hidden`, one rule for Records, lists, queries, search, history, lineage, diffs, explain, context packs and the Derivation preview); Domain-wide clearances are unaffected.
- **Tags.** The tags that decide are effective tags: those of the schema read, plus those the same Attribute (by id) has on the default branch, carried through Derivations (a derived Attribute has the tags of everything it may read; `lorebase_core.expr.sensitivity`). Reading an older Revision, or a branch forked before a tag was added, therefore shows no more than the default branch would. Removing a tag, or a sensitive Attribute, Kind or Relation, on any branch (schema change or merge) needs the clearance for that tag on the Domain (403 `sensitivity_lowered`); the commit pipeline and the merge service refuse it unless Keeper checked the clearance, so no other writer (agent auto-merge, Source merges) can. Commits validate schemas and data, and the materializer, rebuild and verification compute titles and search vectors, with the default branch's tags added; the comparison of tags happens under the branch lock, against the tags the caller is cleared for. A branch forked before the default branch tagged an Attribute therefore cannot commit schema changes until its schema satisfies the rules with those tags (tag the derived Attributes, take the Attribute out of search, change the title): merge the default branch into it or tag them on the branch. A derived Attribute must carry every tag of what its Derivation may read, an Operation step value every tag of the Record Attributes its expression reads (through a path Derivation, the Attributes of its far Kind it reads; crossing a Kind only reveals linkage, which Kind scope governs as for Relations), and neither reads a vault Attribute; preconditions and the expressions that address Records (`target`, `from`, `to`) read no sensitive Attribute (their outcome, or a missing Record's key, reaches the caller); an Operation is checked again with the default branch's tags before it runs (422), and evaluation errors of expressions reading values the caller is not cleared for are reported without their message; a sensitive Attribute cannot be a primary key or a foreign key, as Record keys are shown without clearance (`schema.sensitivity_inherited`, `schema.vault_invalid`, `schema.sensitive_attribute`). A uniqueness violation on a sensitive Attribute does not name the Record holding the value.
- **Masking.** Uncleared callers get `keeper.reads.sensitivity.masked_value` (`"***"`), or no value with `omit: true` (Records only; history, lineage, diffs, Conflicts and Operation edits always show the mask). It applies to Records, Links, includes, traversals, neighborhoods, history and lineage values, diffs and changesets (JSON and NDJSON), explain and context packs (JSON and markdown), Conflicts and stored resolutions, extraction items (values the schema does not know are masked too, and evidence and match details of items about masked values are left out), and Operation edits (dry runs and rejections). Operation invocation records and Proposal descriptions keep parameter values only where they cannot hold a sensitive value, and the parameter digest is keyed with `vault.token_key` when the vault is configured; Source run errors, which quote rows, are shown only to callers cleared Domain-wide for every tag. Responses are cached per clearance and per tags of the default branch (`variant` of the cache key and ETag).
- **Search and titles.** Sensitive Attributes are left out of the full-text vectors, hence of the word lexicon, corrections and snippets, for every caller (`schema.sensitive_attribute` warning on `searchable`). A sensitive title Attribute gives Records no title (warning). On a branch forked before the default branch tagged a searchable or title Attribute, the materialized index and titles still hold its words: Keeper leaves such Kinds out of lexical search and corrections for callers not cleared for the tag, and drops their stored titles from responses and snippets; the materializer adds the default branch's tags when it next writes those Records, and verification reports titles it has not rewritten yet as `sensitivity_stale`. A commit or merge that tags an Attribute on the default branch queues a `store.sensitivity_refresh` job for every other branch whose titles or search vectors it makes stale, by the effective tags (overlays through the job of their base, copies of such a branch when they are made; `store.materialize.refresh_branches_on_tag`, on by default), which rewrites those titles and search vectors in batches without changing the branch's schema; `POST /domains/{d}/branches/{b}/rebase-tags` (`lorebase branch rebase-tags BRANCH`, `schema.write` on the branch) commits the default branch's tags to the branch's schema, never removing one, and recomputes the titles and search vectors of the Kinds it changes in the same Revision. Extraction decides what to extract by the effective tags, as reads do. Semantic and hybrid search rank by the vectors of a Kind whose template embeds sensitive Attributes (`embedding_sensitive`) only for cleared callers.
- **Filters.** Filtering, ordering, grouping or `include` filters on a sensitive Attribute, or on `title` when the title Attribute is sensitive, need the clearance (403 `sensitive_attribute`); on a vault Attribute they are refused for everyone (422 `vault_attribute`), the stored values being references.
- **Vault Attributes** (`vault: true`) must be tagged `pii`, belong to a Kind, and cannot be derived, unique, indexed, a reference, the title, a primary key or the foreign key of a Relation; templates never embed them (`schema.vault_invalid`, `schema.embedding_template_invalid`). Vault storage is chosen when the Attribute is created: turning it on or off, or retyping a vault Attribute, is refused (add a new Attribute instead).
- **Tokens.** `vlt_` + 128 bits of HMAC-SHA256 under `vault.token_key` over the Domain, Kind, Record key, Attribute, a generation and the canonical value. Writing an erased value again moves to the next generation, so Revisions that held the erased token keep reading `erased`. The commit pipeline validates the plain value, replaces it by its reference before the core rules run (CEL sees references), and inserts the row in the commit's transaction. A reference in a write is accepted only when the Record holds it at the branch head or a vault row binds it to the same Kind, key and Attribute; a Conflict over vault values is resolved by picking a side, never by a custom value.
- **Values.** AES-256-GCM under `vault.encryption_key` (key id `vault.key_id`; `previous_keys` still open), with the Domain and token as associated data. Responses are masked and cached with references; references are replaced on every answer, after the cache, by the value (cleared caller), `keeper.reads.sensitivity.erased_value` (erased), or the mask (uncleared caller, unknown token, or a key not configured). A path that misses masking shows a reference, never a value.
- **Erasure.** `POST /domains/{domain}/vault/erase` and `lorebase admin erase` (`pii.erase`, Domain-wide, `admin` only among built-in roles) erase by Record key (optionally some Attributes) or by value of one Attribute in every Record of a Kind (matched through a keyed fingerprint of the value's matching form, without the Record key: case, compatibility characters and white space are ignored). Erasure by key follows `vault_lineage` to every key the Record had: a commit that deletes a Record and writes another of the same Kind holding its references stores the values again under the new key and records the lineage. The ciphertext, key id and fingerprint are cleared and `erased_at` set, and the fingerprints go to `vault_tombstones`: a later write of an erased value is stored erased, refused or stored again (`vault.erased_values`). Commits that store vault values hold the Domain's vault lock shared, erasures exclusively. The erasure and its audit entry (`vault.erase`: Kind, a keyed digest of the key or the Attribute, count; never the key or the value) commit together. Erasing again erases nothing. Erasure covers vault values only: inline sensitive values stay in Revisions.
- **Feeding vault values.** The tombstone check of `vault.tombstone_origins` would tell whoever chooses a written value whether it was erased. Writing values of vault Attributes through those paths therefore needs `pii.import` (Domain-wide, `admin` only among built-in roles; custom roles may grant it), checked before anything is committed (403 naming `pii.import`): the built-in imports (`import_records`, `import_batch`, dry runs included) when a row holds a value of a vault Attribute; schema changes and Proposal merges that add a Source mapping columns to vault Attributes, change such a Source, or make an Attribute a Source maps a vault one; and `POST .../sources/{s}/sync` for a Source that maps vault Attributes; reviewer edits of extraction items (`items:decide`) that set a value of a vault Attribute. Scribe never accepts an extraction item holding a vault value at once (a reviewer decides). Scheduled synchronizations run as configured (`lorebase_keeper.services.pii_import`); `python -m lorebase_keeper sources vault` lists the Sources that map vault Attributes, so those defined before `pii.import` existed can be confirmed or disabled. No Keeper or CLI path retries or requeues a `source_sync` job.
- **Key rotation.** `vault.encryption_key` (with `key_id`, earlier ones in `previous_keys`) and `vault.token_key` (with `token_key_id`, earlier ones in `previous_token_keys`) rotate. New values use the current keys; a value written again finds the token it had under an earlier token key. The `vault.rotate` job (`lorebase admin vault rotate`, `vault.manage`, installation administrators) seals and fingerprints the live rows of every Domain again in batches and resumes where it stopped; `lorebase admin vault status` tells which earlier keys no row or tombstone uses any more. Tombstones hold no value and cannot be made again: dropping a token key retires the tombstones made under it.
- **Exports.** Bundles (`lorebase_store.bundle`) carry objects, hence references, and by default not the vault: values do not leave the installation through them, references imported elsewhere read as masked (a restore from a bundle loses vault values), and an import refuses a Record whose vault Attribute holds anything but a well-formed reference. An operator export with `--include-vault-values` adds the opened values in plain text (or `erased`), the key changes and the tombstones, listed in the manifest, read under the vault lock and audited (`bundle.export_plain_started`, `bundle.export_plain`) without the values; import seals them under the target's keys, bound as the vault row was (Records linked by key changes may share an erased token), validates them against the bundle's schema and applies the tombstones as `vault.erased_values` says.

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

**Implemented so far.** `lorebase_platform.settings` provides the layering above (`LOREBASE_CONFIG` names the YAML file; `LOREBASE__SECTION__KEY` overrides it) and `secret://env/NAME` and `secret://file//path` references. Packages declare their own sections with `register_section`, so a section is validated only where it is used. The sections `logging`, `telemetry`, `keeper`, `inference`, `database`, `store`, `read` and `search` exist in `config/lorebase.example.yaml`; `database.statement_timeout_ms` defaults to 30000 (bulk commits) rather than the 2000 shown above, which is a read-heavy tuning. `store` holds schema-shape values read once by the initial migration (`layout.hash_partitions`, `embeddings.dims`; see [store-schema-notes.md](store-schema-notes.md)). `read` (page sizes, DSL limits, traversal caps, time travel and diff bounds, the 2000 ms statement timeout of read transactions) and `search` (ranking and fusion weights) belong to Keeper's read path (8.6); `keeper.reads` holds the response cache and the reachability bound of read authorization (`reachability_max_depth`). The sections `auth`, `materialization`, `extraction` and `webhooks` arrive with the milestones that use them. The write endpoints read `keeper.domains`, `keeper.branches` (the materialized branch cap of `materialization.max_full_branches_per_domain` lives here as `max_materialized_per_domain`), `keeper.schema_changes` and `keeper.operations` (bulk and dry-run limits, direct commits on protected branches, skipping CEL preconditions while expressions are not enabled). `keeper.reconciliation` (page sizes, bulk limit, merge check) and `scribe.reconciliation` (detection) configure the reconciliation queue (10.6); a Domain overrides an allowlist of the latter in its `reconciliation` setting and can tighten the merge check with its flat `reconciliation_check` setting.

**Settings of the designed changes (not implemented yet).** Corrections that stick (9.1), document versions and ingestion modes (9.2), stewards (10.7) and read presets (8.8) add these keys; defaults keep today's behaviour wherever behaviour was defined before.

```yaml
scribe:
  extraction:
    mode: doubts_only              # implemented; review_all | doubts_only; direct is refused here
    domain_may_loosen: [mode]      # implemented; members a Domain may set looser; []: none
    direct_merge_protected: true   # implemented; direct-mode merges may target protected branches
    versions:                      # implemented
      max_values_per_job: 5000     # values one document_version_check job looks at
keeper:
  documents:
    max_source_key_chars: 1024     # implemented
  proposals:
    required_approvals_from_stewards: 0  # implemented; Domain setting of the same name
    required_roles: []             # implemented; Domain setting of the same name (10.3)
  stewards:                        # implemented
    max_assignees_listed: 50
    max_grants_scanned: 10000      # grants of a Domain read to find assignees
search:
  semantic:
    iterative_scan: auto           # auto: relaxed_order on pgvector >= 0.8; off: over-fetch
    max_scan_tuples: 20000         # hnsw.max_scan_tuples of an iterative scan
    overfetch_factor: 4            # without iterative scans: search.candidates * factor
```

Per Source (schema `sources.{name}`): `local_edits: review | keep | source_wins` (default `review`) and `stale_after` (ISO 8601 duration, default null; implemented, with `scribe.reconciliation.stale.source_unseen`). Per Domain (`settings.extraction`): `mode`, `auto_accept_threshold`, `min_confidence`, `rules: [{source_key_prefix, mode, auto_accept_threshold, min_confidence}]`. Per branch protection rule: `required_approvals_from_stewards` (default 0), implemented as `keeper.proposals.required_approvals_from_stewards` with a Domain setting of the same name, as `required_approvals` is. In the schema: `domain.stewards`, `kinds.{Kind}.stewards`, `lenses`.

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

### Next: knowledge that stays correct

Scope: corrections that stick (per-Source last-written state, local-edit Conflicts with a preserved base, `local_edits`, the extraction `local_edit` hold; 9.1, AC-34); document versions and lifecycle (`source_key`, versions, the evidence check of a new version, retirement and purge; 9.2, AC-35); ingestion modes (`extraction.mode`, prefix rules, the tier cap of `direct`; 9.2, AC-36); stewards (assignees, `assigned` filters, `required_approvals_from_stewards`; 10.7, AC-37); read presets (Traits as search targets and context seeds, lenses, filtered vector search; 8.8, AC-38). Follow-up, implemented: `last_seen_run` and `stale_after` for incremental Sources (`source_unseen` candidates, migration `0043`).

Acceptance:
- A person's correction on the target survives any number of Source runs whose upstream value did not change; an upstream change to that value opens a `DV1` whose base is the Source's previous value, and a snapshot run never deletes a Record holding someone else's values without a `DV2`. Both are audited as `source.local_edit_conflict`.
- `source_wins` reproduces the earlier behaviour exactly; upgrading a Source with existing data raises no Conflict where the fresh value equals the current one.
- A keyed upload of changed bytes creates version n+1; values whose quote is gone from it raise `stale` candidates and are never changed; a purged document's evidence reads `[erased]` while its provenance rows remain.
- Without Domain settings, extraction accepts and holds exactly what it did before; `direct` merges only all-tier-0 jobs with nothing pending, and any tier 1 or tier 2 item caps the job.
- `assigned=me` lists exactly the Proposals and candidates whose assignees include the caller, and never one touching a Kind the caller cannot read.
- A Trait or lens target never returns a Kind the caller cannot read; a filtered semantic search returns `limit` hits whenever that many readable Records match within the scan bound, and says `semantic_truncated` otherwise.

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
| Reconciliation raises too many candidates | Reviewer fatigue; the queue is ignored | Cheap stages first; normalization drops unit and formatting differences; fingerprints on values keep dismissals; thresholds and stages per Domain; candidates ranked by signal (10.6) |
| Cost of the semantic reconciliation stage | Model spend, slow sweeps | Off by default; duplicates compare stored vectors without model calls, in the sweep only; the judge is off and the operator's alone; extraction cache and job budgets; sweeps paced and resumable |
| Sensitive values reach a model through the semantic stage | Disclosure of `pii` or vault values to an inference provider | Values of vault and sensitivity-tagged Attributes are never sent (checked by the detector and again by the stage, fail closed); vault claims keep no evidence quote |
| Entity resolution errors cascade into reconciliation | False contradictions on the wrong Record; wrong duplicate merges | Claims attach only to exact or resolved matches; fuzzy and semantic matches only raise `duplicate` candidates; duplicate merges are reviewed Proposals; candidates become `obsolete` when their claims go |
| One contested value holds back a whole Source run | Upstream changes wait behind a local-edit Conflict under `review` | Conflicts are listed per Attribute and resolvable in bulk; stored resolutions re-apply on later runs; `keep` decides them by policy; the run's stats count local edits |
| Last-written state drifts from what the Source really wrote | Missed or spurious local edits | State advances only in the merge transaction; seeding is compare-only for values of other origins; garbage collection marks every Revision the state names as a root, so the preserved base can always be read |
| `direct` mode lets wrong deterministic output through | Errors on a protected branch without review | Never a default; tier 0 only; nothing pending; Conflicts and blocking reconciliation stop it; operator may forbid with `domain_may_loosen: []`; every merge audited |
| Iterative vector scans cost more under very selective filters | Slower semantic search | Bounded by `max_scan_tuples`; flagged with `semantic_truncated`; over-fetch fallback; budget row in 8.7 |
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
| AC-31 | Contradictions between Sources go to a reconciliation queue, separate from merge Conflicts | They concern values already merged, outlive the Proposal that raised them and compare N claims, not ours and theirs | Conflicts and candidates need one review flow |
| AC-32 | Valid time is opt-in per Attribute and stored on provenance entries, not in content | No change to hashes, merge or materialized values; Attributes without it behave as before | Bitemporal queries become a goal |
| AC-33 | Reconciliation never resolves, invalidates or hides a value on its own; every decision is a person's, or a policy a person applied, written through Operations | Overlapping facts are often both true; silent invalidation loses information | Never |
| AC-34 | A Source writes only Attributes that changed upstream since it last wrote them (per-Source last-written state); an upstream change that meets someone else's value is a `DV1` or `DV2` with a preserved base, decided per `local_edits` (default `review`) | Corrections stick; upstream changes are never dropped; reuses the merge, Conflict and policy machinery | Sources need per-field write-back to upstream systems |
| AC-35 | Documents have an optional stable `source_key`; changed bytes are a new version; values whose evidence quote is gone raise `stale` candidates, never retractions; purge deletes content but keeps provenance rows | Documents change upstream like any Source; the check is literal and deterministic | Evidence matching beyond verbatim quotes is needed |
| AC-36 | Ingestion modes `review_all`, `doubts_only` (default, today's rule), `direct`; `direct` only per Domain, only for jobs whose items are all tier 0 | Lets deterministic feeds flow without weakening AC-19 | Never for tier 1 or 2 output |
| AC-37 | Stewards are RBAC roles named in the schema; assignees are computed, never stored | No new principal model; follows grants and invisibility | Assignment needs per-person routing that roles cannot express |
| AC-38 | Traits and lenses are read presets resolved against the caller's projection; filtered vector search keeps filters before the limit and uses iterative scans or over-fetch so it does not shorten silently | Presets never widen grants; results stay complete and leak-free | Exact vector search is needed by default |

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
| AC-34 | Implemented | `lorebase_scribe.sources.delta` (comparison with the last-written state, local edits, seeding), `lorebase_scribe.sources.sync` (staging, `keep` resolutions, audit), `lorebase_store.merging.preserved` and `merge/engine.py` (`base_overrides`, the preserved base), `lorebase_store.repositories.source_written` (advanced in the merge), migration `0040`; the extraction hold in `lorebase_scribe.extraction.assemble` |
| AC-35 | Implemented | `lorebase_store.repositories.documents` (versions, retire, purge, migration `0041`), `lorebase_keeper.routes.documents` (intake by key, versions, retirement, purge), `lorebase_scribe.reconcile.versions` (job `document_version_check`), `lorebase_core.reconcile.evidence_gone_verdict` |
| AC-31, AC-32, AC-33 | Implemented | `lorebase_core.reconcile` (claims, normalization, classification, fingerprints), `lorebase_scribe.reconcile` (triggers, sweep, semantic stage), `lorebase_keeper.services.reconciliation` (decisions through `update_{kind}`, decision branches, duplicate merges), migration `0038`; valid time on `ProvenanceEntry` and `Attribute.valid_time` |
| AC-25, AC-29 | Partial | `packages/inference`: interfaces, settings, registry and the deterministic fake provider; no HTTP provider yet |

Merge (6.8 to 6.10) is implemented in `merge/` on top of AC-09, AC-11 and AC-13; its decisions are in 6.15.

