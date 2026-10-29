# Roadmap

The public roadmap of Lorebase, by milestone. It is derived from [section 18 of the design](design.md#18-roadmap) and from what is on `main` today. A checked box means the item is merged on `main` and covered by tests. An unchecked box is not done, even when part of it exists; the note says what is missing. Dates are deliberately absent: the order is firm, the timing is not.

Lorebase is pre-alpha. [v0.2.3](https://github.com/aantenore/lorebase/releases/tag/v0.2.3) is the current pre-alpha release (the first was [v0.1.0](https://github.com/aantenore/lorebase/releases/tag/v0.1.0)) ([CHANGELOG](../CHANGELOG.md)); interfaces may change.

## Next up

1. The 10M-Record benchmark on the 32 GB reference hardware of design 6.14, and the 1M-Record neighborhood on branches other than `main`, which is just over budget on a shared laptop (M5).
2. API stability guarantees for `/api/v1`, which bind from 1.0 (M5).
3. Knowledge that stays correct: corrections that stick (done), document versions (done), ingestion modes (done), stewards (done) and read presets ([below](#next-knowledge-that-stays-correct)), designed.

Everything else on this page is merged and covered by tests. The ordered task list of M1 is in the [M1 plan](plan/m1.md).

## M1: Engine and API

Goal: define a Domain in YAML, import data, branch, change, merge with Conflicts resolved through the API, diff, and read Records in milliseconds, all through REST and CLI.

Engine and storage

- [x] Canonical JSON and content hashing, type system and value encoding
- [x] Schema element models with static validation; schema as YAML (load and dump, stable ids, renames keep identity)
- [x] Canonical HAMT trees, tree diff, immutable Revisions, ancestry and merge base
- [x] Data validation against the schema
- [x] Three-way merge of schema and data, full Conflict taxonomy, resolutions, merge policies `prefer`, cascade and defaults
- [x] Three-way merge of long `text` values by sentence or paragraph inside the data merge (design 6.8 step 3); the Conflict resolver showing the hunks is next
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
- [x] Built-in Operations: create, update, delete, link, unlink, bulk import, bulk delete, with dry run and idempotency
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
- [x] Performance baseline on the 1M-Record fixture (the harness and a 100k-Record baseline are in [performance](performance.md): every read is within budget for both a role not subject to row-level security and one subject to it, through the security definer read functions and the branch-scoped search index of migration `0011`. The 1M-Record run is in [performance](performance.md#1m-records): key, page, Links, attribute and full-text reads, Record history and the neighborhood on `main` are within budget for both roles; typo-tolerant search, about 120 ms at p95 with whole-title trigrams, is within budget (7.6 to 14 ms) since it corrects words from a per-branch word lexicon (migration `0018`), the neighborhood on other branches is just over it (29 to 34 ms) when the working set does not fit in memory, which the reference-hardware item of M5 covers, and copy branches are created by a job in batches instead of one request, migration `0017`)
- [x] M1 documentation and release checklist ([release](release.md), [CHANGELOG](../CHANGELOG.md))

Acceptance (design 18): a Domain applied from YAML, filled by CLI import of 1M Records, branched, merged with Conflicts resolved through the API and diffed, with property tests for hashing, merge and diff laws, and the read targets met on the 1M fixture. Met, except the neighborhood on branches other than `main` on a shared laptop (see the M5 reference-hardware item).

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
- [x] Full RBAC: groups from the OIDC groups claim, Kind-scoped grants on reads, sensitivity masking with `pii.read`, `confidential.read` and `restricted.read` ([design 13.5](design.md#135-personal-data-and-erasure)), and Kind-scoped invisibility in queries: Kinds a Kind-narrowed caller may not read are left out of the schema it reads and answer like unknown names ([design 13.3](design.md#133-authorization-enforcement))
- [x] Branch protection on the default branch
- [x] Proposal checks (merge, approvals) and required approvals
- [x] Modeler with every editor: Kinds, Relations, Traits, Operations (with dry run), Derivations and Sources in one form and YAML document, with Keeper's validation and CEL type check next to the fields
- [x] Conflict resolver against live data
- [x] Traits in queries, at branch heads and in time-travel reads (listings ordered by key, with the head's cursors)
- [x] Reconciliation queue for contradictions between Sources, separate from merge Conflicts ([design 10.6](design.md#106-reconciliation-of-source-contradictions)): `reconciliation_candidates`, optional validity periods on provenance, the `valid_time` Attribute flag and the `reconciliation` origin type (migrations `0038` and `0039`); detection stages identity, values, normalization and the optional semantic stage in Scribe, with the extraction and Source sync triggers and the `reconcile_sweep` job; decisions written through Operations with provenance and audit, dismissal and reopening, duplicate merges as Proposals and the Proposal merge check; REST routes, Python client and CLI (`lorebase reconcile`); the Reconciliation screen in the web app ([Scribe](scribe.md#reconciliation), [tour](tour.md#reconciliation))

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
- [x] Helm chart ([kubernetes](kubernetes.md)); landed early, validated by lint, template and kubeconform, and installed, upgraded and tested on a kind cluster with an external Postgres; the CloudNativePG mode has not been run
- [x] Garbage collection of unreachable objects ([operations](operations.md)); landed early
- [x] Observability: a catalogue of every metric with explicit histogram buckets, Grafana dashboards for Keeper, Store and Scribe, Prometheus alert rules with runbooks and the OpenTelemetry Collector configuration, in compose and the Helm chart ([metrics](metrics.md), [operations](operations.md#dashboards-and-alerts)); landed early
- [ ] Performance targets of design 6.14 met at 10M Records on the reference hardware, with a published benchmark harness (the harness is published: `make perf-10m` loads 1M Records through the commit pipeline, replicates them to 10,001,100 Records and 30,327,660 Links and measures every read, the agent trace, commits and branch creation; [performance](performance.md#10m-records). On a shared laptop with an 8 GiB Docker VM, get by key, pages, Links of a Record and attribute queries are at or near budget, branch creation stays at 42 to 147 ms; the 2-hop neighborhood (96 to 153 ms at p95, bound by pages read from disk), full-text search (59 to 111 ms, the GIN bitmap of frequent terms), history and lineage (17 to 28 ms) are over it, and a commit of 1,000 changes takes 3.3 s. Re-measured on 2026-10-23 with realistic Zipf text under heavier load: searches of very common words stay over budget whatever the ranking size, since the GIN bitmap of the word is built first, and `values: false` makes neighborhood responses a third of the size but not faster; [performance](performance.md#search-on-very-common-terms))
- [x] Backup and restore guide, audit chain verification, signed images (Domain bundles and `pg_dump` in [operations](operations.md#backup-and-restore-of-a-domain); `python -m lorebase_keeper audit verify`, `GET .../audit/verify`, `lorebase audit verify` and a daily Scribe job with an alert, [operations](operations.md#audit-chain-verification); images signed with cosign by the release workflow, [release](release.md))
- [x] HTTP JSON Source with cursor, page, offset and Link header pagination ([scribe](scribe.md#sources)); landed early
- [x] LinkML schema import and export (`lorebase schema export --format linkml`, `lorebase schema import FILE --from linkml`; round trips through `lorebase.*` annotations, [LinkML](linkml.md))
- [x] Documentation site at [aantenore.github.io/lorebase](https://aantenore.github.io/lorebase/), built with `mkdocs build --strict` (`make docs`) and published by the Docs workflow
- [ ] API stability guarantees for `/api/v1` (the policy is written, [API stability](api-stability.md): what is stable, compatible and breaking changes, deprecation, client compatibility, MCP tools and the schema format; it binds from 1.0, and Keeper does not emit `Deprecation` or `Sunset` headers yet since nothing is deprecated)
- [x] Threat model published and the security review checklist completed ([threat model](threat-model.md): STRIDE per trust boundary with mitigations and residual risks; [security review](security-review.md): 23 findings, all fixed, none open)
- [x] Full proof scenario (days 1 to 5) as an automated end-to-end test: the five-day demo asserts every day against a real stack with the fake inference provider (a document extracted with the deterministic tiers, the agent over MCP writing through a Proposal), and the End-to-end workflow runs it on pushes to `main`, nightly and on relevant pull requests ([demo](demo.md#assertions-and-ci))

## Next: knowledge that stays correct

Designed in [section 18 of the design](design.md#next-knowledge-that-stays-correct); the items not ticked are not implemented yet.

- [x] Corrections that stick ([design 9.1](design.md#91-structured-sources), AC-34): per-Source last-written state (`source_written`); a run writes only Attributes that changed upstream; an upstream change over someone else's value is a `DV1` with a preserved base, a snapshot deletion of a Record with others' values a `DV2`; Source setting `local_edits: review | keep | source_wins` (default `review`); seeding on first sight for existing Sources; audit `source.local_edit_conflict` (migration `0040`, [Scribe](scribe.md#corrections-that-stick))
- [x] Extraction hold `local_edit`: value items over a value a person set are never accepted at once ([design 9.1](design.md#91-structured-sources))
- [ ] Incremental Sources: `last_seen_run` per Source-owned Record and `stale` candidates after a Source's `stale_after` (follow-up, design only)
- [x] Document versions and lifecycle ([design 9.2](design.md#92-unstructured-extraction), AC-35): optional `source_key` on upload, versions with `replaces` (one active version per key, its bytes unique per key), `GET .../documents?source_key=` and `GET .../documents/{doc}/versions`; the `document_version_check` job raising `stale` candidates (`evidence_gone`) when an old quote is gone; retirement (`document.retire`) and purge (`document.purge`, admin only); audit `document.version`, `document.retire`, `document.purge` (migration `0041`, [extraction](extraction.md#document-versions-and-lifecycle))
- [x] Ingestion modes ([design 9.2](design.md#92-unstructured-extraction), AC-36): `extraction.mode: review_all | doubts_only | direct` per Domain and per `source_key` prefix, tightening always allowed, loosening per `scribe.extraction.domain_may_loosen`; `direct` only for all-tier-0 jobs with nothing pending, audited `proposal.merge` with `mode: direct` ([extraction](extraction.md#ingestion-modes))
- [x] Stewards ([design 10.7](design.md#107-stewards), AC-37): `stewards` on the Domain and per Kind, computed assignees, `assigned=me|any|none` on Proposal and reconciliation lists (REST, CLI `--mine`, web), protection rule `required_approvals_from_stewards` (migration `0042`, [operations](operations.md#ingestion-modes-and-stewards-after-the-upgrade))
- [x] Traits as search targets and context-pack seeds (`traits`), and lenses (`lenses` in the schema; `lens` on context packs, search and MCP) ([design 8.8](design.md#88-read-presets-and-filtered-vector-search), AC-38)
- [x] Filtered vector search that keeps its limit: `hnsw.iterative_scan = relaxed_order` on pgvector 0.8 or later (`search.semantic.iterative_scan`, `max_scan_tuples`), over-fetch otherwise (`overfetch_factor`), `semantic_truncated` when the scan bound is reached ([design 8.8](design.md#88-read-presets-and-filtered-vector-search))

## Not on the roadmap

These are written down as non-goals or anti-goals in the [design](design.md#2-goals-non-goals-anti-goals): a model in the agent read path, direct model writes to protected branches, schemaless writes, a second required datastore, an agent runtime, an ETL platform, BI dashboards, OWL reasoning, multi-region active-active writes, environment and release management (versions of systems are Records, tags or validity periods; release tooling stays the source), key aliases after duplicate merges, and live federated reads across Sources or Domains.

## How to influence it

Open a [discussion](https://github.com/aantenore/lorebase/discussions) with the domain you would model and what your agents need to read or change. Real scenarios move items up. Design changes are proposed as issues that cite the section of the design they challenge; see [CONTRIBUTING.md](../CONTRIBUTING.md).
