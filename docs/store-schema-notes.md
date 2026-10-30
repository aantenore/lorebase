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
  `ref_kind` `snapshot` (revision `0025`) marks the refs that hold the materialized rows of a
  past Revision for reads at it (`materialize.snapshot`, design 8.6); they have no ref log entry.
- 32-byte checks (`octet_length = 32`) on `objects.hash`, `tree_nodes.hash`, `revisions.id`,
  `revisions.root`, `api_keys.hash`, `audit_log.entry_hash`, `documents.sha256`,
  `extraction_cache.cache_key`.
- Primary keys not given by the design: `proposal_reviews (domain_id, id)`,
  `proposal_items (domain_id, proposal_id, item_id)`, `merge_attempts (domain_id, id)`,
  `conflicts (domain_id, attempt_id, id)` with `fingerprint` unique per attempt,
  `conflict_resolutions (domain_id, proposal_id, fingerprint)`, `group_members (group_id,
  principal_id)`, `operation_invocations`, `source_runs`, `documents`, `extraction_jobs`, `vault`,
  `schema_suggestions` on `(domain_id, id)`, `document_chunks (domain_id, document_id, seq)`,
  `source_written (domain_id, source_id, target_branch, element, key)` and
  `source_written_pending (domain_id, proposal_id, element, key)` (revision `0040`).
- `source_written.last_seen_run` and `last_seen_at` (revision `0043`, design 9.1 "Incremental
  Sources"): nullable, additive; readers fall back to `updated_at`. No index: the sweep pages a
  Source's rows by the primary key and filters on the sighting time.
- Document identity (revision `0041`, design 9.2 "Document versions and lifecycle"):
  `documents.status` is `text` with a check (`active`, `superseded`, `retired`, `purged`), not an
  enum, like other lifecycle columns that may grow. Unique `(domain_id, sha256) WHERE source_key
  IS NULL AND status <> 'purged'` (keyless content addressing; a purged row no longer holds the
  bytes), unique `(domain_id, source_key, version) WHERE source_key IS NOT NULL`, unique
  `(domain_id, source_key) WHERE source_key IS NOT NULL AND status = 'active'` (one current
  version per key, so its bytes are unique per key) and a lookup index on `(domain_id,
  source_key, sha256)`. Uploads under one key are serialized by a transaction advisory lock.
  `extraction_cache.chunk_hash` (nullable, 32-byte check, partial index) names the chunk an
  answer was computed from, for purges.
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
  dimension, with the build parameters of `store.storage.hnsw_m` and `hnsw_ef_construction`
  (`WITH (...)`; defaults: pgvector's m 16, ef_construction 128). Indexes that `storage apply`
  builds later take them too; an existing index keeps its own until rebuilt (installations
  from before the setting have pgvector's ef_construction 64; operations, "Rebuilding the
  embedding index"). Index names starting with `embeddings_hnsw_` or `mat_records_attr_` (per-Attribute
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

Revision `0044` adds `ix_mat_records_object_hash` on `(domain_id, branch, object_hash)`
(per partition, `CREATE INDEX CONCURRENTLY`, attached to an index created `ON ONLY` the
parent). Semantic search joins the nearest vectors of `embeddings` (keyed by
`(domain_id, object_hash)`) to the branch's Records through it; without it the planner sorts
every vector of the model (270 ms at 300,000 vectors, over the budget of design 8.7; 25 ms with
it). Trade-off: about 40 bytes per row of `mat_records`, no measurable cost on commits and about
15% on the bulk deletion of a branch (docs/performance.md#filtered-semantic-search).
`embeddings` gets no new column: a template fingerprint includes the Kind id, so it already
names the Kind of its vectors.

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

### Word lexicon (typo tolerance)

At 1M Records the branch-scoped trigram index on titles still read about 150,000 rows for a
common word: two Kinds have sentence-like titles that contain it and are never similar to it
as a whole, and a GIN bitmap cannot stop at `LIMIT`. Revision `0018` drops
`ix_mat_records_branch_title` and moves trigram matching to the distinct words of a branch
(the pg_trgm documentation's approach for unique words):

- (Revision `0045`, design 8.9.) `mat_values(domain_id, branch, kind_id, attribute_id, value,
  norm, records)` applies the same scheme to the values of the Attributes listed in
  `mat_value_attributes(domain_id, kind_id, attribute_id, max_chars)`: statement triggers
  `lb_values_maintain` on `mat_records` (insert, update, delete, transition tables, the same
  plan choice) count per branch the string values and string array elements of those
  Attributes, at most `max_chars` long, skipping rows whose `values` did not change and rows of
  a ref that is gone; spent values go through `ix_mat_values_spent`. `norm` is
  `lb_value_norm(value)` (lower case, NFD with combining marks removed, blanks collapsed), served
  by a btree and a trigram GIN index on `(domain_id, branch, norm)`. The selection is
  Domain-wide and decided by `lorebase_store.values.sync_selection` (Scribe `value_index`).
  Fence: each trigger call first takes `pg_advisory_xact_lock_shared(lb_values_lock_key(domain))`
  for the Domains of its statement, then probes one row of the selection's primary key for them
  (in key order with `LIMIT 1`: a plain `EXISTS` let the cached generic plan scan the selection
  of a Domain holding most of it, 2-3 ms per statement at 100,000 rows); a Domain without
  selected Attributes stops there. A pass that changes the selection takes the same key
  exclusively, so the Domain's writers wait for its backfill and no write falls between the
  backfill and the triggers seeing the new selection; other Domains are not affected.
  Backfills use `ON CONFLICT DO UPDATE` after deleting the pair's rows, and reads join
  `mat_value_attributes`, so rows a race leaves for an Attribute no longer held are never
  served and the next pass removes them. `mat_value_cursors` remembers the fingerprint and time
  of the last decision. The fence relies on READ COMMITTED writers (each trigger statement takes
  a new snapshot after its lock wait); a `default_transaction_isolation` of `repeatable read` or
  `serializable` for Lorebase's roles would break it. A pass takes the exclusive lock only when
  it changes something, re-reads the default branch's schema hash and the selection under it,
  and spends at most `store.values.pass_timeout_ms` holding it (each statement gets the time
  left; Kinds not reached become `pending`). A schema commit or merge on the default branch
  drops newly ineligible Attributes after taking the lock shared, deleting through the
  selection's key and `ix_mat_values_attribute (domain_id, kind_id, attribute_id)`, and queues
  a pass that is not merged into a running one. Lookups pass the (Kind, Attribute) pairs the
  caller may read into the queries (and `lb_values_similar`), so their limits apply after that
  filter. `lb_values_similar` is a security
  definer read function with the checks and grants of revision 0008 (bounded `p_limit`, at
  most 64 terms, Domain in scope, fixed `search_path`). Row-level security as for the other
  Domain tables; `mat_values` rows go with their ref.
- (Revision `0047`, design 8.9.) The value limit holds between passes. `mat_value_attributes`
  gains `max_values` (set by the pass and by a schema commit on the default branch:
  `hard_max_distinct` for Attributes always held, `max_distinct * hysteresis` for plain strings)
  and `overflowed`. `mat_value_sizes(domain_id, branch, kind_id, attribute_id, size)` counts per
  branch the values `mat_values` holds of each selected Attribute; rows go with their ref.
  `lb_values_maintain` skips overflowed Attributes, and its upsert of `mat_values` is one
  statement whose `RETURNING` (inserted rows, `xmax = 0`; existing rows dropping to 0) feeds the
  size changes, upserted in key order, and marks `overflowed` an Attribute whose size passes
  `max_values`; an update between values a branch already holds writes no size. Reads and
  `lb_values_similar` join the selection on `NOT overflowed` and skip a pair whose size row on
  the row's branch is past `max_values` (a primary key probe of `mat_value_sizes`), marked or
  not: a schema commit lowering a limit races with writers of other branches. The next pass
  drops the Attribute. The upgrade (with `lorebase.bypass_rls`, row-level security being forced
  on the owner) sets `max_values` to `hard_max_distinct`, counts the sizes from `mat_values`,
  marks rows past it and clears `mat_value_cursors`; the first pass of each Domain without a
  cursor counts its sizes again under the exclusive fence before deciding (writers of the old
  trigger body during a rolling upgrade) and sets every limit. It also empties
  `resolve_misses.callers`: digests stored before keyed caller digests were plain hashes of
  principal ids (phrases and counts stay). The downgrade restores revision 0045's functions.
- `mat_lexicon(domain_id, branch, kind_id, word, records)`: per Kind, the lexemes of the
  branch's own search vectors and the number of its rows that hold each, without lexemes that
  have a digit or more than 64 characters (`lb_lexicon_word`; codes and keys are not
  corrected). Per Kind, so a caller whose grant names some Kinds gets corrections from the
  words of those Kinds only, as search returns their Records only. Primary key
  `(domain_id, branch, kind_id, word)`, trigram GIN `ix_mat_lexicon_branch_word` on `(domain_id,
  branch, word gin_trgm_ops)`, and a partial index on `(domain_id, branch) WHERE records <= 0`
  for the spent words a trigger deletes. Rows go with their ref (`ON DELETE CASCADE`);
  row-level security as for the other Domain tables.
- Three statement-level `AFTER` triggers on `mat_records` (`INSERT`, `UPDATE`, `DELETE`,
  with transition tables) run `lb_lexicon_maintain`, which sums the changes per word with
  one statement planned for the rows at hand (`EXECUTE`: a cached plan of a one-row statement
  would compare every pair of rows of a 10,000-row commit), upserts the words that gained
  rows, decrements the others and deletes those that reach 0. Unchanged search vectors (most
  `UPDATE`s) are skipped by an anti-join; old rows of a branch whose ref is gone (a
  cascading delete) are skipped. The function runs as the writer, so the writer's Domain
  scope applies to its writes. Words are counted per branch, sorted, so two writers of one
  branch (which the branch lock already serializes) take the row locks in one order.
- An overlay holds the words of its own rows only. `lb_search_corrections` and the plain SQL
  path look a word up in the overlay and its base: a superset of the words the overlay
  reads (the base may have gained words since the fork), which can only add a correction
  candidate that matches nothing. A promotion copies the base rows in, and their words with
  them.
- `lb_lexeme_query(word, alternatives)` builds `'word' | 'alternative' | ...` as tsquery
  input (quotes and backslashes doubled), so lexemes are used as they are; the search
  functions and the plain SQL search rewrite the query with it (`ts_rewrite`).

The upgrade backfills the lexicon from every search vector under the lock that creating the
triggers takes on `mat_records`; downgrade rebuilds the title index of `0011` concurrently.
The search functions of revisions `0005` and `0010` stay for processes that still call them
during an upgrade; their trigram branch has no index any more.

### BM25 search index (optional)

Revision `0037` prepares an optional BM25 ranking of lexical search through ParadeDB
`pg_search` (AGPL-3.0, an opt-in image; [search](search.md#bm25-ranking)). It needs nothing of
the extension: its functions name the extension's types and operator only inside statements
they build at run time.

- `lb_search_terms(search)`: the lexemes of a search vector, each repeated at each of its
  positions, in position order (lexemes of one position bytewise), joined by spaces; once each
  for a vector without positions, NULL for an empty or NULL vector. A standard SQL body,
  `IMMUTABLE`: the index expression.
- `ix_mat_records_bm25`, built by `storage apply` with `store.storage.bm25_index` (not by a
  migration): `USING bm25` over `key`, `domain_id`, `branch`, `kind_id` as literal fields,
  `revision_generation`, `deleted`, and `lb_search_terms(search)` tokenized on whitespace
  without case folding (field `terms`). Per partition `CREATE INDEX CONCURRENTLY`, then
  attached to an index created `ON ONLY` the parent, as the values index; it is valid once
  every partition is attached. Maintained by every write in the writing transaction. Schema
  comparison skips it. `storage apply` refuses to build it while a role other than the owner
  may create objects in the extension's schemas (`pdb`, `paradedb`), whose functions the
  definer functions call as their owner.
- `lb_bm25_query_valid(query)`: the JSON query shapes the store builds (`term`, `phrase` on
  `terms`, `boolean`, `all`), nothing else.
- `lb_search_bm25` and `lb_search_bm25_overlay` (below): one top-k scan of the index per
  branch half and per arm (the query as written, the corrected-only hits), ordered by
  `pdb.score` then Kind and key bytewise; an overlay's base half reads as many more rows as
  the overlay holds of the searched Kinds (at most `p_candidates`), then drops the rows the
  overlay shadows; a superset query (phrases) reads `p_candidates` rows and rechecks
  `search @@`; `truncated` says when a scan's limit may have cut a row that would rank among
  those returned; scores come back relative to the best row.

The downgrade drops the functions and the index (`CASCADE` on `lb_search_terms` also takes
partition indexes never attached); the extension stays.

### Shared lineage maps

`mat_records.lineage` (Attribute to provenance id), `mat_records.lineage_revisions`
(Attribute to Revision) and `mat_links.lineage` were about half of a Record row (840 of 1,600
bytes at 10M Records) while a few hundred distinct maps repeat over millions of rows. Revision
`0036` stores each distinct map once per Domain:

- `mat_lineage_maps(domain_id, id, map)`: `id` is the first 16 bytes of the SHA-256 of the
  map's canonical text (`jsonb::text`); Domain-scoped with row-level security, rows go with
  their Domain. Maps are never updated; one that no row names any more stays (a map is a few
  hundred bytes, and their number is bounded by the distinct lineage combinations written).
- A column holds either the map or a reference `{"$map": "<32 hex>"}`. Readers resolve it with
  `lb_lineage(domain_id, column)` (SQL, inlined: a primary-key probe of the small, always cached
  maps table per reference), which every read path uses: the Record statements of
  `lorebase_store.reads` and `traverse` (the security definer functions' results are wrapped,
  the functions are unchanged), `MaterializedRepository`, the merge lineage and index views,
  the lineage backfill and Keeper's lineage, explain and query compiler. A reference whose map is
  missing raises `XX001` instead of reading as empty.
- `BEFORE INSERT OR UPDATE OF` the map columns row triggers `lb_lineage_maps` replace maps by
  references (`lb_lineage_share`) for every writer, so the commit pipeline, copy-on-write,
  background copies, rebuilds and backfills need no change; `INSERT ... ON CONFLICT DO
  UPDATE SET lineage = excluded.lineage` copies the reference the insert trigger produced.
  Maps of at most 64 bytes (`{}`, a Link with one entry) stay inline: a reference is as large.
- The storage profile decides whether the triggers are enabled (`store.storage.lineage_maps`,
  `shared` except with `fast`); `python -m lorebase_store.storage apply` switches them and
  rewrites the rows in batches either way, so readers always see both forms. The migration
  enables them only on a database without Records: during a rolling upgrade processes of the
  previous version still read the columns as maps, so an existing install switches with
  `storage apply` once every process runs this version. The rewrite locks its rows with `FOR
  UPDATE SKIP LOCKED` and never waits for a commit; rows it skipped, and references copied
  into pages already rewritten, are found by the next run's sample (0.1% of the pages) and
  rewritten again; a few may stay in the other form, which readers accept. The
  downgrade writes every map back into its rows (partition by partition, writes locked out)
  and refuses to drop the maps while a reference remains. The functions
  run as the caller, under its privileges and row-level security; only the trigger functions
  have `EXECUTE` revoked from `PUBLIC`.

### Storage profiles

`store.storage.profile` (`compact`, `balanced`, `fast`; `lorebase_store.storage`) decides what
the database keeps besides the rows: the lineage maps above, the GIN index on Record values
(`ix_mat_records_values`, which serves equality on Attributes without an index of their own),
the per-Attribute indexes of `indexed` Attributes (`compact` keeps those of unique and foreign
key Attributes only: `IndexPlan.built`), the trigram index of the word lexicon, the precision
of the HNSW index (`embeddings_hnsw_half_<dims>` on `halfvec`, `embeddings_hnsw_<dims>` on
`vector`; stored vectors keep full precision, searches cast to the type the index that exists
has, `embeddings.index_precision`), TOAST compression of large values and the fill factor of
`mat_records`. Schema comparison ignores the two optional model indexes.

`storage apply` builds and drops indexes concurrently (the values index partition by
partition, attached to the parent; dropping it takes a lock of at most
`store.storage.lock_timeout_ms` on the partitions), queues nothing, rewrites lineage maps in
page ranges (progress in `storage_tasks`, resumed by the next run), and with `--compact`
moves rows from the last pages of `mat_records`, `mat_links` and `mat_history` into free space
earlier in the table (`DELETE ... RETURNING` into `INSERT` on the partition, one branch at a
time under the branch lock that commits take, so no commit changes a row being moved), then
lets `VACUUM` truncate the emptied pages; `--reindex` rebuilds the indexes of those tables with
`REINDEX TABLE CONCURRENTLY`. Moving rows on the partition does not fire the statement-level
lexicon triggers of the parent: the words do not change. A moved row is a new tuple, so a
concurrent `UPDATE` or `DELETE` that does not hold the branch lock would miss it: every writer
of these tables holds it (commits, background copies and deletions, stale refreshes, the
lineage backfill, promotions; the garbage collector takes the locks of every ref before it
deletes history rows), and copy-on-write inserts with `ON CONFLICT DO NOTHING`. A new writer
must keep that invariant. Index validity is read from `pg_index`: an interrupted concurrent
build is rebuilt, a switch of the HNSW precision drops the old index only once the new one is
valid and `PRECISION_TTL_S` has passed (searches use the newest valid index).

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
- `embeddings.fingerprint` (revision `0024`): the fingerprint of the embedding template a vector
  was rendered with, part of the primary key `(domain_id, object_hash, model, fingerprint)`, so
  branches whose templates differ for the same content keep a vector each and Keeper reads the
  one of the searched branch's template. The upgrade sets it where every branch holding the
  content agrees on one template; the other vectors keep `''` (never read, collected by GC) and
  every cursor loses its fingerprints, so the next pass renders each text once more, copies
  vectors of equal texts and embeds the rest. The downgrade keeps one vector per `(domain_id,
  object_hash, model)` and clears the cursors' fingerprints too.
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
| `lb_search_corrections(domain, branch, kinds, config, query, threshold, candidates, min_length)` | typo corrections of the query words (negated ones aside) the Kinds of a branch (and of its base) do not hold, from the word lexicon (revision `0018`) |
| `lb_search_corrected(domain, branch, kinds, config, query, candidates, limit, words, replacements, weight)` | full-text search with corrected words widened to their replacements, exact matches first (revision `0018`; replaces `lb_search` in the store) |
| `lb_search_bm25(domain, branch, kinds, config, query, candidates, limit, match, match_exact, words, replacements, weight, fixed, fixed_exact)` | BM25 search through the optional `pg_search` index (revision `0037`); `match` and `fixed` are JSON queries checked by `lb_bm25_query_valid`, the scope comes from the arguments only |

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
  a base commit that crosses the limit queues a `store.materialize` job, which copies in
  batches, see [Large branches](#large-branches)); before a commit or merge that changes the
  overlay's schema; before its base is rebuilt or deleted (`promote_overlays_of`, called by a
  branch deletion small enough to run in its transaction, and by `rebuild`). All of these
  copy in the writing transaction only when the base holds at most `async_copy_min_rows`
  rows. Over a larger base, the limits queue the background copy, a schema change is taken
  over by it (`reconcile`), and a rebuild of the base detaches the overlay and builds it from
  its tree; see [Large branches](#large-branches). An overlay whose background copy is queued
  or running is not promoted by its commits' limits.
- **Re-fork.** After an overlay was merged into its base (its head is an ancestor of the base
  head), `materialize.refork` fast-forwards it to the base head (ref log reason
  `branch.refork`), drops its rows and records the new fork point: as cheap as creating it.
- **Creation mode.** `create_branch(mode="overlay" | "copy")`; without a mode,
  `store.materialize.branch_mode` (default `copy`) decides, except names starting with one of
  `overlay_branch_prefixes` (default `agent/`, `extract/`, `source/`), which default to
  overlays. An overlay needs to start at the head of a full source branch; otherwise the branch
  is a copy (or rebuilt from the tree). A copy of a source holding more than
  `async_copy_min_rows` rows starts as an overlay and is copied in the background
  ([Large branches](#large-branches)). Keeper's `POST /branches` takes `mode`; overlays do not
  count against `keeper.branches.max_materialized_per_domain`, copies in progress do.
- **Index jobs of short-lived branches.** Proposal branches are overlays that are merged and
  deleted soon after creation, often before the worker has built their indexes.
  - Deleting a branch (`RefRepository.delete`, the one path of Keeper's branch delete and of
    the task-branch release after a merge or close) cancels, in the same transaction, the
    branch's `queued` `store.index` builds and `store.materialize` copies (`status =
    'cancelled'`, result detail `branch deleted`). Queued drops stay: they clean up indexes
    the branch left. A job a worker already claimed (`running`) is not touched; its handler
    decides. Hiding a large branch for a background deletion (`RefRepository.mark_deleted`)
    cancels the same jobs, and a build that runs afterwards finds no branch (the ref of a
    branch being deleted does not count).
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
  rows are never swept, so the copies an overlay holds go with its ref. A branch being deleted
  in the background keeps its head and fork point as roots until its ref is gone.
- **Bundles.** Export records `overlay`; import restores every branch as a full copy (heads
  are rebuilt from trees).

## Large branches

Revisions `0017` and `0031`. Copying a branch is one `INSERT ... SELECT` per table and deleting one is a
cascade from its ref: both grow with the Domain (85 to 95 s at 1M Records, see
[performance](performance.md#1m-records)) and would not fit in `database.statement_timeout_ms`.
Above a size, both run as jobs in batches, each batch its own short transaction
(`lorebase_store.materialize.background`).

- **Copy.** `create_branch(mode="copy")` at the head of a full source that holds more than
  `store.materialize.async_copy_min_rows` Records and Links (and has room under
  `overlay_max_per_branch`) creates the branch as an overlay of the source, as cheap as any
  overlay: it is readable and writable at once with overlay semantics. A row in
  `branch_materializations` (status `pending`, the fork point) and a `store.materialize` job
  are added in the same transaction (`BranchCreation.pending`). Below the threshold the copy is
  made in the creating transaction as before.
- **Build.** A copy above the threshold that cannot start as an overlay (another start
  point, an overlay source, a source still building, the overlay cap reached) is created with
  `materialization = 'building'` (revision `0031`) and a `branch_materializations` row of
  strategy `tree`. The same job writes its rows from the tree of its head, each batch its own
  transaction: `clear` (rows an earlier state left), `tree_history` (the changesets of the
  head's first-parent chain, a few Revisions at a time), `tree_rows` (Records and Links,
  whole tree leaves at a time in a fixed depth-first order, `NodeRepository.scan_from`, with
  lineage folded from the history, titles and search vectors), `derived` (each Derivation
  over its Kind's keys in order), `paths` (each materialized path over its start keys), then
  `switch` to `full` with the per-Attribute indexes queued. Rows equal those of an inline
  rebuild. Meanwhile Keeper reads the branch head like a past Revision (from the tree and
  snapshots), and commits and merges into it fail with `BranchMaterializingError` (409
  `branch-materializing` with `Retry-After`). The size test is the tree's entry count (or
  the source's own and base rows at its head).
- **Batches.** The job copies the base rows the overlay reads, table by table (`records`,
  `links`, then `history`), in primary key order from a cursor: each batch finds the key a
  batch size ahead and runs `INSERT ... SELECT ... ON CONFLICT DO NOTHING` for the
  base rows of that key range with `revision_generation` at or before the fork (history: of
  Revisions up to its generation), and saves the cursor and `rows_done` in the same
  transaction. A worker that dies, a retry or a second runner resumes from the saved cursor;
  a batch run twice copies nothing. The `switch` phase then takes the branch lock, deletes the
  overlay's tombstones, sets `materialization = 'full'` (head and version unchanged) and queues
  the per-Attribute indexes of a full branch (`BranchIndexes(defer=True)`: none is built in
  that transaction). A re-fork while the copy runs changes the fork point: the copy restarts
  from the first row.
- **Batch size.** Each batch holds the branch lock for its transaction, so a commit on the
  branch, or a base commit copying on write into it, waits for at most one batch; without it
  their inserts of the same keys in another order could deadlock with the batch and fail the
  commit. Batches are sized by that hold time: the first has `job_batch_min_rows` rows; the
  next halves (proportionally) when a batch held the lock longer than its target and doubles,
  up to `job_batch_rows`, when it held it for less than half. The target is
  `job_batch_target_ms` (200 ms), or `job_batch_contended_ms` (50 ms) when a commit waited:
  the batch waited for the branch lock longer than a tenth of that, or found a transaction
  waiting for it at its end (`pg_locks`). A commit on a branch being copied thus waits about
  50 ms at most once commits are seen, instead of up to a full batch of `job_batch_rows`
  (0.5 s for Records at 100k).
- **Commits during the copy.** Between batches commits run
  as on any overlay. A copied row is the row the overlay already reads, so reads do not change
  while it fills up: the overlay half returns it, the base half no longer does (anti-join on
  the identity; for history an anti-join on `(partition, element_key, revision_id)`, which
  `BranchLayout.history_sql` has for this).
  - A commit on the branch copies a base row up before it changes it and leaves a tombstone
    when it deletes one: a later batch that meets such a row finds it held and keeps it (`DO
    NOTHING`); a commit that meets a row a batch copied changes that copy.
  - A commit on the base copies the rows it changes or removes into the overlay first (copy
    on write, as for every overlay) and stamps its own rows with a later generation, which
    the batches skip; the base rows written at or before the fork never change while the
    overlay depends on them, so one pass over the base copies everything.
  - Promotion, re-fork and rebuild delete rows of the branch outside a commit; a batch run
    meanwhile could insert a deleted row again. They take the copy fence (`copy_fence`, an
    advisory lock per branch) exclusively and batches take it shared, so they never overlap;
    the next batch sees the result (a full branch ends the copy, a new fork point restarts it).
  - The switch runs in the transaction of the last step, under the branch lock. When a batch
    waits for the branch lock longer than `commit.lock_timeout_ms` three times, the job queues
    itself again.
  - The overlay's own limits (`overlay_max_rows`, base lag) do not promote it while its copy
    is queued or running, since `promote` copies in one transaction.
  - A schema change on it is accepted (`apply.defer_schema_change`): the commit writes the
    schema row and its own rows under the new schema, with derived values and path Links
    recomputed incrementally for what it wrote, and adds its title or search Kinds to the
    row's `reconcile`. After the switch the job reconciles in batches what the change
    recomputes on a full branch: `prune` (derived values of Attributes no longer derived,
    Links of path Derivations gone), `derived`, `paths`, then `search` (titles and search
    vectors of the Kinds in `reconcile`). Until then, rows the branch did not write since the
    change keep values computed under the previous schema. An overlay over a large base that
    is not being copied yet gets its copy queued by the schema change, which then follows the
    same path.
- **Status.** `branch_materializations` keeps per branch `status` (`pending`: queued or
  waiting for a retry; `running`; `done`; `failed`: a tree build failed `copy_max_failures`
  times), `strategy`, `phase`, `cursor`, `rows_done` and `rows_total` (Records, Links and
  history entries of the base when the copy started; tree entries for a build), `failures`,
  `reconcile`, the last `error`, and the times. Keeper's branch responses carry
  it as `materialization_status` and `materialization_progress`; `lorebase branch create
  --mode copy --wait` waits for it.
- **Failures.** A failed copy leaves a correct overlay and keeps its cursor. Each time its job
  uses `store.jobs.max_attempts`, `failures` grows and the job is queued again after
  `job_retry_seconds` doubled per failure, at most `copy_retry_max_seconds`. After
  `copy_max_failures`, the overlay is detached from its base (`detach_to_build`: `building`,
  no base, head and version unchanged) and built from its tree instead, which reads neither
  the base rows nor the copy's cursor; a deletion of the base waiting for it proceeds
  (outcome `fallback`, alert `LorebaseBranchCopyFallback`). A build that fails as often is
  `failed` (outcome `given_up`, alert `LorebaseBranchBuildFailed`); queuing a
  `store.materialize` job for it again resumes it. A lag promotion queued
  by a base commit uses the same job and the same row. A branch made full by another path
  (`promote`) has its row marked `done`.
- **Delete.** `materialize.delete_branch` (Keeper's branch delete and a merge with
  `delete_source`) deletes a branch holding at most `async_delete_min_rows` rows in the
  caller's transaction, promoting its overlays first (small: its rows are the base), as
  before. A larger branch gets
  `refs.deleted_at`: every `RefRepository` read, listing and count, the replica head lookup,
  the embedding pass, bundle export and index builds skip it, so it is gone for clients at
  once (404, absent from listings, commits fail with branch not found), and its queued index
  builds and copies are cancelled. A `store.branch_delete` job then deletes its
  `mat_records`, `mat_links` and `mat_history` rows in batches sized like the copy's (primary
  keys of the next batch, then `DELETE ... WHERE key IN`), and finally the ref, whose foreign
  keys cascade the rest (`mat_schema`, `embedding_cursors`, `branch_materializations`).
  Overlays based on the branch keep reading its rows: their copies are queued when it is
  hidden, and the job waits (queues itself again) until no ref names it as base. The name
  stays taken while the deletion runs: creating a branch with it answers 409
  `already-exists` with `Retry-After`. A materialization job that meets a branch being
  deleted stops; its batches lock the ref row `FOR KEY SHARE`, so the final ref delete waits
  for a batch in flight and no row outlives the ref.
- **Concurrency.** At most `job_concurrency` copy and delete jobs run at once across workers:
  each job holds, on a connection of its own, a session advisory lock on its branch (a second
  job on the same branch returns `skipped`) and one of `job_concurrency` slot locks (none
  free: `deferred`, queued again after `job_retry_seconds`). Both go with the connection if a
  worker dies.
- **Metrics.** `lorebase.store.branch_jobs.rows`, `.runs` and `.batch.duration`
  ([metrics](metrics.md#store)).

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
- The log is append-only below the owner: revision `0015` revokes `UPDATE`, `DELETE`, `TRUNCATE`,
  `REFERENCES` and `TRIGGER` on `audit_log` from `PUBLIC`, from `database.application_role` and
  from every other role holding them, so the services can only `SELECT` and `INSERT`. No code
  prunes entries; retention or erasure, if ever needed, is done as the owner.

## Personal data vault

Revision `0019` (design 13.5). `vault` rows are keyed by `(domain_id, id)`, `id` being the token
Record objects hold; `kind_id`, `attribute_id` and `record_key` bind the token to the value it
stands for, and `fingerprint` is a keyed hash of the value without the Record key.

- Rows are inserted by the commit pipeline in the commit's transaction, after the objects, with
  `ON CONFLICT DO NOTHING`: the same value of the same Record is the same token.
- Erasure by Record reads `(domain_id, kind_id, record_key)`, erasure by value
  `(domain_id, kind_id, attribute_id, fingerprint)` (both indexed). It clears `value_ciphertext`,
  `key_id` and `fingerprint` and sets `erased_at`; rows are never deleted, so erased tokens keep
  reading as erased and a later write of the same value moves to a new token.
- Row-level security on `domain_id` as for the other Domain tables. The downgrade refuses while
  rows exist.

## Schema suggestions

Revision `0021` (design 9.2 step 10). `schema_suggestions` rows are keyed by `(domain_id, id)`
and unique by `(domain_id, fingerprint)`, the identity of what is suggested (type, normalized
singular name, owner and target Kind). `observations` maps a document id to its count and
summed confidence, so an extraction job replaces the share of the documents it read under
`SELECT ... FOR UPDATE`; `support` is their sum and is indexed with `status` for the listing.
`examples`, `samples`, `labels` and `sources` are bounded JSON lists
(`schema.suggestions`). Inserts race through `ON CONFLICT DO NOTHING` on the fingerprint and
fold into the winner's row. Row-level security on `domain_id` as for the other Domain tables.

## Tests

`lorebase_store.testing` provides the server: `LOREBASE_TEST_DATABASE_URL` if set (its role needs
`CREATEDB` and `CREATEROLE`: each test gets a scratch database and the RLS test creates a probe
role), otherwise a `pgvector/pgvector:pg16` container (override with
`LOREBASE_TEST_POSTGRES_IMAGE`) through testcontainers, installed by `uv sync --all-packages`.
