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
- [ ] Performance baseline on the 1M-Record fixture (the harness and a 100k-Record baseline are in [performance](performance.md): every read is within budget for both a role not subject to row-level security and one subject to it, through the security definer read functions and the branch-scoped search index of migration `0011`. The 1M-Record run is in [performance](performance.md#1m-records): key, page, Links, attribute and full-text reads, Record history and the neighborhood on `main` are within budget for both roles; typo-tolerant search, about 120 ms at p95 with whole-title trigrams, is within budget (7.6 to 14 ms) since it corrects words from a per-branch word lexicon (migration `0018`), the neighborhood on other branches is just over it (29 to 34 ms) when the working set does not fit in memory, and copy branches take longer than the default statement timeout to create)
- [x] M1 documentation and release checklist ([release](release.md), [CHANGELOG](../CHANGELOG.md))

Acceptance (design 18): a Domain applied from YAML, filled by CLI import of 1M Records, branched, merged with Conflicts resolved through the API and diffed, with property tests for hashing, merge and diff laws, and the read targets met on the 1M fixture. Not met yet.

## M2: Structured ingestion and exploration

Goal: days 1 and 2 of the [proof scenario](design.md#32-the-5-day-proof-scenario) run end to end.

- [x] Scribe job runner on the Postgres job queue, with a leader-elected scheduler ([scribe](scribe.md))
- [x] CSV, Parquet and Postgres Sources, snapshot and incremental sync, plus JSON and JSON Lines file Sources ([scribe](scribe.md#sources)); Parquet Sources need the `lorebase-scribe[parquet]` extra
- [x] MySQL/MariaDB and SQL Server Sources (`mysql_table`, `mysql_query`, `mssql_table`, `mssql_query`) with the optional `lorebase-scribe[mysql]` (asyncmy) and `lorebase-scribe[mssql]` (pymssql) extras ([scribe](scribe.md#sources))
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
- [x] Custom Operations with CEL, review policies, outbox and webhooks: Operations declare side effects, written to the outbox in their commit transaction and delivered at least once to signed webhooks with masking of sensitive values ([agents](agents.md#operation-events), [design 10.5](design.md#105-side-effects))
- [ ] Full RBAC: groups, sensitivity masking, Kind-scoped invisibility in queries (groups from the OIDC groups claim, Kind-scoped grants on reads and sensitivity masking with `pii.read`, `confidential.read` and `restricted.read` are done, see [design 13.5](design.md#135-personal-data-and-erasure))
- [x] Branch protection on the default branch
- [x] Proposal checks (merge, approvals) and required approvals
- [x] Modeler with every editor: Kinds, Relations, Traits, Operations (with dry run), Derivations and Sources in one form and YAML document, with Keeper's validation and CEL type check next to the fields
- [x] Conflict resolver against live data
- [x] Traits in queries, at branch heads and in time-travel reads (listings ordered by key, with the head's cursors)

## M4: Documents, extraction and semantic search

Goal: day 3 of the proof scenario, on the sample contracts.

- [x] Document store interface and parsers (Postgres blob backend; Markdown, text, PDF, DOCX)
- [x] Tiered extraction: rules, local GLiNER and GLiREL, then any LLM with JSON schema output; cache, verified evidence, entity resolution, schema suggestions aggregated over documents and accepted as schema change Proposals (migration `0021`, [docs/extraction.md](extraction.md#schema-suggestions))
- [x] OpenAI-compatible chat client, fastembed and OpenAI-compatible embeddings, compose profiles `local-models` and `litellm` ([inference providers](inference-providers.md))
- [x] Extraction Proposal items and evidence Review UI (an Items tab in Review accepts, edits or rejects items one by one or by filter; the Documents viewer highlights the evidence)
- [x] Embedding jobs and hybrid search: Kinds opt in with `search.embedding_template`; scheduled Scribe passes embed the Records that lack a vector of the configured model (keyed by content hash and template fingerprint, shared across branches and overlays with the same template, batched, rate-limited, resumable), `lorebase-scribe embeddings` backfills and reindexes (migrations `0014` and `0024`, [scribe](scribe.md#embeddings)); query vectors are computed on the caller's side (`lorebase-inference embed`, `lorebase search --vector-file`). The model is advertised in `GET /schema` and `lorebase_schema`; prefixes for E5/BGE style models are settings; garbage collection deletes the vectors no branch reads (orphaned content, templates no longer used)
- [x] Derivations: expression, aggregate and path (expression and aggregate are recomputed in the materialized head; path Derivations are virtual Relations composed at read time with a bounded fan-out, or stored and kept current incrementally when `materialized`, and are read by neighborhoods, Link counts, Operation `links()` and other Derivations; a change of the fan-out rewrites what follows it (migration `0030`), see [design 5.8](design.md#58-derivation))

## M5: Enterprise hardening and 1.0

- [x] Overlay branch materialization (AC-16 implemented, migration `0010`, [store schema notes](store-schema-notes.md#overlay-branches)); landed early, `copy` stays the default mode outside the `agent/`, `extract/` and `source/` prefixes, and agent task branches are overlays (`keeper.agent_writes.branch_mode`). Overlays get their own per-Attribute indexes, and migrations `0011` (branch-scoped search index) and `0012` (overlay neighborhood lookups) keep overlay reads within 30% of a copy at p50 ([performance](performance.md#overlay-branches-milliseconds))
- [x] PII vault storage and erasure (`vault: true` Attributes, migration `0019`, `lorebase admin erase`, see [design 13.5](design.md#135-personal-data-and-erasure)); landed early
- [x] Helm chart ([kubernetes](kubernetes.md)); landed early, validated by lint, template and kubeconform, not yet on a live cluster
- [x] Garbage collection of unreachable objects ([operations](operations.md)); landed early
- [x] Observability: a catalogue of every metric with explicit histogram buckets, Grafana dashboards for Keeper, Store and Scribe, Prometheus alert rules with runbooks and the OpenTelemetry Collector configuration, in compose and the Helm chart ([metrics](metrics.md), [operations](operations.md#dashboards-and-alerts)); landed early
- [ ] Performance targets of design 6.14 met at 10M Records, with a published benchmark harness
- [x] Backup and restore guide, audit chain verification, signed images (Domain bundles and `pg_dump` in [operations](operations.md#backup-and-restore-of-a-domain); `python -m lorebase_keeper audit verify`, `GET .../audit/verify`, `lorebase audit verify` and a daily Scribe job with an alert, [operations](operations.md#audit-chain-verification); images signed with cosign by the release workflow, [release](release.md))
- [x] HTTP JSON Source with cursor, page, offset and Link header pagination ([scribe](scribe.md#sources)); landed early
- [x] LinkML schema import and export (`lorebase schema export --format linkml`, `lorebase schema import FILE --from linkml`; round trips through `lorebase.*` annotations, [LinkML](linkml.md))
- [x] Documentation site at [aantenore.github.io/lorebase](https://aantenore.github.io/lorebase/), built with `mkdocs build --strict` (`make docs`) and published by the Docs workflow
- [ ] API stability guarantees for `/api/v1` (the policy is written, [API stability](api-stability.md): what is stable, compatible and breaking changes, deprecation, client compatibility, MCP tools and the schema format; it binds from 1.0, and Keeper does not emit `Deprecation` or `Sunset` headers yet since nothing is deprecated)
- [x] Threat model published and the security review checklist completed ([threat model](threat-model.md): STRIDE per trust boundary with mitigations and residual risks; [security review](security-review.md): 23 findings, all fixed, none open)
- [x] Full proof scenario (days 1 to 5) as an automated end-to-end test: the five-day demo asserts every day against a real stack with the fake inference provider (a document extracted with the deterministic tiers, the agent over MCP writing through a Proposal), and the End-to-end workflow runs it on pushes to `main`, nightly and on relevant pull requests ([demo](demo.md#assertions-and-ci))

## Not on the roadmap

These are written down as non-goals or anti-goals in the [design](design.md#2-goals-non-goals-anti-goals): a model in the agent read path, direct model writes to protected branches, schemaless writes, a second required datastore, an agent runtime, an ETL platform, BI dashboards, OWL reasoning, and multi-region active-active writes.

## How to influence it

Open a [discussion](https://github.com/aantenore/lorebase/discussions) with the domain you would model and what your agents need to read or change. Real scenarios move items up. Design changes are proposed as issues that cite the section of the design they challenge; see [CONTRIBUTING.md](../CONTRIBUTING.md).
