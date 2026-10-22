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
- `application_role` (optional) is read when revisions `0005`, `0008` and `0010` run: the
  role the services connect as gets EXECUTE on the security definer read functions (see
  Row-level security). `0008` and `0010` refuse to run while the role may create objects in the
  schema of the tables or of `pg_trgm`.
- `store.reads.definer_ceilings` is read when revisions `0008` and `0010` run: the largest
  count each read function accepts. Changing it later has no effect; the settings refuse read and traverse caps
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
- Extensions `pg_trgm` and `vector` (revision `0001`) and `btree_gin` (revision `0011`) are
  created if missing and never dropped on downgrade. Where the migration role cannot create
  extensions, an administrator creates them first.

### Search indexes

Revision `0011` replaces the GIN indexes of revision `0001` on `search`
(`ix_mat_records_search`) and on trigrams of `title` (`ix_mat_records_title`) with
`ix_mat_records_branch_search` on `(domain_id, branch, search)` and
`ix_mat_records_branch_title` on `(domain_id, branch, title gin_trgm_ops)`; `btree_gin`
(contrib) provides the GIN operator classes of the two text columns. The old indexes were
shared by every Domain and branch of a hash partition: a search read the matches of all of
them and dropped the others' rows on recheck, and a search on an overlay did so once per half
(its rows and its base's). Now the Domain and branch of a search are index conditions. The
revision builds each index per partition with `CREATE INDEX CONCURRENTLY`, attaches each to
an index created `ON ONLY` the parent, and drops the old index only once the new one is valid
(about 1.4 s for the search index of a partition of 201,800 rows); a re-run rebuilds an
invalid leftover.

The alternative was a partial GIN index per branch (`WHERE domain_id = '<d>' AND branch =
'<b>'`) managed like the per-Attribute indexes. Measured on the dataset of
[performance.md](performance.md) (one partition with `main`, the copy `perf-copy` and the
overlay `agent/perf`: 201,800 rows), as the role subject to row-level security, execution of
the statement inside `lb_search` / `lb_search_overlay` (`auto_explain`, warm, milliseconds;
matches per branch in brackets):

| Query | Branch | shared GIN (`0001`) | partial per branch | `btree_gin` composite |
|-------|--------|--------------------:|-------------------:|----------------------:|
| `hydraulic` (4,064) | copy | 8.37 | 1.49 | 2.28 |
| `needle` (774) | copy | 2.46 | 1.34 | 1.74 |
| `stock` (21,600) | copy | 3.21 | 1.80 | 2.64 |
| `hydraulic` | overlay | 11.70 | 3.30 | 3.19 |
| `needle` | overlay | 4.18 | 3.04 | 2.36 |
| `stock` | overlay | 3.12 | 1.02 | 1.15 |

- Shared GIN: the bitmap of `search @@` holds the matches of both full branches (8,484 for
  `hydraulic`); for some terms the planner adds a `BitmapAnd` with the primary key over the
  branch's 99,900 rows (6.5 ms of the 8.4).
- Partial per branch: the bitmap holds the branch's matches only and the index is small
  (3 MB per 100k Records, 80 kB for the overlay). Every branch needs its own index (built
  with a copy, queued for an overlay, dropped with the branch); a branch whose index is not
  built yet, or an installation where the index manager is off, needs the shared index, so
  every row write would update two GIN indexes; and every partial index of a partition adds
  to the planning of each statement on it.
- `btree_gin` composite: the bitmap holds the branch's matches (`Index Cond: domain_id =
  'supply' AND branch = 'perf-copy' AND search @@ 'needle'`); intersecting the posting lists
  of `domain_id` and `branch` costs about 0.3 ms more than a partial index at 100k Records per
  branch (68 index pages read instead of 4 for `needle`). One index replaces the shared one,
  serves every branch and Domain from its first row with no index management, and is
  smaller (6 MB against 9 MB for the shared index of the same partition).

**Chosen: the `btree_gin` composite.** Its overlay rows were measured with the other change
of `0011` (the other columns with the probe below; the composite with the probe measured
3.79, 3.45 and 1.17): `lb_overlay_rows(..., p_probe => false)` lets `lb_search_overlay` (and
the plain SQL search) hash the overlay's keys (`Hash Anti Join` over an index-only scan of its
2,000 rows) instead of probing its primary key once per base match (2,221 buffer hits for
`needle`, 1.1 ms); other reads keep the probe.

The trigram index on titles had the same problem, worse on an overlay: for typo-tolerant
search (`titles`, Keeper's lexical search) the planner estimates `title % 'price'` at 14 rows
and reads the shared trigram index in each half, which returned 16,421 rows of the partition
(35 ms of recheck in the overlay half, which holds none of them). Scoped by branch, it returns
the branch's 8,075 rows, and 271 in the overlay half. Warm calls of `lb_search` /
`lb_search_overlay` with titles, p50 over 200 random title words: copy 17.1 ms and overlay
30.0 ms with the shared index, 10.3 and 10.6 ms with the composite.

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
  `api_key_usage`, `web_sessions`, `extraction_cache`) are installation-wide and have no
  policy. `api_key_usage` (revision `0006`) holds one row per API key and UTC day with the
  requests served and the context-pack tokens delivered, the counters of the daily quotas
  (`docs/operations.md`); rows go with their key.
- `api_keys.allowed_networks` (revision `0009`, `cidr[]`) lists the client networks a key may be
  used from; null, the value of keys created before it, allows any address.
- `web_sessions` (revision `0013`) holds the browser sessions of web sign-in: the SHA-256 of the
  session id (primary key, never the id), the principal (rows go with it), the provider subject
  and groups, the provider tokens encrypted by Keeper, and the activity, expiry and refresh
  times, indexed for the purge of expired rows ([deployment](deployment.md#web-sign-in)).
- `embeddings.text_hash` and `embedding_cursors` (revision `0014`): the SHA-256 of the text
  each vector was computed from (indexed with the Domain and model, so an identical text reuses
  a vector), and per branch and model the head and template fingerprints of the last complete
  embedding pass. `embedding_cursors` is Domain-scoped with row-level security and goes with its
  branch ([scribe](scribe.md#embeddings)).
- Plan T11 named an `rls.sql` file; the policies are inlined in revision `0001` instead, so that a
  released revision never changes.

### Security definer read functions

Postgres applies the `domain_isolation` policy before any condition built from functions it
does not mark leakproof (`->>`, casts, `@@`, `%`), so under row-level security those conditions
cannot be index conditions (design 8.7). Revision `0005` adds four functions for exactly the
read paths that depend on them, and revision `0008` replaces them with the checks below;
`lorebase_store.reads` and `lorebase_store.traverse` call them when
`store.reads.use_definer_functions` is true (the default) and run the same statements as plain
SQL otherwise:

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
  later with `lorebase_store.definer.grant_read_functions`; `CREATE OR REPLACE` in `0008` keeps
  owner and grants. The schemas of the path must not be writable by the application role (the
  default for `public` since Postgres 15): revision `0008` refuses to run when
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

## Overlay branches

Design 6.11 mode `overlay` (AC-16), revision `0010`. An overlay branch stores only the rows it
changed; every other row is read from its base branch as it was at the fork point.

- **Ref.** `refs.base_branch`, `base_revision` (the base head when the overlay was forked) and
  `base_generation` (its generation) are set exactly when `materialization = 'overlay'`
  (check `ck_refs_overlay_base`). Both references are deferred foreign keys, like `refs.head`:
  a base cannot be deleted while an overlay names it, and deleting a Domain cascades in any
  order. The base is a full branch; an overlay of an overlay is created as a copy.
- **Rows.** `mat_records` and `mat_links` carry `revision_generation` (generation of the
  Revision that last wrote the row; `0` for rows older than revision `0010`) and `deleted`
  (a tombstone; only overlays hold them). `mat_schema` has a row per overlay (copied at
  creation, so schema reads are unchanged); `mat_history` holds the overlay's own entries.
- **Reads.** The rows of an overlay `O` on base `B` forked at generation `g` are

  ```
  rows of O that are not tombstones
  UNION ALL
  rows of B with revision_generation <= g that O does not shadow (any row of O, tombstone or not)
  ```

  rendered once by `lorebase_store.layout.BranchLayout` (and by `lb_overlay_rows` inside the
  read functions), with Domain, branches and generation as literals so the per-Attribute
  partial indexes of `B` match. Every row reports `O` as its `branch`, so statements written
  for a table work unchanged on the resolved rows: store primitives, the Keeper query
  compiler, health scans, IndexView validation, derived recomputation, `verify`. History is
  the overlay's entries plus `B`'s entries of Revisions up to generation `g`. The anti-join is
  a primary-key probe per base row read (`NOT EXISTS (... OFFSET 0)`), except in search, where
  the planner may hash the overlay's keys (`probe=False`, revision `0011`).
- **Plans.** Postgres does not flatten a `UNION ALL` whose members have `WHERE` clauses, so
  the two halves stay subqueries. Restriction conditions (key, Kind, attribute expressions,
  `@@`) and lateral references are pushed into both halves and use the indexes of each
  branch; ordering, `LIMIT` and join keys are not. The store primitives therefore repeat
  `ORDER BY ... LIMIT` inside each half for keyset pages and attribute queries
  (`BranchLayout.records_arms`, the `p_tail` argument of `lb_overlay_rows`), and look Records
  and Links up by key through `LATERAL` subqueries (hydration, the neighborhood walk and its
  final hydration). The neighborhood's lookups (stored Link hops, foreign-key hops out of a
  Record, final hydration) read both branches in one index scan instead of one per half
  (`BranchLayout.records_lookup` / `links_lookup`, `lb_overlay_lookup` of revision `0012`):
  `branch IN (O, B)` with the generation and ref conditions on the base rows, `DISTINCT ON`
  the identity with the overlay's row first, tombstones dropped last. That removes the
  primary-key probe of the overlay per base row and shrinks the statement to plan: measured
  on the dataset of [performance.md](performance.md) as the role subject to row-level
  security, one call of `lb_neighborhood_overlay` went from 5.65 to 4.86 ms (the statement
  inside it from 2.44 to 2.11 ms; the copy: 3.39 and 1.63). Inbound foreign-key hops keep
  both halves, so each half uses its branch's per-Attribute partial index. Keeper's query compiler uses the resolved rows as a table: its results
  are the same, but on an overlay an ordered page or a join on a large Kind sorts or scans
  that Kind's rows (a follow-up). The base half also requires the ref to still be that overlay
  (`base_branch` and `base_generation` unchanged), so a READ COMMITTED reader that resolved
  the layout before a promotion reads only the (now complete) overlay rows.
- **Read functions.** `lb_search_overlay`, `lb_links_in_overlay`, `lb_neighborhood_overlay`
  and `lb_query_by_attribute_overlay` take the arguments of the `0005` functions, read the
  overlay's base and generation from `refs` themselves (no SQL text crosses the boundary) and
  run the same statements over the resolved rows; on a branch that is not an overlay they
  read its own rows. They start with the argument, scope and search_path checks of revision
  `0008` (the same SQLSTATEs) and resolve the overlay's rows only after them. Revision `0011`
  replaces `lb_search_overlay` (hashed anti-join) and revision `0012`
  `lb_neighborhood_overlay` (lookups), with the same signatures, checks and grants. The
  helpers `lb_overlay_rows` and `lb_overlay_lookup` take SQL text and are executable by their
  owner only. The `0005` functions are unchanged and still serve full branches.
  `grant_read_functions` grants both sets.
- **Snapshot, not freeze (option b).** The base keeps committing. Before a base commit changes
  or removes a row (values, title, search vector, derived values, Links), it copies the
  previous row into every overlay of the base that does not hold that key yet
  (`INSERT ... SELECT ... ON CONFLICT DO NOTHING`, one statement per batch for all overlays),
  and it stamps the rows it writes with its generation, which hides rows it inserts from the
  overlays. The base rows an overlay reads therefore never changed after its fork. A base
  commit first takes the branch locks of its overlays (base lock, then overlay locks in name
  order; overlay commits take only their own lock), so no overlay commit or promotion runs
  meanwhile. `store.materialize.overlay_max_per_branch` caps the overlays of one base, which
  bounds that copy work; further branches are created as copies.
- **Writes on an overlay.** A row about to change is first copied up from the base (so it
  keeps its derived values and search vector), then changed; a delete leaves a tombstone; an
  upsert of a tombstoned key starts like a new row.
- **Per-Attribute indexes.** An overlay gets the per-Attribute indexes its schema plans, on its
  own rows (`plan_indexes(schema, overlay=True)`): the same expressions and predicates as a
  full branch's, with the overlay's name as the `branch` literal, so the overlay half of an
  attribute query, an inbound foreign-key Link read or a neighborhood hop uses them as the
  base half uses the base's. None is unique: uniqueness on an overlay holds for its resolved
  rows and is checked by the commit (`MaterializedIndexView`), while its own rows include
  tombstones, which keep their values. Creating or re-forking an overlay queues them as
  `store.index` jobs in every index mode (an index on a few rows is still built by scanning
  the partition, which the creation must not pay); until the worker builds them, the overlay
  half scans the overlay's rows of the Kind by primary key. A promotion plans the full set: the
  unique indexes are created and the overlay's non-unique variants of them dropped. No
  `revision_generation` condition is needed in the plan: the overlay half reads every own row,
  and in the base half `revision_generation <= g` is a filter on the rows the base's index
  returns, which only drops rows the base inserted after the fork (a row it updated since is
  shadowed by the overlay's copy anyway).
- **Promotion.** `materialize.promote` turns an overlay into a full copy: it copies the base
  rows it reads and the base history up to the fork, drops its tombstones, sets
  `materialization = 'full'` (head and ref version unchanged) and builds the per-Attribute
  indexes. It happens when a commit leaves the overlay with more than
  `overlay_max_rows` rows of its own; when the base is more than
  `overlay_max_base_lag_revisions` generations past the fork (checked by the overlay's commits;
  a base commit that crosses the limit queues a `store.materialize` job); before a commit or
  merge that changes the overlay's schema; before its base is rebuilt or deleted
  (`promote_overlays_of`, called by Keeper's branch delete).
- **Re-fork.** After an overlay was merged into its base (its head is an ancestor of the base
  head), `materialize.refork` fast-forwards it to the base head (ref log reason
  `branch.refork`), drops its rows and records the new fork point: as cheap as creating it.
- **Creation mode.** `create_branch(mode="overlay" | "copy")`; without a mode,
  `store.materialize.branch_mode` (default `copy`) decides, except names starting with one of
  `overlay_branch_prefixes` (default `agent/`, `extract/`, `source/`), which default to
  overlays. An overlay needs to start at the head of a full source branch; otherwise the branch
  is a copy (or rebuilt from the tree). Keeper's `POST /branches` takes `mode`; overlays do not
  count against `keeper.branches.max_materialized_per_domain`.
- **Index jobs of short-lived branches.** Proposal branches are overlays that are merged and
  deleted soon after creation, often before the worker has built their indexes.
  - Deleting a branch (`RefRepository.delete`, the one path of Keeper's branch delete and of
    the task-branch release after a merge or close) cancels, in the same transaction, the
    branch's `queued` `store.index` builds (`status = 'cancelled'`, result detail `branch
    deleted`). Queued drops stay: they clean up indexes the branch left. A job a worker
    already claimed (`running`) is not touched; its handler decides.
  - A build whose branch is gone when it runs (claimed before the delete, or queued by hand)
    completes as a no-op: no DDL, result `{"status": "cancelled", "detail": "the branch no
    longer exists"}`, a `debug` log, and the job ends `succeeded` on the first attempt, so it
    neither fails nor retries. The `lorebase.scribe.jobs` counter records it as `succeeded`;
    none of its `status` values distinguishes a no-op, so no label was added. A build still
    running when its branch is deleted drops the index it just made and ends the same way
    (`the branch was deleted during the build`).
  - Coalescing: a queued or running job per (Domain, index name, action) is reused instead of
    queued again (`BranchIndexes._insert_job`), and the index name pins branch, Kind and
    Attribute, so re-forking or re-syncing an overlay before its builds ran adds no jobs. A
    coarser unit (one job per branch and Kind) was not adopted: jobs build one index each, so
    it would need a job format that carries several and partial-failure handling for them, to
    save a few rows of a queue that merge and delete cleanup already empties. Deferring the
    builds of branches under a minimum age was not adopted either: it delays the indexes of
    branches that live long, and the cancellation above removes the wasted work. The
    existence check is not atomic with the insert, so two concurrent syncs can queue the same
    build twice; the second finds the index built (`already built`) and does nothing.
- **Garbage collection.** Fork points (`refs.base_revision`) are marking roots; materialized
  rows are never swept, so the copies an overlay holds go with its ref.
- **Bundles.** Export records `overlay`; import restores every branch as a full copy (heads
  are rebuilt from trees).

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
- `verify` walks the whole log, or one chain (`domain_id`), in `seq` order, `batch_size`
  entries per query, and checks each entry against the previous entry of its own chain. With `after_seq`, the first entry of each chain in the range is trusted as
  the anchor. Truncating the tail of a chain is not detectable from the chain alone.
- Appends and verification run with the RLS bypass; listing a Domain's entries is scoped to it.

## Tests

`lorebase_store.testing` provides the server: `LOREBASE_TEST_DATABASE_URL` if set (its role needs
`CREATEDB` and `CREATEROLE`: each test gets a scratch database and the RLS test creates a probe
role), otherwise a `pgvector/pgvector:pg16` container (override with
`LOREBASE_TEST_POSTGRES_IMAGE`) through testcontainers, installed by `uv sync --all-packages`.
