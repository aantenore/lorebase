# Security review before the first release

Review of the trust boundaries of Lorebase at commit `4bd2d26`, with the fixes made on top of it.
Each fixed finding has its own commit with a test that fails without the fix. Findings that could
not be fixed safely in this pass are listed with a proposed fix.

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
| F6 | Medium | deploy | The compose stack connects as the Postgres superuser, which ignores row-level security | Open |
| F7 | Medium | deploy | Compose falls back to `change-me` for the LiteLLM master key and the Grafana password | Open |
| F8 | Low | keeper | A Domain id of `*` in a path was authorized against grants in any Domain | Fixed |
| F9 | Low | store | Index drop jobs accepted any index name | Fixed |
| F10 | Low | inference | A per-task endpoint inherited the main API key | Fixed |
| F11 | Low | CI | Checkouts kept the job token in `.git/config` | Fixed |
| F12 | Low | store | Security definer functions accept NULL limits and do not check the transaction scope | Open |
| F13 | Low | store | Revision `0005` does not verify that the table schema is not writable by the application role | Open |
| F14 | Low | store | Bundle import has no size limits and trusts unhashed metadata | Open |
| F15 | Low | keeper | Schema inference sources are installation-wide | Open |
| F16 | Low | keeper | The optional API key IP allowlist of design 13.2 is not implemented | Open |
| F17 | Low | keeper | OIDC issuer and JWKS URLs may use plain HTTP | Open |
| F18 | Low | scribe | CSV over HTTP checks the host name only; local CSV reads have a check-then-read race | Open |
| F19 | Low | deploy | Helm NetworkPolicy is disabled by default; `/api/v1/docs` is served by default | Open |
| F20 | Low | keeper | Requests with invalid credentials are not rate limited | Open |
| F21 | Info | inference | Provider error text (up to 300 characters) is kept in exception messages; clients honour proxy variables | Open |
| F22 | Info | store | `rls_scope` does not refuse commas in Domain ids; `sql_literal` assumes `standard_conforming_strings` | Open |
| F23 | Info | keeper | Server-Sent Event streams are authorized once, at connection | Open |

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

### Open

**F6. Compose uses the superuser for the application** — `deploy/compose/docker-compose.yaml`
(`LOREBASE_DB_DSN`, `LOREBASE_DB_MAINTENANCE_DSN`). Superusers bypass row-level security, so the
second line of defence of design 13.3 is off on the compose stack. Repro: start the stack and run
`SELECT rolsuper FROM pg_roles WHERE rolname = current_user` with the Keeper DSN. Fix: an init
script creating a `NOSUPERUSER NOBYPASSRLS` application role; Keeper and Scribe use it, `migrate`
keeps the owner. Keeper could also log an error at start when its role has `rolsuper` or
`rolbypassrls`.

**F7. Default passwords in compose** — `deploy/compose/docker-compose.yaml`
(`LITELLM_MASTER_KEY`, `GRAFANA_ADMIN_PASSWORD` default to `change-me`), `.env.example`. Both
services are opt-in profiles bound to `127.0.0.1`. Fix: empty values in `.env.example` and
`${VAR:?}` in the services, with a documented generation step; the compose validation job needs
values for every profile.

**F12. Security definer arguments** — `packages/store/migrations/versions/0005_security_definer_reads.py`.
`IF p_limit < 1` is NULL for a NULL argument, so `LIMIT NULL` is unbounded (also `p_candidates`,
`p_fanout`, `p_max_steps`, `p_max_nodes`), and the functions trust `p_domain` without comparing it
with `lorebase.domain_ids`. Python callers cap every argument and pass an authorized Domain, so
this is not reachable over HTTP. Fix, in a new revision: `IS NULL OR NOT BETWEEN 1 AND <ceiling>`
checks and an error unless the Domain is in the transaction scope or the bypass is on.

**F13. Definer search_path** — same revision. The path includes the table schema; if the
application role can create objects there (Postgres before 15 with `public`), it could shadow an
operator and run code as the function owner. Fix: refuse the upgrade when
`has_schema_privilege(<application role>, <schema>, 'CREATE')`.

**F14. Bundle import** — `packages/store/src/lorebase_store/bundle.py` (CLI only). No cap on
member size, member count or JSON line length, and authors, provenance, ref log, change sets and
Domain settings are imported as written. Fix: size caps and bounded `readline`, refuse symlinks in
directory mode, document bundles as trusted input or sign the manifest.

**F15. Schema inference sources** — `packages/keeper/src/lorebase_keeper/services/schema_inference.py`.
`allowed_sources` is installation-wide: `schema.write` in any Domain may profile any table the
source role reads, and sample values appear in the evidence. Fix: bind each source to Domains and
optional schema and table lists.

**F16. API key IP allowlist** — design 13.2 lists an optional allowlist; the key model has none.
Fix: an `allowed_networks` column checked in `Authenticator._with_api_key` against the client
address (behind a configured trusted proxy list), or remove the claim from the design.

**F17. OIDC over HTTP** — `packages/keeper/src/lorebase_keeper/auth/config.py`. Signing keys
fetched over plain HTTP can be replaced in transit, which allows forged tokens. Fix: require
`https` for `issuer` and `jwks_uri` except for loopback hosts, with an explicit opt-out.

**F18. CSV Source reads** — `packages/scribe/src/lorebase_scribe/sources/connectors.py`. URLs
are checked by host name only (no address check, no pinning); the hosts are operator allowlisted.
Local files are checked after `resolve()` and read afterwards by path, so a writer inside an
allowed root can swap a symlink in between. Fix: reuse `check_target` and address pinning from
the webhook sender; open with `O_NOFOLLOW` and read a bounded amount from the same descriptor.

**F19. Helm defaults** — `deploy/helm/lorebase/values.yaml`. `networkPolicy.enabled: false`;
`ci/production-values.yaml` allows egress to `0.0.0.0/0:443`; `keeper.openapi.docs_enabled`
defaults to true and `/readyz` returns check details. Fix: enable the policy by default, narrow
egress, document turning the docs off in production.

**F20. Unthrottled failed authentication** — `packages/keeper/src/lorebase_keeper/ratelimit/enforce.py`.
Limits are keyed by the authenticated principal, so requests with invalid keys are not limited;
each costs a prefix lookup and an INFO log line. Guessing a key is infeasible (256-bit secret),
but the path can be used to load the database and the logs. Fix: a per-client-address bucket for
401 answers.

**F21. Inference client details** — `packages/inference/src/lorebase_inference/transport.py`.
Error text from the provider is kept in `EndpointError`; some providers echo a masked key there.
`httpx` clients in inference and Herald honour proxy environment variables. Fix: omit the detail
for 401 and 403; make `trust_env` a setting, default false.

**F22. Store literals** — `packages/store/src/lorebase_store/auth/scope.py` joins Domain ids with
commas without the check `scope_to_domain` has (ids are slugs, so not exploitable);
`indexes.sql_literal` assumes `standard_conforming_strings=on`. Fix: the same comma check, and
`standard_conforming_strings: on` in the connection `server_settings`.

**F23. Event streams** — `packages/keeper/src/lorebase_keeper/routes/events.py`. A grant revoked
during a stream takes effect at the next connection. Fix: cap stream duration or re-check grants
every few minutes.

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
