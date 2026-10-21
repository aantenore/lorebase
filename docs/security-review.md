# Security review before the first release

Review of the trust boundaries of Lorebase at commit `4bd2d26`, with the fixes made on top of it.
Each fixed finding has its own commit with a test that fails without the fix. The findings left
open by the first pass (F6, F7, F12 to F23) were fixed in a second pass, described below.

## Scope and method

Packages `keeper`, `store`, `scribe`, `inference` and `herald`, the deployment files under
`deploy/`, the environment and example configuration, and the GitHub workflows. The security
sections of [design.md](design.md#13-security-and-multi-tenancy) and
[store-schema-notes.md](store-schema-notes.md#row-level-security) give the intended model.

Boundaries examined:

| Boundary | What was checked |
|----------|------------------|
| Unauthenticated to authenticated | auth modes and their required settings, API key parsing and HMAC, OIDC JWKS validation, bind address of `auth.mode: none`, body limits, problem details |
| Narrowed grant to Domain-wide | RBAC matrix, Kind, Operation and branch scoped grants, `ReadGuard`, response cache variants, idempotency keys per principal |
| Domain to installation | Domain id handling in the authorizer, API key scopes, installation administrators, Source connections, schema inference sources |
| Agent to human-reviewed write | Operation review policies, Proposal approvals and self-approval, merge permission on the target branch |
| External data to model prompt | inference chat client, structured output validation, local span extraction |
| Keeper to Postgres role | row-level security policies, revision `0005` security definer functions, maintenance jobs, role separation |

Tools: `make security` (pip-audit on the lock file, `pnpm audit --audit-level=high`),
`uvx bandit -r packages -q -ll`, `pnpm audit` in `apps/web`, and a grep of every logging call
near tokens, keys, DSNs and secrets.

## Findings

| ID | Severity | Area | Finding | Status |
|----|----------|------|---------|--------|
| F1 | Critical | scribe | A Source could resolve any secret reference, including the Lorebase database DSN | Fixed |
| F2 | High | scribe | Source errors echoed the resolved value of a secret reference | Fixed |
| F3 | High | keeper | `auth.mode: none` could be served on a non-loopback bind address | Fixed |
| F4 | Medium | store | Webhook targets in shared address space and other non-global ranges were accepted | Fixed |
| F5 | Medium | scribe | A webhook target could stall delivery for every Domain | Fixed |
| F6 | Medium | deploy | The compose stack connects as the Postgres superuser, which ignores row-level security | Fixed |
| F7 | Medium | deploy | Compose falls back to `change-me` for the LiteLLM master key and the Grafana password | Fixed |
| F8 | Low | keeper | A Domain id of `*` in a path was authorized against grants in any Domain | Fixed |
| F9 | Low | store | Index drop jobs accepted any index name | Fixed |
| F10 | Low | inference | A per-task endpoint inherited the main API key | Fixed |
| F11 | Low | CI | Checkouts kept the job token in `.git/config` | Fixed |
| F12 | Low | store | Security definer functions accept NULL limits and do not check the transaction scope | Fixed |
| F13 | Low | store | Revision `0005` does not verify that the table schema is not writable by the application role | Fixed |
| F14 | Low | store | Bundle import has no size limits and trusts unhashed metadata | Fixed |
| F15 | Low | keeper | Schema inference sources are installation-wide | Fixed |
| F16 | Low | keeper | The optional API key IP allowlist of design 13.2 is not implemented | Fixed |
| F17 | Low | keeper | OIDC issuer and JWKS URLs may use plain HTTP | Fixed |
| F18 | Low | scribe | CSV over HTTP checks the host name only; local CSV reads have a check-then-read race | Fixed |
| F19 | Low | deploy | Helm NetworkPolicy is disabled by default; `/api/v1/docs` is served by default | Fixed |
| F20 | Low | keeper | Requests with invalid credentials are not rate limited | Fixed |
| F21 | Info | inference | Provider error text (up to 300 characters) is kept in exception messages; clients honour proxy variables | Fixed |
| F22 | Info | store | `rls_scope` does not refuse commas in Domain ids; `sql_literal` assumes `standard_conforming_strings` | Fixed |
| F23 | Info | keeper | Server-Sent Event streams are authorized once, at connection | Fixed |

### Fixed

**F1. Source connections are limited to an allowlist** (`packages/scribe/src/lorebase_scribe/sources/connectors.py`).
Sources are defined in the schema by callers holding `schema.write`. Scribe resolved whatever
secret reference a Source named, so a modeler of one Domain could define a Postgres Source on the
reference holding the Lorebase DSN and copy any table of the installation into their Domain; the
policies rely on a transaction setting that such a query can change. Scribe now resolves only the
references in `scribe.sources.allowed_connections` (empty by default).
Test: `packages/scribe/tests/test_source_connections.py`.

**F2. Source errors never contain secret values** (same file). A CSV Source whose reference held
another secret failed the allowed-roots check with the value in the message, which is stored in
the run record and returned by `GET /domains/{d}/sources/{s}/runs`. Messages now name the setting
or the reference only. Test: `test_source_connections.py`.

**F3. Anonymous mode is refused off loopback** (`packages/keeper/src/lorebase_keeper/settings.py`).
`auth.mode: none` accepts every request as an installation administrator. Keeper now refuses to
start with that mode and a `keeper.host` that is not loopback unless `keeper.auth.allow_insecure`
is true. The compose stack sets the flag through `KEEPER_ALLOW_INSECURE`, since it publishes the
port on `127.0.0.1`. Test: `packages/keeper/tests/test_settings.py`.

The pepper (at least 32 characters) is required with `api_key` and `api_key+oidc`, and
`oidc.issuer` with `oidc.audience` with `oidc` and `api_key+oidc` (`auth/config.py`,
`_mode_requirements`); the Helm chart also refuses to render an API key mode without a pepper.

**F4. Webhook targets must be global unicast** (`packages/store/src/lorebase_store/webhooks/delivery.py`).
The check missed 100.64.0.0/10 (which holds an instance metadata address on some clouds), IETF
protocol assignments and IPv6 site-local addresses. Only addresses Python classifies as global,
and not multicast, reserved or site-local, are accepted. Test: `packages/store/tests/test_webhooks.py`.

**F5. Webhook attempts are bounded and bodies are not read** (`packages/scribe/src/lorebase_scribe/webhooks.py`).
`client.post` read the whole response with per-read timeouts; deliveries run in sequence, so one
target streaming or trickling a body blocked delivery for all Domains. Each attempt now runs under
`asyncio.timeout` and closes the streamed response unread. Test: `packages/scribe/tests/test_webhook_sender.py`.

**F8. `*` is an ordinary Domain id** (`packages/keeper/src/lorebase_keeper/auth/rbac.py`). The
authorizer selected its any-Domain grant lookup by comparing the requested Domain id with `*`, so
`/domains/*/...` was authorized with the caller's grants anywhere. The routes found no such Domain,
so no data crossed. The lookup is now selected by `None`. Test: `packages/keeper/tests/auth/test_rbac.py`.

**F9. Index drop jobs drop managed indexes only** (`packages/store/src/lorebase_store/indexes.py`).
Jobs are queued by the application role and run by the maintenance role; a drop job now runs only
for a per-Attribute index of its Domain. Test: `packages/store/tests/test_indexes.py`
(`test_a_drop_job_only_drops_managed_indexes_of_its_domain`).

**F10. Task endpoints use their own key** (`packages/inference/src/lorebase_inference/settings.py`).
A task override with another `base_url` and no `api_key` sent the main key to that host. It now
gets no key unless it sets one. Test: `packages/inference/tests/test_settings_providers.py`.

**F11. CI checkouts do not persist credentials** (`.github/workflows/*.yaml`). Every
`actions/checkout` sets `persist-credentials: false`; no job fetches or pushes afterwards.

### Fixed in the second pass

Each fix has its own commit with a test that fails without it (F21 to F23 are one commit each).

**F6. Keeper and Scribe connect as a role subject to RLS on compose**
(`deploy/compose/docker-compose.yaml`, `deploy/compose/postgres/app-role.sh`). A one-shot
`db-roles` service creates or updates `LOREBASE_APP_USER` (default `lorebase_app`, password from
the required `LOREBASE_APP_PASSWORD`) on every start: `LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
NOCREATEROLE NOREPLICATION`, row rights on the tables and sequences of `public` (default
privileges cover later tables), `CONNECT, TEMPORARY` on the database, `EXECUTE` on the read
functions of an earlier migration; `CREATE` on `public` is revoked from `PUBLIC`. It is
idempotent, so existing volumes are converted. Keeper and Scribe use that role; `migrate` and
the index maintenance DSN keep the owner, and `migrate` sets `database.application_role`. A
one-shot service was chosen over a `docker-entrypoint-initdb.d` script because init scripts run
only on an empty volume. Roles are documented in [deployment.md](deployment.md#database-roles).
Test: `tests/tooling/test_compose_stack.py`; the web e2e suite passed on the stack (11 tests),
where `pg_stat_activity` showed Keeper as `lorebase_app` with `rolsuper` and `rolbypassrls`
false, and Scribe ran its index and webhook jobs under the role. The Helm chart still runs the
migration and the services with one DSN; deployment.md says to give that role neither
`SUPERUSER` nor `BYPASSRLS`.

**F7. No placeholder secrets for the LiteLLM and Grafana profiles**
(`deploy/compose/docker-compose.yaml`, `deploy/compose/require-secrets.sh`, `.env.example`).
The `change-me` defaults are gone and `.env.example` leaves both values empty with a generation
command. `${VAR:?}` fails for every profile, because compose interpolates every service, so
`litellm` and `grafana` depend on one-shot guards (`litellm-secrets`, `grafana-secrets`) that
exit 1 and name the variable when it is empty, `change-me` or shorter than 16 characters.
Test: `tests/tooling/test_compose_stack.py`.

**F12, F13. Security definer arguments, scope and search_path**
(`packages/store/migrations/versions/0008_definer_argument_checks.py`). Revision `0008`
replaces the four functions (same signatures, owner and grants) with checks at entry: every
count between 1 and its ceiling (`store.reads.definer_ceilings`, NULL refused, SQLSTATE 22023),
the Domain given (22023) and in the transaction scope or with the bypass on (42501), and
`search_path` equal to the path fixed by the migration (55000). The upgrade refuses to run while
`database.application_role` may create objects in the schema of the tables or of `pg_trgm`.
Test: `packages/store/tests/test_definer_checks.py` (run as a non-owner role).

**F14. Bundle import is bounded** (`packages/store/src/lorebase_store/bundle.py`). Per-file and
total size caps counted on the bytes read, an entry count cap and a bounded line length
(`store.bundle.*`); tarballs accept only regular files and the `schemas` directory and refuse
links, devices, absolute and `..` paths; directory mode opens every file with `O_NOFOLLOW` at
each level. Bundles are documented as trusted input in [operations.md](operations.md).
Test: `packages/store/tests/test_bundle_limits.py`.

**F15. Schema inference sources per Domain**
(`packages/keeper/src/lorebase_keeper/services/schema_inference.py`).
`keeper.schema_inference.domain_sources` maps a Domain to its own source references (an empty
list disables Postgres inference for it); Domains not listed use `allowed_sources`. The refusal
does not name other Domains' sources. Test: `packages/keeper/tests/test_schema_inference.py`.

**F16. API key network allowlist** (`packages/keeper/src/lorebase_keeper/client_address.py`,
`auth/principals.py`, revision `0009`). Keys may carry `allowed_networks` (CIDR list, capped by
`keeper.auth.api_keys.max_allowed_networks`), set through the admin API, the client and
`lorebase apikey create --allow-network`; a key used from another address gets the 401 of an invalid
key. The client address is the socket peer, or, when the peer is in `keeper.trusted_proxies`,
the first `X-Forwarded-For` hop from the right that is not a trusted proxy; uvicorn no longer
rewrites the peer from proxy headers. Tests: `packages/keeper/tests/test_client_address.py`,
`packages/keeper/tests/auth/test_api_key_authentication.py`.

**F17. OIDC over HTTPS only** (`packages/keeper/src/lorebase_keeper/auth/config.py`, `oidc.py`).
`oidc.issuer`, `oidc.jwks_uri` and a discovered `jwks_uri` must use `https` unless the host is
loopback or `keeper.auth.oidc.allow_insecure` is set.
Test: `packages/keeper/tests/auth/test_oidc.py`.

**F18. CSV Source reads** (`packages/scribe/src/lorebase_scribe/sources/connectors.py`). URL
hosts go through the webhook target check (global unicast only, unless
`scribe.sources.csv.allow_private_targets`) and the download connects to the checked addresses;
`timeout_s` bounds the whole download and the body is capped by `max_bytes`. Local files are
resolved and checked against the resolved roots, then opened from the root one component at a
time with `O_NOFOLLOW`, must be regular files and are read from that descriptor up to
`max_bytes`. Test: `packages/scribe/tests/test_connectors.py`.

**F19. Helm network policy on by default** (`deploy/helm/lorebase/values.yaml`,
`templates/networkpolicy.yaml`). Ingress closed except Keeper, Herald, web and Ollama from Scribe;
egress per workload: DNS, Postgres (in-cluster pods or `networkPolicy.egress.database.to`), the
OTLP collector when telemetry is on, the model endpoint for Scribe, webhook ranges, Herald and
the test pod to Keeper only. No default rule opens every address; the production profile lists
placeholder ranges and turns `keeper.openapi.docs_enabled` off, as [kubernetes.md](kubernetes.md)
recommends. Test: `tests/tooling/test_helm_network_policy.py`.

**F20. Failed authentications are rate limited**
(`packages/keeper/src/lorebase_keeper/ratelimit/enforce.py`, `limiter.py`). A bucket class of
its own, `keeper.limits.rate.auth_failures` (30 per minute, burst 30), with one bucket per
client address (IPv6 by /64) and one per API key prefix. A 401 to a request that carried a
credential takes a token from both; while either is empty the request gets 429 with
`Retry-After` before the prefix lookup, the HMAC or the token validation, stays out of the audit
log and is logged at DEBUG. Successes take nothing. Trade-off, documented in
[operations.md](operations.md): whoever knows a key prefix (it is not secret) can keep that key
throttled, and clients behind one address share its bucket. Tests:
`packages/keeper/tests/auth/test_auth_failure_limits.py`,
`packages/keeper/tests/ratelimit/test_rate_limit_buckets.py`.

**F21. Inference client details** (`packages/inference/src/lorebase_inference/transport.py`).
401 and 403 errors keep the status and a fixed message only; other excerpts are scrubbed of
the configured key, `Bearer` values, `sk-`-style keys and long base64 or hex runs. httpx clients
of inference and Herald ignore proxy variables unless `trust_env` is set
(`inference.llm.trust_env`, `inference.embeddings.trust_env`, `herald.keeper.trust_env`); with it,
`NO_PROXY` is honoured as usual. Tests: `packages/inference/tests/test_transport.py`,
`packages/herald/tests/test_safety.py`.

**F22. Store literals** (`packages/store/src/lorebase_store/auth/scope.py`, `db.py`). `rls_scope`
and `scope_to_domain` share one check that refuses empty ids and ids with a comma; every store
engine sets `standard_conforming_strings: on`. Tests: `packages/store/tests/auth/test_rls_scope.py`,
`packages/store/tests/test_settings.py`.

**F23. Event streams are authorized again** (`packages/keeper/src/lorebase_keeper/agents/feed.py`,
`routes/events.py`). Every `agents.feed.reauthorize_interval_s` (default 60 s) an open stream
authenticates its request again (with its client address, for key allowlists) and requires
`data.read` on the branch; it closes when either fails. The check uses the usual caches, so a
grant changed through another Keeper is seen within the interval plus
`keeper.auth.authz.grants_cache_ttl_s`. Test:
`packages/keeper/tests/integration/test_read_authorization.py`.

Remaining limits: the per-prefix bucket of F20 can be used to throttle a known key; the Helm
chart has one DSN for the migration and the services (F6); bundle headers of pax and GNU long
names are bounded by the archive size only (F14); the `O_NOFOLLOW` walk of CSV and bundle files
is POSIX only (F14, F18); schema inference sources are bound to Domains but not to schemas or
tables (F15).

## Other observations

- **Prompt injection.** The extraction model gets no tools, its output is validated against a
  JSON schema with bounded repair attempts, and local span extraction takes evidence text from
  the source by checked offsets. No extraction prompt builder exists yet in `packages/inference`;
  when it is added, document text must go in the user message as delimited data, never in the
  system prompt, and outputs must stay Proposals that need review.
- **Webhook signing key.** The stored value is the HMAC key itself (documented); a database
  reader can sign deliveries. Encrypting it with a configured key would close that.
- **Herald** forwards only the caller's credential in HTTP mode, never logs it, maps Keeper
  problems to short messages, and caps tool output except the context pack, which Keeper bounds
  by its token budget.

## Tool results

- `make security`: pip-audit found no known vulnerabilities; `pnpm audit --audit-level=high` found none.
- `pnpm audit` in `apps/web` (all severities): no known vulnerabilities.
- `uvx bandit -r packages -q -ll`: 42 medium, 0 high. 39 are B608 (SQL built as strings) in
  `store/reads.py`, `store/traverse.py`, `store/gc.py`, `store/indexes.py`, `store/jobs.py`,
  `scribe/sources/connectors.py`, `core/expr/checker.py` and tests: each interpolates constants,
  whitelisted keywords, checked identifiers or quoted literals, or (Postgres Sources) SQL written
  by the schema's modelers and run read-only; the checker hit is an error message. B506 in
  `core/model/yaml_schema.py` is a `SafeLoader` subclass. B104 hits are in tests. No real hit.
- Logging: no call at INFO or above emits a token, key, pepper, DSN or password. Rejected API keys
  log the public prefix, rejected tokens the PyJWT error, schema inference the secret reference;
  the redaction filter also scrubs every resolved secret value from log records.
