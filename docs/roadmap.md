# Roadmap

The public roadmap of Lorebase, by milestone. It is derived from [section 18 of the design](design.md#18-roadmap) and from what is on `main` today. A checked box means the item is merged on `main` and covered by tests. An unchecked box is not done, even when part of it exists; the note says what is missing. Dates are deliberately absent: the order is firm, the timing is not.

Lorebase is pre-alpha. Nothing is released, no tag exists and interfaces may change.

## Next up

1. Python client and CLI (M1, T24 and T25).
2. Lineage and history endpoints, then the 1M-Record performance baseline (M1, T23 and T28).
3. Expressions accepted in schema change sets, so the CEL path of stored Operations can be used end to end.
4. Scribe and Source ingestion (M2).

The ordered task list of M1 is in the [M1 plan](plan/m1.md).

## M1: Engine and API

Goal: define a Domain in YAML, import data, branch, change, merge with Conflicts resolved through the API, diff, and read Records in milliseconds, all through REST and CLI.

Engine and storage

- [x] Canonical JSON and content hashing, type system and value encoding
- [x] Schema element models with static validation; schema as YAML (load and dump, stable ids, renames keep identity)
- [x] Canonical HAMT trees, tree diff, immutable Revisions, ancestry and merge base
- [x] Data validation against the schema
- [x] Three-way merge of schema and data, full Conflict taxonomy, resolutions, merge policies `prefer`, cascade and defaults
- [ ] Merge policies `authority` and `newest_wins`
- [x] Postgres schema and Alembic migrations, row-level security, store repositories
- [x] Commit pipeline, materializer and verification
- [x] In-memory merge demo ([examples/merge_demo.py](../examples/merge_demo.py))
- [x] CEL expression engine with type checking and evaluation budgets (`lorebase_core.expr`), and expression checks in schema validation
- [x] Per-Attribute partial indexes on materialized heads, maintained on schema changes when the commit role owns the tables

Keeper REST API

- [x] Application skeleton: settings, structured logging, OpenTelemetry, error format, health and readiness
- [x] Authentication with API keys and OIDC bearer tokens, role grants per Domain, Kind, Operation and branch, hash-chained audit log
- [x] Domains, branches and tags
- [x] Schema read and validate, schema change sets
- [x] Built-in Operations: create, update, delete, link, unlink, bulk import, with dry run and idempotency
- [x] Records and Links, the query DSL, traversal, neighborhood, lexical search, time travel, ETags and conditional reads
- [x] Revisions, branch log and diff
- [x] Proposals, reviews, required approvals, merge and Conflict resolution API, scoped to what the caller may read
- [ ] Lineage and history endpoints (a lineage summary is available on Record reads; per-Record lineage and history endpoints are not)
- [ ] Expressions (CEL): stored Operations evaluate them behind `keeper.operations.expressions_enabled` (off by default), but schema change sets still refuse schemas that use expressions

Clients and operations

- [ ] Python client
- [ ] CLI
- [x] Docker image and docker compose for Postgres, migrations and Keeper
- [x] CI: lint, types, import contracts, unit and integration tests; CodeQL, Scorecard, SBOM and pinned actions
- [x] Inference interfaces and a deterministic fake provider (no model calls in M1; the read path cannot import them)
- [x] Read replica routing with a freshness guard and a `scale-out` compose profile ([deployment](deployment.md)). The design lists this under M5; it landed early. Individual read endpoints adopt it incrementally.
- [ ] Performance baseline on the 1M-Record fixture (the harness and a 100k-Record baseline are in [performance](performance.md): every read is within budget for a role not subject to row-level security; under RLS, Links of a Record, the neighborhood and search are not)
- [ ] M1 documentation and release checklist

Acceptance (design 18): a Domain applied from YAML, filled by CLI import of 1M Records, branched, merged with Conflicts resolved through the API and diffed, with property tests for hashing, merge and diff laws, and the read targets met on the 1M fixture. Not met yet.

## M2: Structured ingestion and exploration

Goal: days 1 and 2 of the [proof scenario](design.md#32-the-5-day-proof-scenario) run end to end.

- [ ] Scribe job runner on the Postgres job queue (the queue and a worker for store jobs exist: garbage collection, index builds)
- [ ] CSV, Parquet and Postgres Sources, snapshot and incremental sync
- [x] Schema inference from CSV files and Postgres tables, with a golden test on the example `supply` schema ([inference](inference.md)); Sources do not run it yet
- [ ] Source branches, merge policies and Lineage for syncs
- [ ] OIDC login with BFF sessions for the web app (bearer token validation in Keeper is done)
- [x] Web app shell, Explorer, Canvas (graph and schema graph) and Review screens on the mock API
- [ ] Web app verified against a live Keeper
- [ ] Review: Proposal list and detail, schema and data diff and merge against live Proposals
- [ ] Canvas rendering 5k nodes and 10k edges smoothly, with a worker layout

## M3: Governance and agents

Goal: day 5 of the proof scenario, an external agent connected through MCP.

- [ ] Herald, the MCP server: tools, resources and prompts, over stdio and Streamable HTTP
- [ ] Custom Operations with CEL, review policies, outbox and webhooks (the executor evaluates CEL and review policies behind a setting, see M1; signed webhooks on Revision events are built, see [agents](agents.md); the Operation outbox is not)
- [ ] Full RBAC: groups, sensitivity masking, Kind-scoped invisibility in queries (Kind-scoped grants on reads are already enforced)
- [x] Branch protection on the default branch
- [x] Proposal checks (merge, approvals) and required approvals
- [ ] Modeler with every editor (schema editing exists on the mock API; Operation, Derivation and Source editors are not built)
- [ ] Conflict resolver against live data (the screen exists on the mock API)
- [ ] Traits in queries

## M4: Documents, extraction and semantic search

Goal: day 3 of the proof scenario, on the sample contracts.

- [ ] Document store interface and parsers
- [ ] Tiered extraction: rules, local GLiNER and GLiREL, then any LLM with JSON schema output; cache, verified evidence, entity resolution, schema suggestions
- [ ] OpenAI-compatible chat client, fastembed and OpenAI-compatible embeddings, compose profiles for local models and a LiteLLM proxy
- [ ] Extraction Proposal items and evidence Review UI
- [ ] Embedding jobs and hybrid search (the read path already ranks stored vectors by caller-supplied query vectors; nothing computes embeddings)
- [ ] Derivations: expression, aggregate and path

## M5: Enterprise hardening and 1.0

- [ ] Overlay branch materialization
- [ ] PII vault storage and erasure
- [ ] Helm chart
- [x] Garbage collection of unreachable objects ([operations](operations.md)); landed early
- [ ] Performance targets of design 6.14 met at 10M Records, with a published benchmark harness
- [ ] Backup and restore guide, audit chain verification, signed images (Domain export and import bundles and their guide are in [operations](operations.md))
- [ ] HTTP JSON Source
- [ ] LinkML schema import and export
- [ ] Documentation site and API stability guarantees for `/api/v1`
- [ ] Threat model published and the security review checklist completed
- [ ] Full proof scenario (days 1 to 5) as an automated end-to-end test

## Not on the roadmap

These are written down as non-goals or anti-goals in the [design](design.md#2-goals-non-goals-anti-goals): a model in the agent read path, direct model writes to protected branches, schemaless writes, a second required datastore, an agent runtime, an ETL platform, BI dashboards, OWL reasoning, and multi-region active-active writes.

## How to influence it

Open a [discussion](https://github.com/aantenore/lorebase/discussions) with the domain you would model and what your agents need to read or change. Real scenarios move items up. Design changes are proposed as issues that cite the section of the design they challenge; see [CONTRIBUTING.md](../CONTRIBUTING.md).
