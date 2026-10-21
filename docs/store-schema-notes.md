# Store schema notes

Decisions taken where [design.md](design.md) section 7 leaves the Postgres schema open. The
revisions in `packages/store/migrations` are the source of truth; the models in
`lorebase_store.models` match them (an integration test checks that schema comparison finds no
difference).

## Configuration

- The connection lives in the `database` section of design 16 (`dsn`, pool sizes, timeouts),
  shared with Keeper readiness and docker compose (`LOREBASE__DATABASE__DSN`). `dsn` is a secret
  setting; the alias `LOREBASE_DB_DSN` sets it. Schema shape lives in the `store` section.
- `alembic upgrade head` run from `packages/store` (or the image copy at `/opt/lorebase/store`)
  reads both sections through `load_settings()`. The wheel ships the environment as
  `lorebase_store/_migrations`, so `lorebase_store.migrations.head_revisions()` works in
  installed services.
- `statement_timeout_ms` defaults to 30 s, safe for bulk commits; read-heavy deployments lower it
  (design 16 shows 2 s). Migrations run with `migration_statement_timeout_ms` (default: none).
- `read_dsn` (optional, several allowed), `read_max_lag_ms`, `read_fallback_to_primary` and
  `read_probe_timeout_ms` configure read replicas; `lorebase_store.replicas` routes reads and
  guards freshness (see [deployment.md](deployment.md)). Everything else, writes included, uses
  `dsn`.
- `store.layout.hash_partitions` and `store.embeddings.dims` are read when revision `0001` runs.
  Changing them later needs a new migration.
- `application_role` (optional) is read when revisions `0005` and `0007` run: the role the
  services connect as gets EXECUTE on the security definer read functions (see Row-level
  security). `0007` refuses to run while the role may create objects in the schema of the
  tables or of `pg_trgm`.
- `store.reads.definer_ceilings` is read when revision `0007` runs: the largest count each read
  function accepts. Changing it later has no effect; the settings refuse read and traverse caps
  above it.

## Types and keys

- Closed value sets are native enum types: `ref_kind`, `materialization`, `proposal_origin`,
  `proposal_status`, `review_verdict`, `item_decision`, `merge_attempt_status`,
  `principal_type`, `grant_scope`, `invocation_status`, `job_status`, `run_status`. A new value
  needs a migration with `ALTER TYPE ... ADD VALUE`. Roles stay `text` because custom roles exist.
- 32-byte checks (`octet_length = 32`) on `objects.hash`, `tree_nodes.hash`, `revisions.id`,
  `revisions.root`, `api_keys.hash`, `audit_log.entry_hash`, `documents.sha256`,
  `extraction_cache.cache_key`.
- Primary keys not given by the design: `proposal_reviews (domain_id, id)`,
  `proposal_items (domain_id, proposal_id, item_id)`, `merge_attempts (domain_id, id)`,
  `conflicts (domain_id, attempt_id, id)` with `fingerprint` unique per attempt,
  `conflict_resolutions (domain_id, proposal_id, fingerprint)`, `group_members (group_id,
  principal_id)`, `operation_invocations`, `source_runs`, `documents`, `extraction_jobs`, `vault`
  on `(domain_id, id)`, `document_chunks (domain_id, document_id, seq)`.
- `audit_log.seq`, `outbox.id` and `jobs.id` are `bigint` identities (ordering matters); other ids
  are application-generated prefixed text ids.
- Timestamps that are hashed or carried by history (`revisions.created_at`, `ref_log.at`,
  `provenance.at`, `refs.created_at`, `domains.created_at`) have no default; operational
  timestamps default to `now()`.

## Foreign keys

- `domain_id` references `domains(id) ON DELETE CASCADE` on Domain-scoped tables, except
  `audit_log`, `outbox` and `jobs`, whose rows outlive a Domain.
- `revision_parents`, `changesets` and `refs.head` reference `revisions`; materialized tables
  reference `refs (domain_id, name)` with cascade, so deleting a branch drops its rows; Proposal,
  merge attempt and document children cascade from their parent.
- `revision_parents.parent_id` and `refs.head` reference `revisions` without a cascade, so a
  Revision that is still referenced cannot be deleted on its own. Both constraints are
  `DEFERRABLE INITIALLY DEFERRED` (revision `0004`): deleting a Domain removes Revisions, edges
  and refs in one cascade of no fixed order, and the check runs at commit, when none is left.
- No foreign keys point at the content-addressed `objects` and `tree_nodes` (bulk `COPY` order,
  garbage collection), nor from `ref_log`, which keeps entries of deleted refs.

## Physical layout

- `objects`, `tree_nodes`, `mat_records`, `mat_links` are hash-partitioned on `domain_id` into
  partitions named `<table>_p<n>`.
- `tree_nodes.body` uses lz4 compression, set on the parent and on every partition (an
  `ALTER` on a partitioned parent does not reach existing partitions).
- `embeddings.embedding` is a dimensionless `vector`, as in design 7.2, with the check
  `vector_dims(embedding) = dims`; models of different sizes coexist. Revision `0001` creates
  the HNSW cosine index `embeddings_hnsw_<dims> ... WHERE dims = <dims>` for the configured
  dimension. Index names starting with `embeddings_hnsw_` or `mat_records_attr_` (per-Attribute
  indexes of the materializer) are managed at runtime and ignored by schema comparison.
- Per-Attribute indexes (`lorebase_store.indexes`) are created on the hash partition that holds
  the Domain, not on the partitioned parent: one partition is scanned and locked, `CONCURRENTLY`
  works, and a unique index needs no partition key column. Each one is partial on literal
  `domain_id`, `branch` and `kind_id`, so a query must repeat those literals to use it; a JSON
  comment on the index (`domain`, `branch`, `kind`, `attribute`, `unique`, `cast`) is how
  existing indexes are found again. Index jobs use `jobs.type = 'store.index'`.
- Index DDL needs a role that owns `mat_records`. A commit role that owns it changes the
  indexes in the commit (large Kinds through queued concurrent builds). A role that does not
  (the application role) queues every change when `database.maintenance_dsn` names a
  maintenance role; the job worker (`python -m lorebase_store.jobs work`, types `gc` and
  `store.index` by default) runs index jobs with that role. An index that would reject the
  rows of a schema change (unique, or a cast of a retyped Attribute) cannot be dropped before
  they are written: its drop is queued, forced, in its own transaction, and the commit
  succeeds when retried after the worker ran. Without either, index management is skipped
  with a warning and `python -m lorebase_store.indexes plan --domain <id> [--branch <name>]`
  prints the pending DDL (`CREATE INDEX CONCURRENTLY`, `COMMENT ON INDEX`, `DROP INDEX
  CONCURRENTLY`, one statement per line) for an administrator to run outside a transaction.
- The compose stack runs Keeper and Scribe as an application role subject to row-level
  security and sets `LOREBASE_DB_MAINTENANCE_DSN` to the owner role that runs the migrations,
  its Postgres superuser ([deployment.md](deployment.md#database-roles)); production uses a
  dedicated owner role instead of a superuser.
- Extensions `pg_trgm` and `vector` are created if missing and never dropped on downgrade. Where
  the migration role cannot create extensions, an administrator creates them first.

## Row-level security

- Every table with a `domain_id` column, partitions included, has `ENABLE` and `FORCE ROW LEVEL
  SECURITY` and the policy `domain_isolation`: a row is visible and writable when
  `lorebase.domain_ids` (transaction setting, comma-separated ids) contains its `domain_id`, or
  when `lorebase.bypass_rls` is `on`. Unset means no rows.
- The bypass is for system paths that span Domains: job claiming, authentication lookups of
  grants, the audit hash chain, data migrations. Rows with a null `domain_id` (`audit_log`,
  `jobs`) are visible only with the bypass.
- Superusers and roles with `BYPASSRLS` ignore policies; the application role must be neither.
- Tables without `domain_id` (`domains`, `principals`, `groups`, `group_members`, `api_keys`,
  `api_key_usage`, `extraction_cache`) are installation-wide and have no policy. `api_key_usage`
  (revision `0006`) holds one row per API key and UTC day with the requests served and the
  context-pack tokens delivered, the counters of the daily quotas (`docs/operations.md`); rows go
  with their key.
- `api_keys.allowed_networks` (revision `0008`, `cidr[]`) lists the client networks a key may be
  used from; null, the value of keys created before it, allows any address.
- Plan T11 named an `rls.sql` file; the policies are inlined in revision `0001` instead, so that a
  released revision never changes.

### Security definer read functions

Postgres applies the `domain_isolation` policy before any condition built from functions it
does not mark leakproof (`->>`, casts, `@@`, `%`), so under row-level security those conditions
cannot be index conditions (design 8.7). Revision `0005` adds four functions for exactly the
read paths that depend on them, and revision `0007` replaces them with the checks below; `lorebase_store.reads` and `lorebase_store.traverse` call them
when `store.reads.use_definer_functions` is true (the default) and run the same statements as
plain SQL otherwise:

| Function | Read path |
|----------|-----------|
| `lb_search(domain, branch, kinds, config, query, candidates, limit, trigram_threshold, trigram_weight)` | full-text search over a bounded candidate set, optional trigram title matches |
| `lb_links_in(domain, branch, relation, to_key, limit, after, from_kind, attribute)` | Links pointing at a Record along one Relation, stored or foreign-key backed |
| `lb_neighborhood(domain, branch, seeds, depth, relations, fanout, max_steps, max_nodes)` | the recursive neighborhood walk |
| `lb_query_by_attribute(domain, branch, kind, attribute, type, op, value, limit, after_value, after_key)` | comparisons on a per-Attribute partial index |

Why this keeps Domains isolated:

- **What RLS protects against.** The policy reads `lorebase.domain_ids`, a setting any role can
  set in its own transaction. It is a second line of defense against query bugs (a statement
  that forgets its Domain), not against a compromised application role, which can scope
  itself to any Domain. Authorization happens in Keeper before any read (design 13.3).
- **Fixed statements, Domain pinned.** Each function takes the Domain as its first argument
  and puts it, as a literal, in the predicate of every table it reads (`mat_records`,
  `mat_links`), joins included. The statement text is fixed: arguments reach it as `%L`
  literals or `EXECUTE ... USING` parameters, keywords (comparison, cast type) come from
  whitelists, and malformed arguments raise. A query bug in a caller cannot widen the Domain.
- **Bounded arguments.** Every count (`limit`, `candidates`, `depth`, `fanout`, `max_steps`,
  `max_nodes`) must be between 1 and its ceiling in `store.reads.definer_ceilings`; NULL is
  refused, so no call runs `LIMIT NULL` (SQLSTATE 22023).
- **Scope checked, not widened.** Rows come only from the Domain argument, which must also be
  visible in the transaction under the condition of the `domain_isolation` policies: listed in
  `lorebase.domain_ids`, or `lorebase.bypass_rls` on. A missing Domain raises 22023, one outside
  the scope 42501. The callers are store primitives that receive a Domain their caller has
  already authorized and scope the transaction to it with `scope_to_domain` first; a wider
  scope (several Domains, or the bypass) still returns rows of the argument only.
- **Least privilege.** `SECURITY DEFINER`, `STABLE` (read only), a fixed `search_path`
  (`pg_catalog`, the schema of the tables, the schema of `pg_trgm`, `pg_temp` last), which each
  function compares with the current one before anything else: after `ALTER FUNCTION ... SET`
  or `RESET search_path` every call raises 55000. EXECUTE is revoked from PUBLIC and granted to
  `database.application_role` by the migrations (an error if the role does not exist), or
  later with `lorebase_store.definer.grant_read_functions`; `CREATE OR REPLACE` in `0007` keeps
  owner and grants. The schemas of the path must not be writable by the application role (the
  default for `public` since Postgres 15): revision `0007` refuses to run when
  `has_schema_privilege(role, schema, 'CREATE')` holds for the schema of the tables or of
  `pg_trgm`, since such a role could shadow an operator and run code as the owner. Granting
  CREATE later reopens that; keep it revoked.
- **Owner.** A function skips the policies only when its owner does: a superuser or a role
  with `BYPASSRLS`. The owner is the migration role, which never serves requests; no login
  role used for reads bypasses RLS. If the migration role is subject to RLS, the migration logs
  a warning and the functions still return the right rows, with the policy applied and the
  slower plans. `ALTER FUNCTION ... OWNER TO` a `NOLOGIN` role with `BYPASSRLS` is the
  alternative where the migration role must not have it.
- **Tests.** `test_read_functions_read_only_the_domain_they_are_given` reads one Domain while
  the transaction is scoped to two and checks that no row of the other comes back, that a
  Domain outside the scope is refused, and that a role without the grant is refused;
  `test_definer_checks.py` covers the argument, scope and `search_path` checks as the
  application role, and the refused upgrade. `test_definer_functions_keep_index_plans_under_row_level_security`
  checks with `auto_explain` that the statements run inside the functions, by a role subject
  to RLS, are index scans.

The functions build their statements per call with the Domain, branch and Kind as literals, so
each call is planned with them and matches the partial index predicates; the planning time is
part of the measured latency ([performance.md](performance.md)).

## Concurrency contracts

- Ref update: `UPDATE refs SET head = :new, version = version + 1 WHERE domain_id = :d AND
  name = :n AND head = :expected AND version = :v`; zero rows means the ref moved.
- Job claim: `SELECT ... FROM jobs WHERE status = 'queued' AND run_after <= now() ORDER BY
  priority DESC, run_after LIMIT :n FOR UPDATE SKIP LOCKED`, served by `ix_jobs_claimable`.

## Proposals and merge attempts

Revision `0002` adds what the merge API needs (design 6.10, 11.1):

- `proposals.idempotency_key` (unique per Domain and author when set) and `updated_at`.
- `merge_attempts.base` is nullable: heads without a common ancestor, or a virtual base
  (`virtual_base` true, AC-13), have no stored base Revision. New columns: `result`
  (`conflicts`, `merged`, `fast_forward`, `up_to_date`), `policy` (the merge policy options
  that differ from the defaults), `created_by`, `outcome` (automatic resolutions, resolutions
  that matched no Conflict, Conflict counts, change stats), `revision` (the target head after a
  committed merge) and `idempotency_key` (of the merge request that committed it).
- `conflicts.id` is the hex fingerprint, the public Conflict id; `payload` holds the core
  Conflict document plus the full member keys of grouped Conflicts; `resolution`,
  `resolved_by` and `resolved_at` are set when the attempt applied a resolution to the
  Conflict (resolved). Queries test `resolved_by`, which is SQL null while the Conflict is
  open (`resolution` may hold JSON `null`).
- Every merge run stores a new attempt; the previous open attempt becomes `stale` (or
  `abandoned` when the Proposal is closed). Attempts beyond `keeper.proposals.attempt_retention`
  are deleted, merged attempts excepted.

## Audit chain

- One implementation, `lorebase_store.auth.AuditRepository`, used by both the Keeper (request
  audit, admin listing) and the commit pipeline (step 8, inside the commit transaction).
- `audit_log` holds one hash chain per Domain plus one installation-wide chain (`domain_id` null):
  `entry_hash = sha256(prev_hash || JCS(entry))`, `prev_hash` being the hash of the previous
  entry of the same chain in `seq` order. Lookups use the `(domain_id, seq)` index.
- Appends take `pg_advisory_xact_lock(audit_lock_key(domain_id))`, a 64-bit key from
  `sha256("lb1\0audit-chain\0" || domain_id)`. Per-Domain chains were chosen over a single global
  chain: a commit holds its chain lock until the commit transaction ends, and a global lock
  would serialize every commit and every audited request of the installation behind it. Lock
  order is branch lock, then audit lock; Keeper writes take only the audit lock, so no cycle.
- `verify` walks the whole log in `seq` order and checks each entry against the previous entry
  of its own chain. With `after_seq`, the first entry of each chain in the range is trusted as
  the anchor. Truncating the tail of a chain is not detectable from the chain alone.
- Appends and verification run with the RLS bypass; listing a Domain's entries is scoped to it.

## Tests

`lorebase_store.testing` provides the server: `LOREBASE_TEST_DATABASE_URL` if set (its role needs
`CREATEDB` and `CREATEROLE`: each test gets a scratch database and the RLS test creates a probe
role), otherwise a `pgvector/pgvector:pg16` container (override with
`LOREBASE_TEST_POSTGRES_IMAGE`) through testcontainers, installed by `uv sync --all-packages`.
