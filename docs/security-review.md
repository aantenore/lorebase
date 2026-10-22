# Security review before the first release

Review of the trust boundaries of Lorebase at commit `4bd2d26`, with the fixes made on top of it.
Each fixed finding has its own commit with a test that fails without the fix. The findings left
open by the first pass (F6, F7, F12 to F23) were fixed in a second pass, described below.

The [threat model](threat-model.md) covers the same boundaries and the ones added since (agents
over MCP, webhooks, inference providers, the release pipeline) by STRIDE category, with the
mitigation of each threat and the risk that remains.

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
| Browser to Keeper (added with web sign-in, below) | session cookie, sign-in state, CSRF, return paths, token storage, refresh and sign-out |

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
false, and Scribe ran its index and webhook jobs under the role. The Helm chart has the same
split since the [hardening from the threat model](#hardening-from-the-threat-model).

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
Revision `0010` (overlay branches) creates the four `_overlay` variants with the same checks,
run before the overlay's rows are resolved, and the same refusal.
Test: `packages/store/tests/test_definer_checks.py` (run as a non-owner role, both sets).

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
throttled, and clients behind one address share its bucket. Operators who cannot accept that set
`auth_failures.per_key_prefix_enabled: false`: only the per-address bucket (always on) is kept.
Tests:
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

Remaining limits: the per-prefix bucket of F20 can be used to throttle a known key
(`per_key_prefix_enabled: false` removes it); bundle headers of pax and GNU long
names are bounded by the archive size only (F14); the `O_NOFOLLOW` walk of CSV and bundle files
is POSIX only (F14, F18); schema inference sources are bound to Domains but not to schemas or
tables (F15).

## Web sign-in: a new boundary

Web sign-in (`keeper.auth.sessions`, migration `0013`) adds a credential the browser sends by
itself: the session cookie. Code: `packages/keeper/src/lorebase_keeper/auth/sessions.py`,
`auth/provider.py`, `auth/keyring.py`, `routes/sessions.py`; storage in
`packages/store/src/lorebase_store/auth/sessions.py`. Setup and the full list of checks are in
[deployment.md](deployment.md#web-sign-in). Threats considered and what answers them:

| Threat | Mitigation |
|--------|------------|
| Token theft by script in the page (XSS) | The browser gets no token: the session id is in an `HttpOnly` cookie; provider tokens stay in Keeper |
| Session id theft from the database or backups | Only its SHA-256 is stored; refresh and ID tokens are AES-256-GCM encrypted under a key derived (HKDF) from `encryption_key`, which is a secret reference, never stored |
| Cookie injection from a sibling host, or over http | `__Host-` prefix with `Secure`, `Path=/` and no `Domain` (unless `cookie_secure: false`, meant for loopback development) |
| Cross-site request forgery | `SameSite=Lax`; unsafe methods also need the CSRF header (HMAC of the session id, compared in constant time), a trusted `Origin` when present and no cross-site `Sec-Fetch-Site`; requests with `Authorization` or an API key ignore the cookie and need no token |
| Login CSRF, code injection, replayed ID token | `state` and `nonce` (256 bits each) and the PKCE verifier live in an encrypted, 10-minute, `HttpOnly` cookie bound to the browser that started; the callback checks the state before redeeming the code, the provider checks the verifier, Keeper checks the ID token's signature, issuer, audience, `azp`, expiry and nonce |
| Session fixation | A new random id at every sign-in; the session the browser presented is deleted |
| Open redirect | `return_to` must be a local path (no scheme, authority, backslash or control character, at most 2,048 characters) and is checked again when the state cookie is opened; redirects go to `app_url`'s origin only; provider error codes are mapped to a fixed list |
| Long-lived or orphaned sessions | Idle and absolute timeouts checked on every request; refresh at the provider when the access token expires, and a refused refresh, a changed subject or an unreadable token blob ends the session; sign-out deletes the row and offers RP-initiated logout; rows go with their principal |
| Redirect URI manipulation | The configured `redirect_uri` is sent verbatim to the authorization and token endpoints (exact match at the provider); it is never derived from request headers |
| Provider metadata or endpoints over plain http | Same rule as F17: https unless loopback or `oidc.allow_insecure`; the metadata's issuer must equal `oidc.issuer`; providers that do not list S256 PKCE are refused |

The `web_sessions` table is installation-wide like `api_keys`: it has no `domain_id`, hence no
row-level security policy, and the application role reaches it through the owner's default
privileges. A session resolves to the principal a bearer token of the same subject gets, so
grants, Domain isolation and row-level security apply unchanged; `auth.mode: none` refuses a
sessions configuration. Sign-in and sign-out routes (`/auth/login`, `/auth/callback`,
`/auth/logout`) are not authenticated by the limits dependency, so a stale cookie never blocks
them; they take from a bucket per client address instead (the class of the method, `reads` or
`writes`), before any provider request or session lookup. The limits dependency names routes
without the API prefix whatever form the router reports, so `exempt_paths` and these routes
match. The CSRF check runs before the session is looked up, so a forged request neither
extends nor refreshes a session, and a bad session cookie on other routes counts as a failed
authentication for `auth_failures`. Refreshed ID tokens are checked like the first one
(signature, issuer, audience, `azp`, expiry); the subject must not change and a `nonce`, if
present, must be the sign-in's, which is kept in the encrypted token blob.

The authorization code reaches Keeper in the query string of `/auth/callback`. Keeper's access
log records the route template, not the query, but reverse proxies and load balancers usually
log full URLs: such a log holds codes that are single use, short-lived and useless without the
PKCE verifier, which only the browser's encrypted sign-in cookie carries. Keep proxy logs
protected anyway, or drop the query of that path from them.

Tests: `packages/keeper/tests/auth/test_sessions.py` (state, nonce, PKCE, audience, tampered
and expired state cookie, CSRF cases, Origin and `Sec-Fetch-Site`, idle and absolute expiry,
rotation, refresh, refused refresh, logout revocation, open redirects, cookie flags, settings),
`packages/store/tests/auth/test_sessions_integration.py` and
`packages/keeper/tests/integration/test_sessions_postgres.py` (as a role without
`BYPASSRLS`), and the web app's `src/api/session.test.ts` and `src/api/fetcher.test.ts`.

Remaining limits: the CSRF token is readable by any origin that `keeper.cors` allows with
credentials, so list only the app's origins there; sessions survive a provider outage until the
outage ends or a timeout applies; one sign-in at a time per browser (a second tab's sign-in
replaces the first one's state cookie); the per-session refresh is serialized by a lease, not a
lock, so a refresh that fails mid-way waits `refresh_lease_s` before the next attempt.

## Hardening from the threat model

The [threat model](threat-model.md) listed residual risks by priority. Each fix below has its own
commit and tests.


**Audit log append-only for the application role**
(`packages/store/migrations/versions/0015_append_only_audit_log.py`,
`deploy/compose/postgres/app-role.sh`). Revision `0015` revokes `UPDATE`, `DELETE`, `TRUNCATE`,
`REFERENCES` and `TRIGGER` on `audit_log` from `PUBLIC`, from `database.application_role` and from
every other role holding them except the owner; the compose `db-roles` script, which re-grants row
rights on every start, takes them back on `audit_log`. No code path updates, deletes or prunes
audit entries, so no maintenance-role or definer function was needed. Test:
`packages/store/tests/auth/test_audit_append_only_integration.py` (the application role appends
and reads, and gets SQLSTATE 42501 on `UPDATE`, `DELETE` and `TRUNCATE`; the downgrade gives the
rights back).

**Document URL intake checks and pins addresses**
(`packages/keeper/src/lorebase_keeper/services/document_urls.py`). After the scheme and the
`keeper.documents.url_hosts` allowlist, the host is resolved once and refused unless every
address is global unicast, with the `check_target` of webhook targets and Sources; the GET then
connects to the checked addresses only (the `Host` header and the TLS server name keep the host
name), trying the next one on a connection failure, never a new lookup. Redirects, proxies from
the environment and bodies above `max_bytes` stay refused. A resolution failure answers 502, a
private address 422. `keeper.documents.url_allow_private_targets` (false) turns the check off.
Test: `packages/keeper/tests/test_document_urls.py` (loopback, RFC 1918, link-local, shared
address space, IPv6 unique local and IPv4-mapped loopback refused before any request; the
request goes to the checked address with the original `Host` and SNI).
**PDF and DOCX parsed in a limited child process**
(`packages/scribe/src/lorebase_scribe/extraction/sandbox.py`, settings
`scribe.extraction.parser`). The extraction job hands the bytes to
`python -I -B -m lorebase_scribe.extraction.sandbox` over standard input, with an empty
environment. Before reading them the child sets `RLIMIT_CPU` (`cpu_s`, 60), `RLIMIT_AS`
(`memory_mb`, 1024, where the system enforces it) and no core files, and replaces the socket
constructors and name resolution with functions that refuse. The parent kills it after
`timeout_s` (60 s), reads a bounded answer, refuses documents over `max_input_bytes` (50 MiB)
without starting it, and turns a limit, a signal, a parser exception or a garbled answer into a
`ParseError`, a permanent job error. Markdown and text are still decoded in the worker. Test:
`packages/scribe/tests/test_parser_sandbox.py` with stand-in parsers
(`packages/scribe/tests/sandbox_targets.py`) that sleep, spin, allocate, return a huge text,
open sockets and kill themselves; real PDF and DOCX parse as before.
**Webhook signing keys encrypted at rest** (`packages/store/src/lorebase_store/webhooks/keys.py`,
`packages/store/migrations/versions/0016_sealed_webhook_keys.py`, `webhooks.encryption_key`
and `previous_encryption_keys`). The keyring of web sessions moved to
`packages/store/src/lorebase_store/keyring.py` with a namespace (Keeper's derived keys are
unchanged), so Keeper, which stores keys, and Scribe, which signs with them, share it. With a
key set, the signing key is sealed with AES-256-GCM in `webhooks.sealed_key` and `secret_hash`
is null; revision `0016` makes `secret_hash` nullable with a check that exactly one column is
set. Rows stored before keep working: each delivery pass seals up to `batch` of them, and keys
sealed under a previous key are sealed again under the current one. A key that does not open
fails the attempt with an error naming the setting. The downgrade refuses while sealed rows
exist. Tests: `packages/store/tests/test_webhook_keys.py` (sealing, rotation, tampering,
settings, unchanged session key derivation) and
`packages/store/tests/test_webhook_keys_integration.py` (a webhook created before the key is
sealed by the worker and still verifies at the receiver; rotation; an unknown key).
**Security headers from Keeper and the web image**
(`packages/keeper/src/lorebase_keeper/middleware.py`, `settings.py`,
`deploy/docker/web/40-lorebase-config.sh`, `deploy/docker/web/default.conf`,
`deploy/helm/lorebase/templates/web-configmap.yaml`). Keeper's outermost middleware adds
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` with `Content-Security-Policy:
frame-ancestors 'none'`, `Referrer-Policy: no-referrer` and, in `hsts.enabled: auto`, HSTS when
the request scheme is https or a peer in `keeper.trusted_proxies` forwarded https; a header a
route set is kept (`keeper.security_headers`). The web image writes an nginx include at start
with a Content Security Policy built from the runtime Keeper URL (`connect-src`), extra sources
(`LOREBASE_WEB_CSP_CONNECT_SRC`) or a whole replacement (`LOREBASE_WEB_CSP`), included in every
location so `add_header` inheritance cannot drop it; values with quotes, `$`, braces or newlines
stop the container. Tests: `packages/keeper/tests/test_security_headers.py`,
`tests/tooling/test_web_security_headers.py` (the entrypoint script and the rendered chart).
**Dependency audit in CI** (`.github/workflows/audit.yaml`, `scripts/audit_check.py`,
`.github/audit-allowlist.yaml`, `Makefile` targets `security-python` and `security-web`). On
every pull request, on `main` and weekly, pip-audit (pinned version) checks the exported locked
runtime dependencies and `pnpm audit --prod` the web app's. The check fails on any advisory not
in the allowlist (matched by package and id or alias) and on expired entries; accepted ones are
printed as warnings. Entries carry a reason, a review date and an expiry. The allowlist starts
empty: both audits found nothing. Dependabot version updates stay off. Test:
`tests/tooling/test_audit_check.py`.
**Base images pinned by digest** (`deploy/docker/backend.Dockerfile`,
`deploy/docker/web.Dockerfile`, the compose proxy image). Each `FROM` uses an `ARG ..._IMAGE`
default of the form `tag@sha256:<index digest>`, the multi-arch index so amd64 and arm64 builds
get the same content; [release](release.md#base-images) describes the bump. Test:
`tests/tooling/test_dockerfile_pins.py` fails on an external `FROM` without a 64-hex digest.

**Shared rate limits across Keepers** (`packages/keeper/src/lorebase_keeper/ratelimit/shared.py`,
`packages/store/src/lorebase_store/ratelimit.py`, revision `0022`). With
`keeper.limits.rate.backend: postgres` the token buckets live in the UNLOGGED `rate_limit_buckets`
table, keyed by the SHA-256 of the bucket name (client addresses and principal ids are not
stored). One upsert refills a bucket on the database clock and leases up to `lease_tokens` whole
tokens (at most a quarter of the bucket) to the Keeper, which serves requests from memory until
the lease is used or expires (`lease_ttl_s`); tokens leave the shared bucket only through leases,
so all Keepers together never exceed a limit. Failed-authentication buckets lease one token per
failure, are read before each authentication from a reading at most `check_cache_s` old, and fail
closed (429) when the database does not answer within `timeout_s`; other buckets follow
`on_error` (`local` by default: the in-process buckets meanwhile). The Helm chart sets the backend
when `replicaCount > 1` or the HPA is on. Tests:
`packages/store/tests/test_ratelimit_buckets_integration.py` (concurrent leases never exceed the
bucket, refill, prune, names not stored), `packages/keeper/tests/ratelimit/test_shared_store.py`
(local, allow, deny, fail closed on a slow database),
`packages/keeper/tests/integration/test_limits_postgres.py` (two applications on one database
share a bucket and the failed-authentication buckets), `tests/tooling/test_helm_db_roles.py`.

**Read auditing** (`packages/keeper/src/lorebase_keeper/auth/read_audit.py`, `auth/audit.py`,
revision `0022`). With `keeper.auth.audit.reads.enabled` the audit middleware hands every
successful read of an authenticated caller (GET, HEAD and the read-only POST routes of
`keeper.limits.rate.routes`) to a bounded queue; a background task writes batches in one
transaction to `audit_read_log`. The read stream has hash chains of its own, per Domain and
installation-wide, rather than sharing those of `audit_log`: reads are sampled and dropped under
pressure, so their volume and gaps must not touch the chains of actions, whose appends (inside
commits too) would otherwise wait on read traffic for the chain lock; each stream is verified,
checkpointed and kept for its own retention. Filters (`principal_types`, `kinds`) and
`sample_rate` apply except to reads that showed or masked a sensitive value: the masking and
vault disclosure code notes those in a context variable of the request, and cached bodies carry
the notes of their first rendering. Records hold the route template, never query strings, bodies
or values. A full queue or a failed batch drops records, counted in
`lorebase.audit.reads.dropped` by reason. The table is append-only for the application role like
`audit_log` (compose `db-roles` and the chart's `db-roles.py` included). Tests:
`packages/keeper/tests/auth/test_read_audit.py`,
`packages/keeper/tests/integration/test_sensitivity_api.py` (masked and cleared reads recorded at
`sample_rate: 0`, cached answers included, no value in the records),
`packages/store/tests/auth/test_audit_streams_integration.py`,
`packages/store/tests/auth/test_audit_append_only_integration.py`.

**Audit chain checkpoints outside the database**
(`packages/store/src/lorebase_store/auth/checkpoints.py`,
`packages/scribe/src/lorebase_scribe/checkpoints.py`, `packages/keeper/src/lorebase_keeper/__main__.py`).
An `audit_checkpoint` job, queued every `scribe.audit_checkpoints.interval_s` by the scheduler
leader, reads the newest entry of every chain of each stream and writes a JSON checkpoint signed
with Ed25519 over its RFC 8785 form. Sinks: a file that is never replaced (written to a temporary
name, synced and hard-linked into place, refused outside the directory); an HTTP PUT or POST;
or an S3-compatible PUT signed with Signature Version 4. HTTP targets pass the checks of webhook
targets (`check_target`: allowed schemes, every address global unicast unless
`allow_private_targets`, connection pinned to the checked address with the host name kept for
TLS, no redirects, no proxy from the environment, response body never read). The signing key is
a secret reference, refused at start unless it is an Ed25519 PEM. `audit verify --checkpoints
PATH --checkpoint-key PUB` verifies each checkpoint's signature (a file that does not verify is
reported and fails the run) and reports checkpointed entries that are missing or whose hash
changed, which a table owner cutting a chain's tail or recomputing a chain cannot hide without
the signing key. Tests: `packages/store/tests/auth/test_audit_streams_integration.py` (truncation
and rewrite detected, forged and altered documents refused),
`packages/scribe/tests/test_checkpoint_sinks.py` (the published Signature Version 4 example,
file sink never replaces or escapes, pinned HTTP, private targets, schemes and redirects refused,
settings), `packages/scribe/tests/integration/test_audit_checkpoints.py`,
`packages/keeper/tests/test_main.py`.

**Helm chart: the services no longer connect as the table owner**
(`deploy/helm/lorebase/templates/_helpers.tpl`, `templates/migrate.yaml`,
`deploy/helm/lorebase/files/db-roles.py`). The chart ran the migration Job, Keeper and Scribe with
one DSN, so the services connected as the owner of the tables: row-level security held only
through `FORCE`, the revoke of revision `0015` did not apply to them and they could run DDL. As on
compose, the chart now takes two DSNs, `db-maintenance-dsn` (the owner: the migration and
`database.maintenance_dsn` of Scribe and Keeper) and `db-dsn` (the application role: `database.dsn`
of Keeper and Scribe). A `db-roles` init container of the migration Job, running as the owner,
creates the application role (or waits for the one CloudNativePG manages from
`postgresql.cluster.roles`), refuses it with `SUPERUSER` or `BYPASSRLS` or when it is the owner,
and applies the grants of `app-role.sh`, `audit_log` revoke included; the migration runs with
`database.application_role` set. The chart refuses to render with one user for both DSNs, a missing
maintenance DSN or an unsafe managed role unless `database.allowSharedRole: true` (development).
Test: `tests/tooling/test_helm_db_roles.py`, the CI negative cases of the `helm` job.

## Other observations

- **Prompt injection.** The extraction model gets no tools, its output is validated against a
  JSON schema with bounded repair attempts, and local span extraction takes evidence text from
  the source by checked offsets. No extraction prompt builder exists yet in `packages/inference`;
  when it is added, document text must go in the user message as delimited data, never in the
  system prompt, and outputs must stay Proposals that need review.
- **Webhook signing key.** The stored value is the HMAC key itself; with
  `webhooks.encryption_key` set it is stored encrypted (see the hardening section above).
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
