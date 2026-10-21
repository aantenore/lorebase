# Changelog

What changed in each release, newest first. The notes are written for the person deciding whether to upgrade; the rules are in [docs/launch/changelog-style.md](docs/launch/changelog-style.md). Lorebase follows semantic versioning. Before 1.0, any minor version may change an interface, and the notes say so.

## 0.1.0

First release. Lorebase keeps a typed ontology in Postgres and lets teams change it on branches, review the change in a Proposal and merge schema and data together, with Conflicts resolved per Attribute. Agents read through a REST API that never calls a model, or through the MCP server, and write only through Operations; a write that needs review becomes a task branch and a Proposal. Documents are extracted into Proposals whose items reviewers accept or reject. Pre-1.0: `/api/v1` is not frozen and interfaces may change in a minor version.

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
- Document intake and tiered extraction into Proposals ([extraction](docs/extraction.md)): `POST .../documents` takes Markdown, plain text, PDF and DOCX (PDF and DOCX with the scribe `documents` extra); Scribe parses and chunks them and proposes Records, Links and values with verified evidence, from rules (tier 0, no model), local GLiNER and GLiREL (tier 1) or any OpenAI-compatible model with JSON schema output (tier 2, cached, token budget per job). Reviewers accept, edit or reject items one by one or by filter (`GET .../proposals/{p}/items`, `POST .../items:decide`, `lorebase documents upload`, `lorebase proposal items|decide`); nothing reaches the default branch without a merge. Derived and sensitive Attributes are never extracted. The web app has no screen for extraction items yet.
- Inference providers behind one OpenAI-compatible interface for chat and embeddings, plus local fastembed, GLiNER and GLiREL (optional extras), and compose profiles for Ollama and a LiteLLM proxy. Extraction uses them; nothing computes embeddings yet.

Agents and access ([MCP](docs/mcp.md), [operations](docs/operations.md))

- Herald, the MCP server, over stdio and Streamable HTTP with 11 tools (`lorebase_context`, `lorebase_query`, `lorebase_execute` with dry run and more), two resources and one prompt. It is a thin layer over Keeper and holds no database credentials.
- Agent writes that need review ([agents](docs/agents.md#writes-that-need-review)): an Operation refused with `409 review-required` (protected branch or review policy) runs, when the call names a `task`, on a task branch `agent/{principal}/{task}` and opens or updates one Proposal, merged at once only when `keeper.agent_writes.allowed_roles_for_auto_merge` allows the caller's role, nothing conflicts and the required approvals are in. Without a `task` the `409` carries `next` hints. Settings `keeper.agent_writes`, overridable per Domain.
- API keys (hashed with a server-side pepper), OIDC bearer tokens, role grants per Domain, Kind, Operation and branch, and a hash-chained audit log (`lorebase apikey`, `GET .../audit`). `python -m lorebase_keeper bootstrap` creates the first installation administrator key from the database settings and prints it once; it refuses when an administrator already holds a usable key, unless `--force`.
- Rate limits per principal and request class (`RateLimit-*` headers, `429` with `Retry-After`) and daily quotas per API key, shown by `lorebase whoami` (`keeper.limits.rate`, `keeper.limits.quota`).
- Domain backup and restore bundles (`python -m lorebase_store.bundle export|import`) and garbage collection of unreachable Revisions with a dry run (`python -m lorebase_store.gc`).

Clients and web app ([web app](apps/web/README.md), [tour](docs/tour.md))

- The `lorebase` command line and a Python client, sync and async, checked against the committed OpenAPI document ([docs/openapi.json](docs/openapi.json)). Exit codes are documented: 0 success, 1 usage or not found, 2 validation or conflict, 3 authentication or permission, 4 server error.
- A web app with Explorer, Canvas, Modeler (including a Start from data wizard over schema inference), Review with a Conflict resolver, Sources and Admin screens. It runs on the mock API or on Keeper, with end-to-end tests against a real Keeper for browsing, Branches, schema changes, Review and Settings. It reads `/config.js` (`window.__LOREBASE_CONFIG__`: `apiMode`, `keeperBaseUrl`, an optional `apiToken` or any `VITE_*` name) over its build values, so the `lorebase-web` image switches to live mode at run time, from `VITE_*` variables of the container or the chart's `web.config`.
- The five-day demo: `make demo` brings up a dedicated compose stack and runs the proof scenario of [design 3.2](docs/design.md#32-the-5-day-proof-scenario) with the CLI, in under a minute once the image is built ([docs/demo.md](docs/demo.md)).

Deployment ([deployment](docs/deployment.md), [kubernetes](docs/kubernetes.md))

- Docker compose for Postgres 16 with pgvector, migrations, Keeper and Scribe, with profiles `local-models`, `litellm`, `observability` and `scale-out` (a second Keeper, a Postgres streaming replica and nginx; read replica routing with a freshness guard).
- A Helm chart (`deploy/helm/lorebase`) for Keeper, a migration hook, Scribe, Herald, the web app, Ollama, Ingress, autoscaling and read replica DSNs. It passes `helm lint --strict`, `helm template` and kubeconform; it has not run on a live cluster.
- Release artifacts: images `ghcr.io/aantenore/lorebase` (Keeper, Scribe and Herald share it; the command picks the service) and `ghcr.io/aantenore/lorebase-web`, signed with cosign keyless and carrying a CycloneDX SBOM attestation; CycloneDX SBOMs and the Helm chart package on the GitHub release. The nine Python workspace packages go to PyPI only when the maintainers configured publishing.

### Security

- A security review of the trust boundaries before this release ([docs/security-review.md](docs/security-review.md)) fixed all 23 findings, among them: a Source could resolve any secret reference, including the database DSN (critical), Source errors echoed resolved secrets, `keeper.auth.mode: none` could be served on a non-loopback address (now refused unless `keeper.auth.allow_insecure`), webhook targets in non-global address ranges were accepted, and a slow webhook target could stall delivery for every Domain.
- Compose: Keeper and Scribe connect as an application role without superuser or `BYPASSRLS` (`LOREBASE_APP_USER`, `LOREBASE_APP_PASSWORD`, created by the one-shot `db-roles`), so row-level security applies to them; the owner role runs the migrations and index maintenance. The `litellm` and `observability` profiles refuse to start while `LITELLM_MASTER_KEY` or `GRAFANA_ADMIN_PASSWORD` is empty, `change-me` or shorter than 16 characters.
- Migration `0008` adds argument, scope and `search_path` checks to the security definer read functions and refuses to run while the application role may create objects in their schemas; migration `0009` adds client network allowlists to API keys (`allowed_networks`, `lorebase apikey create --allow-network`, behind `keeper.trusted_proxies`).
- Bundle import is bounded and refuses links, schema inference sources are configured per Domain, the OIDC issuer and signing keys need `https`, CSV Sources check the resolved address and open files without following links, failed authentications are rate limited per client address and key prefix, the Helm NetworkPolicy is on by default with narrow egress, provider error text is scrubbed, and open event streams are authorized again at an interval.

### Not done yet

- Lineage and history endpoints beyond the lineage summary on Record reads.
- The 1M-Record performance baseline and the 10M-Record targets of the design.
- Merge policies `authority` and `newest_wins`.
- Parquet and HTTP JSON Sources; embedding-based entity resolution, schema suggestions and OCR for documents; embedding jobs and hybrid search; `path` Derivations.
- Groups, sensitivity masking, the Operation outbox, overlay branches and the PII vault.
- Web app: OIDC login and a screen for extraction items.
- A documentation site, LinkML import and export, and the stability promise for `/api/v1` that comes with 1.0.

### Upgrade notes

There is no earlier release. For a new installation, run the migrations (`alembic upgrade head`, migrations `0001` to `0009`) before Keeper starts: the compose `migrate` service and the Helm hook do it. Keeper's shipped configuration accepts every request as an administrator (`keeper.auth.mode: none`); use `api_key` or `oidc` for anything shared, give `api_key` modes a pepper of at least 32 characters, list the administrator in `keeper.auth.installation_admins` and create the first key with `python -m lorebase_keeper bootstrap`.
