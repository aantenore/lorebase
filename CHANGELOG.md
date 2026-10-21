# Changelog

What changed in each release, newest first. The notes are written for the person deciding whether to upgrade; the rules are in [docs/launch/changelog-style.md](docs/launch/changelog-style.md). Lorebase follows semantic versioning. Before 1.0, any minor version may change an interface, and the notes say so.

## 0.1.0

First release. Lorebase keeps a typed ontology in Postgres and lets teams change it on branches, review the change in a Proposal and merge schema and data together, with Conflicts resolved per Attribute. Agents read through a REST API that never calls a model, or through the MCP server, and write only through Operations. Pre-1.0: `/api/v1` is not frozen and interfaces may change in a minor version.

### Added

Schema and versioning ([design](docs/design.md), [schema as code](examples/schema_as_code.md))

- A Domain is a YAML schema document (`format: lorebase.schema/v1`) with Kinds, Attributes, Relations, Traits, Operations, Derivations and Sources, checked statically and applied as one schema change set: `POST /api/v1/domains/{domain}/branches/{branch}/schema/changes`, `lorebase schema validate|diff|apply`. Attribute ids are stable, so a rename keeps its history.
- Content-addressed Revisions, branches, tags, diff and time-travel reads (`?at=<revision>`, `lorebase diff`, `lorebase record get --at`).
- Three-way merge of schema and data with typed Conflicts (attribute value, delete or modify, add or add, missing value, type coercion, cardinality, dangling Link and more), resolved with `ours`, `theirs`, `base` or a custom value, one by one or from a file (`lorebase proposal conflicts|resolve`). Merge policies `prefer`, cascade and defaults. The merged data is validated against the merged schema before the commit.
- Proposals with reviews, required approvals (`keeper.proposals`), Conflict resolution and merge over REST and CLI (`lorebase proposal create|review|merge|close`). The default branch of a Domain is protected (`keeper.domains.protect_default_branch`): changes reach it through a Proposal.
- Expressions in CEL for Operations (preconditions, steps, review policies) and for `expression` and `aggregate` Derivations, type checked when a schema is applied (`schema.expressions_enabled`) and recomputed in the materialized head on every commit. `path` Derivations are not computed.

Reads for people and agents ([agents](docs/agents.md), [performance](docs/performance.md))

- Records, Links, the JSON query DSL, traversal, neighborhoods and lexical search over REST (`lorebase record`, `query`, `search`), with ETags and the serving Revision in every response. Semantic search takes vectors supplied by the caller; nothing computes embeddings.
- Context packs within a token budget (`POST .../context`, `lorebase context`), Record explanations with the origin of each value (`GET .../records/{key}/explain`, `lorebase explain`) and data health reports (`GET .../health`, `lorebase health`), all computed without a model.
- A change feed as Server-Sent Events and as polling (`GET .../branches/{branch}/events`, `.../changes`) and signed webhooks (`POST .../webhooks`, `Lorebase-Signature` HMAC).
- Per-Attribute indexes and security definer read functions that keep Domain isolation under row-level security. At 99,900 Records and 302,940 Links on one laptop, the measured p95 of every agent read stays within its budget ([performance](docs/performance.md)); the 1M-Record baseline is not measured.

Writes and ingestion ([quickstart](docs/quickstart.md), [scribe](docs/scribe.md), [inference](docs/inference.md))

- Built-in Operations `import_records`, `import_links` and `import_batch` with dry run and idempotency keys, and custom Operations declared in the schema (`POST .../operations/{operation}/execute`).
- `lorebase import records|links|batch` load CSV, TSV, JSON Lines or JSON (Parquet with the `parquet` extra): types are converted from the schema, a Record travels with its Links, files are cut into batches of one Revision each.
- Schema inference from CSV or TSV files and from Postgres tables (`POST .../schema:infer`): Kinds, typed Attributes, keys, Relations with cardinality, a confidence and the evidence for every decision. It proposes and writes nothing.
- CSV and Postgres Sources with snapshot and incremental syncs run by Scribe, the worker and scheduler on the Postgres job queue. A sync commits on a Source branch, opens a Proposal and applies the Source's merge policy (`proposal`, `auto_when_clean`, `auto_with_authority`); every changed value keeps its row as Lineage (`lorebase sources sync|runs`). Parquet and HTTP Sources do not exist.
- Inference providers behind one OpenAI-compatible interface for chat and embeddings, plus local fastembed, GLiNER and GLiREL (optional extras), and compose profiles for Ollama and a LiteLLM proxy. Nothing in the ingestion path calls them yet.

Agents and access ([MCP](docs/mcp.md), [operations](docs/operations.md))

- Herald, the MCP server, over stdio and Streamable HTTP with 11 tools (`lorebase_context`, `lorebase_query`, `lorebase_execute` with dry run and more), two resources and one prompt. It is a thin layer over Keeper and holds no database credentials.
- API keys (hashed with a server-side pepper), OIDC bearer tokens, role grants per Domain, Kind, Operation and branch, and a hash-chained audit log (`lorebase apikey`, `GET .../audit`).
- Rate limits per principal and request class (`RateLimit-*` headers, `429` with `Retry-After`) and daily quotas per API key, shown by `lorebase whoami` (`keeper.limits.rate`, `keeper.limits.quota`).
- Domain backup and restore bundles (`python -m lorebase_store.bundle export|import`) and garbage collection of unreachable Revisions with a dry run (`python -m lorebase_store.gc`).

Clients and web app ([web app](apps/web/README.md), [tour](docs/tour.md))

- The `lorebase` command line and a Python client, sync and async, checked against the committed OpenAPI document ([docs/openapi.json](docs/openapi.json)). Exit codes are documented: 0 success, 1 usage or not found, 2 validation or conflict, 3 authentication or permission, 4 server error.
- A web app with Explorer, Canvas, Modeler (including a Start from data wizard over schema inference), Review with a Conflict resolver, Sources and Admin screens. It runs on a mock API; against a live Keeper it is not verified.
- The five-day demo: `make demo` brings up a dedicated compose stack and runs the proof scenario of [design 3.2](docs/design.md#32-the-5-day-proof-scenario) with the CLI, in under a minute once the image is built ([docs/demo.md](docs/demo.md)).

Deployment ([deployment](docs/deployment.md), [kubernetes](docs/kubernetes.md))

- Docker compose for Postgres 16 with pgvector, migrations, Keeper and Scribe, with profiles `local-models`, `litellm`, `observability` and `scale-out` (a second Keeper, a Postgres streaming replica and nginx; read replica routing with a freshness guard).
- A Helm chart (`deploy/helm/lorebase`) for Keeper, a migration hook, Scribe, Herald, the web app, Ollama, Ingress, autoscaling and read replica DSNs. It passes `helm lint --strict`, `helm template` and kubeconform; it has not run on a live cluster.
- Release artifacts: images `ghcr.io/aantenore/lorebase` (Keeper, Scribe and Herald share it; the command picks the service) and `ghcr.io/aantenore/lorebase-web`, signed with cosign keyless and carrying a CycloneDX SBOM attestation; CycloneDX SBOMs and the Helm chart package on the GitHub release. The nine Python workspace packages go to PyPI only when the maintainers configured publishing.

### Not done yet

- Lineage and history endpoints beyond the lineage summary on Record reads.
- The 1M-Record performance baseline and the 10M-Record targets of the design.
- Merge policies `authority` and `newest_wins`.
- Parquet and HTTP JSON Sources; document upload, parsing and tiered extraction; embedding jobs and hybrid search; `path` Derivations.
- Groups, sensitivity masking, the Operation outbox, overlay branches and the PII vault.
- Web app: OIDC login, a verified live mode, and reading its configuration at run time. The `lorebase-web` image is built in mock mode; the chart's `web.config` is written to `/config.js` but the app does not read it yet.
- A documentation site, LinkML import and export, and the stability promise for `/api/v1` that comes with 1.0.

### Upgrade notes

There is no earlier release. For a new installation, run the migrations (`alembic upgrade head`, migrations `0001` to `0006`) before Keeper starts: the compose `migrate` service and the Helm hook do it. Keeper's shipped configuration accepts every request as an administrator (`keeper.auth.mode: none`); use `api_key` or `oidc` for anything shared, and give `api_key` modes a pepper of at least 32 characters.
