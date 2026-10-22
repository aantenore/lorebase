# Roadmap

The public roadmap of Lorebase, by milestone. It is derived from [section 18 of the design](design.md#18-roadmap) and from what is on `main` today. A checked box means the item is merged on `main` and covered by tests. An unchecked box is not done, even when part of it exists; the note says what is missing. Dates are deliberately absent: the order is firm, the timing is not.

Lorebase is pre-alpha. [v0.1.0](https://github.com/aantenore/lorebase/releases/tag/v0.1.0) is the first pre-alpha release ([CHANGELOG](../CHANGELOG.md)); interfaces may change.

## Next up

1. The performance baseline on the 1M-Record fixture (M1, T28).

The ordered task list of M1 is in the [M1 plan](plan/m1.md).

## M1: Engine and API

Goal: define a Domain in YAML, import data, branch, change, merge with Conflicts resolved through the API, diff, and read Records in milliseconds, all through REST and CLI.

Engine and storage

- [x] Canonical JSON and content hashing, type system and value encoding
- [x] Schema element models with static validation; schema as YAML (load and dump, stable ids, renames keep identity)
- [x] Canonical HAMT trees, tree diff, immutable Revisions, ancestry and merge base
- [x] Data validation against the schema
- [x] Three-way merge of schema and data, full Conflict taxonomy, resolutions, merge policies `prefer`, cascade and defaults
- [x] Merge policies `authority` and `newest_wins`
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
- [x] Lineage and history endpoints: per-Record history and lineage over REST, client, CLI, MCP and the web Explorer
- [x] Expressions (CEL) end to end: schema change sets type check them (`schema.expressions_enabled`), Keeper executes custom Operations with them and the materializer recomputes `expression` and `aggregate` Derivations

Clients and operations

- [x] Python client (sync and async), checked against the committed OpenAPI document
- [x] CLI (`lorebase`)
- [x] Docker image and docker compose for Postgres, migrations and Keeper
- [x] CI: lint, types, import contracts, unit and integration tests; CodeQL, Scorecard, SBOM and pinned actions
- [x] Inference interfaces and a deterministic fake provider (no model calls in M1; the read path cannot import them)
- [x] Read replica routing with a freshness guard and a `scale-out` compose profile ([deployment](deployment.md)). The design lists this under M5; it landed early. Individual read endpoints adopt it incrementally.
- [ ] Performance baseline on the 1M-Record fixture (the harness and a 100k-Record baseline are in [performance](performance.md): every read is within budget for both a role not subject to row-level security and one subject to it, through the security definer read functions and the branch-scoped search index of migration `0011`; the 1M-Record run is not done)
- [x] M1 documentation and release checklist ([release](release.md), [CHANGELOG](../CHANGELOG.md))

Acceptance (design 18): a Domain applied from YAML, filled by CLI import of 1M Records, branched, merged with Conflicts resolved through the API and diffed, with property tests for hashing, merge and diff laws, and the read targets met on the 1M fixture. Not met yet.

## M2: Structured ingestion and exploration

Goal: days 1 and 2 of the [proof scenario](design.md#32-the-5-day-proof-scenario) run end to end.

- [x] Scribe job runner on the Postgres job queue, with a leader-elected scheduler ([scribe](scribe.md))
- [x] CSV, Parquet and Postgres Sources, snapshot and incremental sync, plus JSON and JSON Lines file Sources ([scribe](scribe.md#sources)); Parquet Sources need the `lorebase-scribe[parquet]` extra
- [x] Schema inference from CSV, JSON, JSON Lines and Parquet files and Postgres tables, with a golden test on the example `supply` schema ([inference](inference.md)); any Source can be profiled through its connector, but syncs do not run inference
- [x] Source branches, merge policies and Lineage for syncs
- [x] OIDC login with BFF sessions for the web app: authorization code with PKCE, server-side sessions in Postgres (migration `0013`), CSRF checks, refresh and RP-initiated logout ([deployment](deployment.md#web-sign-in)); the web app signs in with `VITE_AUTH_MODE=session`
- [x] Web app shell, Explorer, Canvas (graph and schema graph) and Review screens on the mock API
- [x] Web app Modeler "Start from data" wizard over schema inference, a Sources screen (list, runs, YAML editor) and Admin basics (API keys, principals and grants, audit log) on the mock API; Source runs and Sync now use Keeper's Source routes in live mode
- [x] Web app verified against a live Keeper (Playwright flows against Keeper in CI)
- [x] Review: Proposal list and detail, reviews, data diff and merge against live Proposals
- [x] Canvas rendering 5k nodes and 10k edges, with a worker layout (first render of the generated graph in about 0.6 s instead of 1.7 s, see [performance](performance.md#web-app-canvas-5000-nodes-10000-edges)); frame times were measured on a software WebGL only

## M3: Governance and agents

Goal: day 5 of the proof scenario, an external agent connected through MCP.

- [x] Herald, the MCP server: tools, resources and prompts, over stdio and Streamable HTTP ([MCP server](mcp.md))
- [ ] Custom Operations with CEL, review policies, outbox and webhooks (CEL and review policies work, see M1; the outbox and signed webhooks carry Revision events only, see [agents](agents.md); Operations cannot declare their own events)
- [ ] Full RBAC: groups, sensitivity masking, Kind-scoped invisibility in queries (groups from the OIDC groups claim and Kind-scoped grants on reads are done; sensitivity masking is not)
- [x] Branch protection on the default branch
- [x] Proposal checks (merge, approvals) and required approvals
- [x] Modeler with every editor: Kinds, Relations, Traits, Operations (with dry run), Derivations and Sources in one form and YAML document, with Keeper's validation and CEL type check next to the fields
- [x] Conflict resolver against live data
- [ ] Traits in queries (they work at branch heads; time-travel reads do not support them)

## M4: Documents, extraction and semantic search

Goal: day 3 of the proof scenario, on the sample contracts.

- [x] Document store interface and parsers (Postgres blob backend; Markdown, text, PDF, DOCX)
- [ ] Tiered extraction: rules, local GLiNER and GLiREL, then any LLM with JSON schema output; cache, verified evidence, entity resolution, schema suggestions (all but schema suggestions done, [docs/extraction.md](extraction.md))
- [x] OpenAI-compatible chat client, fastembed and OpenAI-compatible embeddings, compose profiles `local-models` and `litellm` ([inference providers](inference-providers.md))
- [x] Extraction Proposal items and evidence Review UI (an Items tab in Review accepts, edits or rejects items one by one or by filter; the Documents viewer highlights the evidence)
- [x] Embedding jobs and hybrid search: Kinds opt in with `search.embedding_template`; scheduled Scribe passes embed the Records that lack a vector of the configured model (content-hash keyed, shared across branches and overlays, batched, rate-limited, resumable), `lorebase-scribe embeddings` backfills and reindexes (migration `0014`, [scribe](scribe.md#embeddings)); query vectors are computed on the caller's side (`lorebase-inference embed`, `lorebase search --vector-file`). The model is not advertised in `GET /schema` or `describe_domain` yet
- [ ] Derivations: expression, aggregate and path (expression and aggregate are recomputed in the materialized head; path Derivations are defined in the schema but not materialized)

## M5: Enterprise hardening and 1.0

- [x] Overlay branch materialization (AC-16 implemented, migration `0010`, [store schema notes](store-schema-notes.md#overlay-branches)); landed early, `copy` stays the default mode outside the `agent/`, `extract/` and `source/` prefixes, and agent task branches are overlays (`keeper.agent_writes.branch_mode`). Overlays get their own per-Attribute indexes, and migrations `0011` (branch-scoped search index) and `0012` (overlay neighborhood lookups) keep overlay reads within 30% of a copy at p50 ([performance](performance.md#overlay-branches-milliseconds))
- [ ] PII vault storage and erasure
- [x] Helm chart ([kubernetes](kubernetes.md)); landed early, validated by lint, template and kubeconform, not yet on a live cluster
- [x] Garbage collection of unreachable objects ([operations](operations.md)); landed early
- [x] Observability: a catalogue of every metric with explicit histogram buckets, Grafana dashboards for Keeper, Store and Scribe, Prometheus alert rules with runbooks and the OpenTelemetry Collector configuration, in compose and the Helm chart ([metrics](metrics.md), [operations](operations.md#dashboards-and-alerts)); landed early
- [ ] Performance targets of design 6.14 met at 10M Records, with a published benchmark harness
- [ ] Backup and restore guide, audit chain verification, signed images (Domain export and import bundles and their guide are in [operations](operations.md); the release workflow signs the images with cosign, [release](release.md); the audit chain check exists in the store, `AuditRepository.verify`, but no command or endpoint runs it)
- [x] HTTP JSON Source with cursor, page, offset and Link header pagination ([scribe](scribe.md#sources)); landed early
- [ ] LinkML schema import and export
- [x] Documentation site at [aantenore.github.io/lorebase](https://aantenore.github.io/lorebase/), built with `mkdocs build --strict` (`make docs`) and published by the Docs workflow
- [ ] API stability guarantees for `/api/v1` (no stability policy is written yet)
- [ ] Threat model published and the security review checklist completed (the review of the trust boundaries is done: 23 findings, all fixed, none open, [security review](security-review.md); the threat model document is not written)
- [ ] Full proof scenario (days 1 to 5) as an automated end-to-end test (`make demo` runs the scenario against a real stack, [demo](demo.md); it is not part of CI)

## Not on the roadmap

These are written down as non-goals or anti-goals in the [design](design.md#2-goals-non-goals-anti-goals): a model in the agent read path, direct model writes to protected branches, schemaless writes, a second required datastore, an agent runtime, an ETL platform, BI dashboards, OWL reasoning, and multi-region active-active writes.

## How to influence it

Open a [discussion](https://github.com/aantenore/lorebase/discussions) with the domain you would model and what your agents need to read or change. Real scenarios move items up. Design changes are proposed as issues that cite the section of the design they challenge; see [CONTRIBUTING.md](../CONTRIBUTING.md).
