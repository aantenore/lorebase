# Performance baseline

Latency of the agent read paths on a materialized head (design 8, "Agent read budgets"), measured by `tests/perf/bench.py`. Every read is one transaction that scopes row-level security to the Domain and runs one store primitive (`lorebase_store.reads`, `lorebase_store.traverse`); times are client side over a local socket, so they include the driver and the RLS scoping round trip.

The sections from Dataset to Reading the results are the 100k-Record run of 2026-10-22, after the word lexicon of migration `0018` (typo-tolerant search); [Word lexicon: before and after](#word-lexicon-before-and-after-100k-records) compares it with the code before. The run on the 1M-Record fixture (M1) is in [1M Records](#1m-records): every read is within budget there except the 2-hop neighborhood on branches other than `main` and Record lineage against the budget proposed for it; typo-tolerant search, over budget in that run, is within it with the lexicon ([1M Records: word lexicon](#1m-records-word-lexicon)). The run at 10M Records is in [10M Records](#10m-records): get by key, pages, Links of a Record and attribute queries are at or near budget on a shared laptop; the neighborhood, full-text search, history and lineage are over it. Focused comparisons of 2026-10-23 on datasets with realistic text follow it: [Search on very common terms](#search-on-very-common-terms), [BM25 search](#bm25-search) (optional `pg_search` backend), [Headers-only neighborhoods](#headers-only-neighborhoods) and [a narrower history index](#record-history-and-lineage-a-narrower-index).

## Dataset

- `supply` Domain from `examples/supply/scripts/make_supply_data.py --scale 540`, plus 4 extra `supplied_by` and `stocked_at` Links per Part.
- 99,900 Records and 302,940 Links on `main`, loaded through the commit pipeline in 45 commits (148 s, then `VACUUM ANALYZE`).
- 300 timed calls per read after 30 warm-up calls, random keys from the dataset.

## Environment

- machine: Darwin 25.6.0 arm64
- python: 3.12.13
- cpu: Apple M5 Pro
- cores: 15
- memory: 24 GiB
- postgres: 16.15 (Debian 16.15-1.pgdg12+2)
- shared_buffers: 128MB
- effective_cache_size: 4GB
- work_mem: 4MB
- run: 2026-10-22, Postgres in Docker on the same machine, container defaults except the settings above.

## Reads (milliseconds)

`owner`: a role not subject to row-level security (superuser in the container). `rls`: a role subject to the `domain_isolation` policies, like the application role in production.

| Operation | Target p95 | owner p50 | owner p95 | owner p99 | rls p50 | rls p95 | rls p99 |
|-----------|-----------:|----------:|----------:|----------:|--------:|--------:|--------:|
| get by key | 5.00 | 1.10 | 1.31 | 1.52 | 1.31 | 1.39 | 1.43 |
| list page of 50 | 20.00 | 1.56 | 1.80 | 2.82 | 1.66 | 1.81 | 1.88 |
| links of a record | 10.00 | 1.42 | 1.94 | 3.23 | 1.49 | 1.94 | 2.51 |
| 2-hop neighborhood | 30.00 | 4.73 | 5.43 | 8.78 | 4.98 | 6.01 | 8.19 |
| query by indexed attribute | 25.00 | 1.75 | 1.95 | 2.50 | 1.98 | 3.23 | 4.75 |
| full-text search | 50.00 | 2.18 | 3.15 | 3.87 | 2.32 | 2.80 | 3.40 |
| typo-tolerant full-text search | 50.00 | 3.09 | 4.12 | 4.45 | 3.35 | 4.80 | 5.89 |
| record history page | 10.00\* | 2.69 | 3.16 | 3.87 | 2.91 | 3.19 | 3.65 |
| record lineage | 10.00\* | 4.42\*\* | 8.18\*\* | 11.28\*\* | 4.27\*\* | 8.43\*\* | 11.67\*\* |

\* No budget in design 8.7; held to the budget of Links of a Record, the other per-Record index range read.
\*\* One-statement lineage, measured on a loaded machine where get by key took 1.9 ms at p50; see [Record lineage in one statement](#record-lineage-in-one-statement-100k-records).

## Writes (milliseconds)

| Operation | Runs | p50 | p95 | p99 |
|-----------|-----:|----:|----:|----:|
| branch creation (copy) | 3 | 8162.80 | 8311.81 | 8311.81 |
| branch creation (overlay) | 20 | 13.17 | 14.96 | 15.83 |
| commit of 1000 changes | 5 | 397.32 | 527.19 | 527.19 |

## Overlay branches (milliseconds)

`agent/perf` (overlay) and `perf-copy` (copy) forked from `main` and got the same commit of 1000 Part updates; `main` then committed 1000 other Part updates. The overlay stores 2,000 rows (its changes, its derived and copied-on-write rows), the copy 402,840. Reads alternate between the two branches call by call, with the same random keys, 300 timed calls each.

| Operation | Role | copy p50 | overlay p50 | copy p95 | overlay p95 | copy p99 | overlay p99 | overhead p50 | overhead p95 |
|-----------|------|---------:|------------:|---------:|------------:|---------:|------------:|-------------:|-------------:|
| get by key | owner | 1.12 | 1.14 | 1.27 | 1.30 | 1.35 | 1.55 | +1% | +3% |
| get by key | rls | 1.30 | 1.29 | 1.70 | 1.80 | 3.37 | 3.28 | -1% | +6% |
| list page of 50 | owner | 1.65 | 1.61 | 1.80 | 1.81 | 1.96 | 2.00 | -2% | +0% |
| list page of 50 | rls | 1.91 | 1.85 | 2.20 | 2.14 | 2.42 | 2.27 | -4% | -3% |
| links of a record | owner | 1.43 | 1.82 | 1.62 | 2.03 | 1.92 | 2.17 | +28% | +25% |
| links of a record | rls | 1.79 | 2.10 | 2.19 | 2.46 | 2.70 | 2.80 | +18% | +12% |
| 2-hop neighborhood | owner | 5.19 | 6.44 | 6.26 | 7.66 | 6.80 | 8.95 | +24% | +22% |
| 2-hop neighborhood | rls | 6.38 | 7.31 | 10.46 | 9.19 | 42.65 | 10.65 | +15% | -12% |
| query by indexed attribute | owner | 1.84 | 2.19 | 2.19 | 2.76 | 2.41 | 2.93 | +19% | +26% |
| query by indexed attribute | rls | 1.98 | 2.36 | 2.68 | 3.07 | 3.34 | 3.88 | +19% | +15% |
| full-text search | owner | 2.42 | 2.87 | 2.94 | 3.40 | 3.30 | 4.10 | +19% | +16% |
| full-text search | rls | 2.42 | 2.96 | 3.08 | 3.71 | 3.51 | 4.33 | +22% | +21% |
| typo-tolerant full-text search | owner | 3.53 | 4.67 | 4.82 | 5.94 | 5.13 | 6.84 | +32% | +23% |
| typo-tolerant full-text search | rls | 3.73 | 4.92 | 4.90 | 6.17 | 5.59 | 6.58 | +32% | +26% |
| record history page | owner | 3.14 | 2.99 | 5.12 | 3.94 | 7.52 | 6.12 | -4% | -23% |
| record history page | rls | 3.37 | 3.38 | 4.05 | 3.81 | 4.71 | 4.53 | +1% | -6% |
| record lineage | owner | 3.60 | 3.91 | 4.26 | 4.74 | 4.80 | 5.66 | +9% | +11% |
| record lineage | rls | 4.02 | 4.40 | 4.47 | 5.04 | 5.40 | 6.07 | +9% | +13% |

## Plans

Scan nodes of the statements of one call of each read, statements run inside the read functions included (`auto_explain`).

| Operation | owner | rls |
|-----------|-------|-----|
| get by key | Index Scan mat_records_p3_pkey | Index Scan mat_records_p3_pkey |
| list page of 50 | Index Scan mat_records_p3_pkey | Index Scan mat_records_p3_pkey |
| links of a record | Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b | Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b |
| 2-hop neighborhood | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b<br>Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_links_p3_pkey<br>Index Scan mat_records_p3_pkey | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b<br>Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_links_p3_pkey<br>Index Scan mat_records_p3_pkey |
| query by indexed attribute | Index Scan mat_records_attr_71d8b499dff05dab64f394fe | Index Scan mat_records_attr_71d8b499dff05dab64f394fe |
| full-text search | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_domain_id_branch_search_idx | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_domain_id_branch_search_idx |
| typo-tolerant full-text search | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_domain_id_branch_search_idx<br>Index Scan pk_mat_lexicon<br>Seq Scan mat_lexicon<br>Seq Scan refs | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_domain_id_branch_search_idx<br>Index Scan pk_mat_lexicon<br>Seq Scan mat_lexicon<br>Seq Scan refs |
| record history page | Index Scan objects_p3_pkey<br>Index Scan pk_domains<br>Index Scan pk_mat_history<br>Index Scan pk_provenance<br>Index Scan pk_revision_parents<br>Index Scan pk_revisions<br>Seq Scan refs | Index Scan objects_p3_pkey<br>Index Scan pk_domains<br>Index Scan pk_mat_history<br>Index Scan pk_provenance<br>Index Scan pk_revision_parents<br>Index Scan pk_revisions<br>Seq Scan refs |
| record lineage | Bitmap Heap Scan provenance<br>Bitmap Index Scan pk_provenance<br>Index Only Scan pk_mat_history<br>Index Scan ix_proposals_domain_id_target_ref_status<br>Index Scan mat_records_p3_pkey<br>Index Scan objects_p3_pkey<br>Index Scan pk_domains<br>Index Scan pk_mat_history<br>Index Scan pk_provenance<br>Index Scan pk_revision_parents<br>Index Scan pk_revisions<br>Seq Scan refs | Bitmap Heap Scan provenance<br>Bitmap Index Scan pk_provenance<br>Index Only Scan pk_mat_history<br>Index Scan ix_proposals_domain_id_target_ref_status<br>Index Scan mat_records_p3_pkey<br>Index Scan objects_p3_pkey<br>Index Scan pk_domains<br>Index Scan pk_mat_history<br>Index Scan pk_provenance<br>Index Scan pk_revision_parents<br>Index Scan pk_revisions<br>Seq Scan refs |

## Reading the results

- Both roles run the defaults: search, incoming Links, the neighborhood and attribute queries
  go through the security definer functions of revision `0005` (`lb_search`, `lb_links_in`,
  `lb_neighborhood`, `lb_query_by_attribute`; store schema notes), on an overlay their
  `_overlay` variants. The `rls` role gets the owner's index plans inside them, and every read
  of both roles is within its budget.
- With plain SQL (`store.reads.use_definer_functions: false`), a role subject to RLS cannot use
  conditions built from functions Postgres does not mark leakproof (`->>`, casts, `@@`, `%`)
  as index conditions: the policy is applied first. The plain SQL baseline of 2026-10-20
  measured, at p95 for that role, 28.06 ms for Links of a Record (inbound foreign-key Links
  filter every purchase order), 136.73 ms for the neighborhood and 68.14 ms for search (a scan
  of the Domain's partition).
- Key, page and stored-Link reads compare text columns only; text equality and ordering are
  leakproof, so they keep their index plans as plain SQL under row-level security.
- The functions build each statement with the Domain, branch and Kind as literals, so every
  call is planned with them (partial index predicates match) and runs without JIT; the
  planning time is part of the numbers above.
- Search reads the GIN index on `(domain_id, branch, search)` (revision `0011`,
  `btree_gin`): the Domain and branch are index conditions, so a search reads its branch's
  matches only, however many branches share the partition (here `main`, a copy and an
  overlay). It ranks at most `store.reads.search_candidates` matches, so very common terms
  cost a bounded amount. Typo-tolerant search (revision `0018`, Keeper's lexical search)
  queries a misspelled title word: the word is looked up in the branch's word lexicon
  (`mat_lexicon`, and the base's for an overlay), its closest words come from the trigram
  index on the lexicon, and the search runs with `(word | correction ...)`.
- Times come from a laptop shared with other work; repeated runs differ by a few milliseconds
  at p95, up to ten for typo-tolerant search.
- Branch creation copies the Records, Links and history rows of `main` (about twice the Records
  and Links) and builds the new branch's per-Attribute indexes in the same transaction.
- An overlay branch (AC-16, store schema notes) is created by inserting its ref, copying the
  compiled schema row and queuing the builds of its per-Attribute indexes (`store.index`
  jobs, one per index): its cost does not depend on the size of the Domain. The job worker
  builds them concurrently; this run builds them before the reads below.
- Overlay reads resolve the overlay's own rows over the base rows as of the fork point in one
  statement: an index scan of each branch (the overlay's own per-Attribute indexes for its
  half), a primary-key probe of the overlay per base row read (search hashes the overlay's
  keys instead) and a one-row check that the ref is still that overlay. Keyset pages and
  attribute queries are cut per half; the neighborhood looks Links and Records up by key with
  one index scan over both branches (revision `0012`). `test_overlay_reads_keep_index_plans`
  checks the plans.
- Much of the overlay overhead left is planning: an overlay statement plans two or three
  scans of the partition where the copy plans one, and the planner checks each scan against
  every partial index of the partition (the per-Attribute indexes of `main`, the copy and the
  overlay: 42 here), about 0.2 ms more per statement. Search, attribute queries, Links of a
  Record and the neighborhood stay within 30% of the copy at p50, and every read far within
  its budget of design 8.7; `tests/perf/test_perf.py` asserts that search on the overlay
  stays within 25% of the copy at p50 at a small scale. Before revisions `0011` and `0012`
  and the overlay's own indexes, search cost +93 to +101% at p50 (+73 to +132% at p95) and
  the neighborhood +50%.
- The overlay and the copy below got the same commits; `main` then moved, and its commit
  copied the previous state of the rows it changed into the overlay (copy on write).

Regenerate with:

```sh
uv run python tests/perf/bench.py --scale 540 --extra-links 4 --out docs/performance.md
```

## Background branch copies and deletions

Since migration `0017`, a copy of a large branch is created as an overlay and copied by a
`store.materialize` job in batches, and a large branch is deleted by a `store.branch_delete`
job in batches ([store schema notes](store-schema-notes.md#large-branches)). Measured on
2026-10-22 on the 100k-Record dataset above (99,900 Records, 302,940 Links, 45 commits), same
machine, with `tests/perf/bench.py --scale 540 --extra-links 4 --branch-iterations 2` (reads at
100 iterations). Every transaction ran under the default `database.statement_timeout_ms`
(30 s); default `store.materialize.job_batch_rows` (5,000).

| Operation | Runs | p50 | p95 | p99 | max |
|-----------|-----:|----:|----:|----:|----:|
| branch creation (copy), request | 2 | 76.65 | 101.57 | 101.57 | 101.57 |
| branch copy, background job | 2 | 9,682 | 9,763 | 9,763 | 9,763 |
| branch copy, one batch | 326 | 47.63 | 132.96 | 208.49 | 531.93 |
| branch deletion, request | 2 | 7.81 | 8.49 | 8.49 | 8.49 |
| branch deletion, background job | 2 | 8,631 | 12,273 | 12,273 | 12,273 |
| branch deletion, one batch | 326 | 39.40 | 86.04 | 113.25 | 157.85 |

- The request that creates a copy now takes as long as an overlay (77 ms here against 7.3 s
  for the copy in one transaction before); the branch is readable and writable at once.
- The background copy reads about 815,000 base rows (Records, Links and history entries) in
  163 batches and takes 9.7 s, against 8.0 s for the single `INSERT ... SELECT` in the same
  run: the price of `ON CONFLICT DO NOTHING`, one statement per batch and the cursor. Batches
  read the base along its primary key (`ORDER BY ... LIMIT`), so their cost does not grow with
  the size of the branch; a history batch probes `revisions` by primary key per row
  (`OFFSET 0`), where a semi-join scanned every Revision of the Domain per row and made the
  history phase four times slower at this size.
- Records batches were the slowest (up to 0.5 s at 5,000 rows: the GIN indexes on search
  vectors, values and titles), and a batch holds the branch lock, so a commit on the copy
  could wait that long. Since revision `0031` batches are sized by their hold time (store
  schema notes, "Batch size"): they aim at `job_batch_target_ms` (200 ms), and at
  `job_batch_contended_ms` (50 ms) once a commit waited for the lock.
- Deleting is a 8 ms request; the job removes the rows in 8.6 to 12.3 s, batches under 0.2 s.
- At 1M Records (not measured in this run: the `--seed-database` fixture of [1M Records](#1m-records)
  was not available, and loading it takes about 33 minutes), the batches stay the same size and
  the copy and deletion jobs scale with the rows, about ten times longer in total; no
  transaction depends on the size of the Domain any more.

## Path Derivations

Not measured by the harness, whose schema has no path Derivation. A commit on a schema without materialized paths runs no extra statement, so the commit figures above stand. With a materialized path, a commit adds per path: for each step whose Links (or foreign-key Records) changed, one indexed read per step walked back to find the affected start Records, then one indexed read per step to recompute them and one read and one write of their stored pairs; a schema change or rebuild recomputes the path for every Record of its start Kind, in batches of `store.materialize.read_batch_rows`. A commit after a change of `store.traverse.path_fanout` rewrites the materialized paths and recomputes the derived values reading a path, as a schema change does, once. Reads of a materialized path cost what reads of a stored Relation cost. A path composed at read time costs one join per step, each later step a lateral read of at most `store.traverse.path_fanout` Links per intermediate Record; reading it from the far end (its inverse name) composes it forward over the first step's Links, so materialize paths read that way on large Kinds. Derivations and Operation `links()` compose a path in a few indexed reads per step for the Records they read (from the far end, each intermediate Record's Links are read once more to check the bound); a neighborhood following a composed path runs one statement per hop instead of one in all.

## Word lexicon: before and after (100k Records)

Typo-tolerant search before migration `0018` matched whole titles by trigram similarity on a
correctly spelled title word; since, it corrects a misspelled title word (two letters in its
middle swapped, `tests/perf/harness.py` `misspelled`) from the branch's word lexicon and
searches with the corrections. Both runs on 2026-10-22, same machine and container settings,
the "before" run on `origin/main` before the change.

| Operation | before p50 | before p95 | after p50 | after p95 |
|-----------|-----------:|-----------:|----------:|----------:|
| typo-tolerant search, owner | 11.64 | 31.06 | 3.09 | 4.12 |
| typo-tolerant search, rls | 11.24 | 26.91 | 3.35 | 4.80 |
| typo-tolerant search, overlay, rls | 11.86 | 28.19 | 4.92 | 6.17 |
| commit of 1000 changes | 355.22 | 536.19 | 397.32 | 527.19 |
| branch creation (copy) | 7,712.82 | 7,984.67 | 8,162.80 | 8,311.81 |

The commit pays for the lexicon triggers: one call per `mat_records` statement, and the
pipeline runs many one-row statements (`executemany`), so the trigger sums small statements
with a cached plan and plans only large ones (more than 64 rows). Without the triggers the
same commit measured 262 to 297 ms against 320 to 388 ms with them (scale 60, five commits
each). A copy branch also counts the words of every copied row.

## Partial matching (100k Records)

Lexical search matches every term of `q` first; when that finds fewer than
`search.partial.min_results` Records, the ranked partial matches follow (design 8.4). The
read `partial-match full-text search` of `tests/perf/harness.py` is Keeper's lexical search on
three random title words, which rarely share a Record: the corrections, the full match, the
stop-word check of the terms, the OR query through `lb_search_corrected` and one primary-key
read that counts the terms each candidate holds. Same 100k dataset, 2026-10-22, p50 / p95 in
milliseconds.

The laptop ran other work during these runs (load average 10 to 17 on 15 cores), so every
read measured slower than the baseline above, `full-text search` included, whose code path
did not change. The comparison that matters was therefore measured call by call, alternating
the code before (`search.partial.min_results: 0`, the full match alone) and after on the same
queries:

| Descriptive query, 3 title words | before | after |
|---|---|---|
| owner | 3.73 / 5.36 | 7.94 / 11.18 |
| rls | 4.29 / 7.08 | 8.71 / 13.63 |
| hits per query (limit 20) | 0 in 660 of 660 calls | 20, all `partial` |

Bench run with the partial read, same load (`full-text search` and typo-tolerant search for
reference; their baseline is in [Reads](#reads-milliseconds)):

| Operation | Target p95 | owner p50 | owner p95 | rls p50 | rls p95 |
|-----------|-----------:|----------:|----------:|--------:|--------:|
| full-text search | 50.00 | 3.69 | 8.07 | 3.73 | 5.59 |
| typo-tolerant full-text search | 50.00 | 6.98 | 27.75 | 5.57 | 8.68 |
| partial-match full-text search | 50.00 | 9.71 | 15.31 | 8.66 | 11.14 |

On an overlay branch the partial read costs +20% (owner) and +19% (rls) at p50 over a copy,
p95 12.05 and 17.21 ms. Partial matching costs about twice the full match alone and stays
far within the 50 ms budget of search; a query whose full match finds enough Records pays
nothing more. Plans: the OR query reads the same GIN index as the full match, and the term
count reads `mat_records` by primary key (`Index Scan mat_records_p3_pkey`) for at most
`search.partial.candidates` (100) Records, under row-level security too.

## 1M Records

The M1 fixture: the same harness at ten times the size, run on 2026-10-22.

### Method

```sh
LOREBASE_TEST_DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:PORT/postgres \
uv run python tests/perf/bench.py --scale 5406 --extra-links 4 --iterations 1000 --warmup 100 \
  --branch-iterations 2 --seed-database perf_seed_1m
```

- 1,000,110 Records and 3,032,766 Links on `main`, loaded through the commit pipeline in 408
  commits of up to 10,000 changes (batches above `write.copy_threshold_rows` go through `COPY`
  into a staging table, as every large commit does): 1,961 s including the index builds and
  `VACUUM ANALYZE`. The loader was not changed; the harness now builds the batches lazily, one
  Kind at a time.
- `--seed-database` keeps the loaded dataset in its own database; each run measures a copy of
  it (`CREATE DATABASE ... TEMPLATE`) and drops the copy, so a run after a change does not
  load again.
- 1,000 timed calls per read and role after 100 warm-up calls, random keys; p50, p95 and p99.
  Reads run first as `owner` (not subject to row-level security), then as `rls` (subject to
  the `domain_isolation` policies, like the application role), so the `owner` pass also warms
  the cache.
- Record history (first page, `read.history.default_limit`) and lineage are timed as Keeper
  serves them: resolving the branch head (Domain, ref, head Revision) and running
  `reading.record_history`, without the HTTP layer and authorization.
- Overlay reads: as in the baseline, an overlay (`agent/perf`) and a copy (`perf-copy`) of
  `main` with the same commit of 1,000 Part updates, then 1,000 other updates on `main`; calls
  alternate between the two branches with the same keys. The partition then holds `main`, the
  copy (4,032,876 rows) and the overlay (2,000 rows).
- The harness connects without a statement timeout: copying the branch takes longer than the
  default `database.statement_timeout_ms` (see Open issues). Copies and deletions now run in
  batches in the background; see [Background branch copies and deletions](#background-branch-copies-and-deletions).

### Environment

Apple M5 Pro, 15 cores, 24 GiB, Darwin 25.6.0 arm64, Python 3.12.13. Postgres
16.15 with pgvector (`pgvector/pgvector:pg16`) in Docker Desktop on the same machine; the
Docker VM has 8 GiB of memory and the database is 13 GB after loading. Two runs: container
defaults (`shared_buffers` 128MB), and `shared_buffers` 2GB with `effective_cache_size` 6GB
(the tables below); `work_mem` 4MB in both. The laptop ran other work during both runs.

### Reads (milliseconds)

| Operation | Target p95 | owner p50 | owner p95 | owner p99 | rls p50 | rls p95 | rls p99 | 128MB: owner p95 | 128MB: rls p95 |
|-----------|-----------:|----------:|----------:|----------:|--------:|--------:|--------:|-----------------:|---------------:|
| get by key | 5.00 | 2.09 | 3.53 | 4.46 | 1.79 | 3.20 | 4.31 | 3.44 | 4.76 |
| list page of 50 | 20.00 | 2.80 | 4.43 | 5.64 | 2.90 | 4.66 | 5.47 | 4.50 | 5.17 |
| links of a record | 10.00 | 2.79 | 4.27 | 5.20 | 2.82 | 4.65 | 5.61 | 4.74 | 5.61 |
| 2-hop neighborhood | 30.00 | 12.68 | 16.98 | 18.40 | 7.36 | 9.48 | 11.40 | 24.42 | 21.03 |
| query by indexed attribute | 25.00 | 2.64 | 4.22 | 5.25 | 3.01 | 5.07 | 6.25 | 4.46 | 4.73 |
| full-text search | 50.00 | 5.69 | 7.79 | 10.75 | 6.16 | 8.14 | 11.40 | 7.80 | 12.24 |
| full-text search with typo-tolerant titles | 50.00 | 31.84 | **116.37** | 155.05 | 32.57 | **119.38** | 156.95 | **143.30** | **156.52** |
| record history page | 10.00\* | 5.63 | 7.82 | 8.90 | 5.66 | 7.71 | 8.81 | 7.37 | 8.33 |
| record lineage | 10.00\* | 7.12 | 9.36 | 10.54 | 7.23 | 9.33 | 10.26 | 9.81 | **10.98** |

\* No budget in design 8.7. Held here to the budget of Links of a Record, the other per-Record
index range read.

### Writes (milliseconds)

| Operation | Runs | p50 | p95 | 128MB: p50 |
|-----------|-----:|----:|----:|-----------:|
| branch creation (copy) | 2 | 84,529 | 86,178 | 94,825 |
| branch creation (overlay) | 20 | 23.68 | 27.95 | 26.25 |
| commit of 1000 changes | 5 | 2,367 | 4,276 | 1,357 |

### Overlay branches (milliseconds)

| Operation | Role | copy p50 | overlay p50 | copy p95 | overlay p95 | copy p99 | overlay p99 |
|-----------|------|---------:|------------:|---------:|------------:|---------:|------------:|
| get by key | owner | 1.98 | 2.16 | 3.60 | 3.87 | 4.49 | 4.76 |
| get by key | rls | 1.84 | 1.86 | 3.55 | 3.49 | 4.33 | 4.41 |
| list page of 50 | owner | 2.95 | 3.30 | 4.50 | 4.98 | 5.60 | 6.04 |
| list page of 50 | rls | 4.19 | 3.67 | 6.29 | 5.48 | 7.43 | 6.28 |
| links of a record | owner | 3.21 | 4.17 | 4.88 | 6.08 | 6.39 | 7.36 |
| links of a record | rls | 4.11 | 4.46 | 6.14 | 6.42 | 7.30 | 7.88 |
| 2-hop neighborhood | owner | 22.99 | 25.91 | 29.35 | **34.47** | 48.29 | 53.24 |
| 2-hop neighborhood | rls | 26.19 | 24.27 | **33.47** | **30.03** | 36.59 | 33.10 |
| query by indexed attribute | owner | 2.83 | 3.15 | 4.70 | 4.81 | 5.41 | 5.70 |
| query by indexed attribute | rls | 3.05 | 3.42 | 4.92 | 5.32 | 6.16 | 6.52 |
| full-text search | owner | 6.44 | 6.86 | 8.89 | 8.82 | 12.43 | 11.90 |
| full-text search | rls | 6.91 | 7.54 | 9.00 | 9.40 | 12.25 | 12.75 |
| full-text search with typo-tolerant titles | owner | 27.61 | 33.48 | **70.69** | **120.43** | 103.06 | 152.87 |
| full-text search with typo-tolerant titles | rls | 28.25 | 35.19 | **70.81** | **122.66** | 103.24 | 159.41 |
| record history page | owner | 5.69 | 5.73 | 7.66 | 7.90 | 9.03 | 9.13 |
| record history page | rls | 6.03 | 5.97 | 8.90 | 8.52 | 10.62 | 10.79 |
| record lineage | owner | 6.86 | 7.34 | **10.38** | **10.24** | 14.09 | 14.10 |
| record lineage | rls | 6.86 | 7.20 | **10.41** | **10.57** | 13.64 | 14.53 |

With 128MB of shared buffers the same table differs by a few milliseconds; the neighborhood
was at 29.45 to 33.02 at p95 and typo-tolerant search at 149 to 168.

### Reading the results

- Key, page, Links, attribute and full-text reads stay index-driven and within their budgets
  for both roles, on `main`, the copy and the overlay, with both memory settings; p99 is
  under 16 ms for all of them. They grew by 2 to 10 ms at p95 from the 100k baseline, mostly
  in pages read from outside shared buffers.
- History and lineage read one primary-key range of `mat_history` per Record and are flat in
  the size of the Domain. Lineage made about eight statements in this run (Domain, ref and head
  Revision, then the Record, its history range, provenance and Proposals) and sat at its
  proposed budget: 9.3 ms at p95 on `main` with 2GB of buffers, 9.8 to 11.0 on branches and
  with 128MB. It now reads the Record and its lineage in one statement, about 25% faster at
  100k ([Record lineage in one statement](#record-lineage-in-one-statement-100k-records)); not
  re-measured at 1M.
- The 2-hop neighborhood is within budget on `main` (9.5 to 24.4 ms at p95) and over it on the
  copy and the overlay (29 to 34 ms). The plans are the baseline's (index scans only, about
  900 buffers per call for about 130 nodes and 130 Links); the time goes to the pages missing
  from shared buffers and from the 8 GiB VM's page cache. The alternating measurement reads
  two branches of a partition holding two full copies of the Domain, with random keys, and
  repeated runs over the same keys
  fall from 23 to 14 ms at p50 as the cache warms. Nothing in the statement scales with the
  Domain; the budget holds when the working set is in memory, which the reference hardware of
  design 6.14 (32 GB) gives and this VM does not.
- Typo-tolerant title search misses its budget in this run, at 1.4 to 3.4 times it at p95,
  on every branch and for both roles. Plain full-text search stays at 8 to 12 ms. With the
  word lexicon it is 7.6 to 14 ms ([1M Records: word lexicon](#1m-records-word-lexicon)).
- Branch creation by copy grows with the Domain (85 to 95 s for 4 million rows); an overlay
  takes 24 to 26 ms, as at 100k. A commit of 1,000 Part updates takes 1.4 to 2.4 s at p50
  (0.36 s at 100k); the load committed 10,000 changes per Revision in about 4.8 s each, at the
  5 s target of design 6.14.

### 1M Records: word lexicon

The same fixture and harness on 2026-10-22 after migration `0018`: typo tolerance corrects a
query word the branch does not hold from the branch's word lexicon (`mat_lexicon`, trigram
GIN index on the words) and runs full-text search with the corrections. The timed read takes
a title word with two letters in its middle swapped (`harness.misspelled`), so every call
looks the word up, finds its corrections and searches with them; the run before searched the
word as written. `shared_buffers` 2GB, 1,000 calls per read and role; the fixture was loaded
again (1,831 s for the load, against 1,961 s before: the title trigram index is gone and the
triggers count the words).

| Operation | Target p95 | owner p50 | owner p95 | owner p99 | rls p50 | rls p95 | rls p99 |
|-----------|-----------:|----------:|----------:|----------:|--------:|--------:|--------:|
| full-text search | 50.00 | 4.42 | 5.85 | 10.33 | 4.62 | 6.04 | 10.60 |
| typo-tolerant full-text search | 50.00 | 5.49 | 7.55 | 11.79 | 5.74 | 7.91 | 11.97 |
| typo-tolerant, copy branch | 50.00 | 7.62 | 11.80 | 14.19 | 7.77 | 12.99 | 18.22 |
| typo-tolerant, overlay branch | 50.00 | 8.26 | 12.35 | 16.35 | 8.47 | 13.98 | 20.48 |

At p95 that is 7.6 to 14 ms against 116 to 123 ms for the whole-title trigram match (15 times
less on `main`), and the overlay stays within 10% of the copy. The other reads stayed in the
range of the first run (the neighborhood on branches 34 to 42 ms at p95, as before over its
budget when the working set leaves memory), except the commit, measured right after the two
copies of 4 million rows each with the page cache full of them (5.5 s at p50). Measured again
on a copy of the loaded fixture, warm, four commits of 1,000 Part updates per setting, twice:

| Commit of 1000 changes (ms) | first round | second round |
|-----------------------------|-------------|--------------|
| lexicon triggers on | 653, 699, 727, 755 | 654, 700, 702, 711 |
| lexicon triggers off | 675, 684, 690, 700 | 642, 665, 666, 749 |

The triggers cost about 5% of a commit at this size (the run before this change measured 1.4
to 2.4 s at p50 cold). The run used the change without the per-Kind split of the lexicon
(`kind_id` in its key, which narrows corrections to the Kinds a caller may read); lookups and
statements are otherwise the same.

### 1M Records: run with the 10M harness

The same fixture measured again on 2026-10-22 in the evening with the code of the 10M run
(`make perf-1m`: measured in place, a full copy and an overlay, paired reads alternating which
branch reads first) while other workloads kept the machine busy (load average 12 to 166,
median 21). p95 in milliseconds; `rls` reads the keys `owner` just read, so it is the warm
figure.

| Read | Budget | owner | rls | copy, rls | overlay, rls |
|------|-------:|------:|----:|----------:|-------------:|
| get by key | 5 | 13.26 | 8.33 | 7.69 | 7.49 |
| list page of 50 | 20 | 19.45 | 10.86 | 11.06 | 12.06 |
| links of a record | 10 | 28.78 | 9.66 | 12.88 | 13.64 |
| 2-hop neighborhood | 30 | 129.84 | 20.47 | 64.51 | 77.23 |
| query by indexed attribute | 25 | 8.95 | 11.12 | 7.44 | 8.35 |
| full-text search | 50 | 25.30 | 19.53 | 14.42 | 17.83 |
| typo-tolerant full-text search | 50 | 27.18 | 19.92 | 17.73 | 22.32 |
| partial-match full-text search | 50 | 46.32 | 36.13 | 31.15 | 37.56 |
| record history page | 10\* | 19.26 | 15.76 | 13.46 | 14.58 |
| record lineage | 10\* | 15.41 | 14.43 | 13.29 | 12.77 |

- Every figure is two to four times the morning's run, `full-text search` included, whose code
  did not change: the machine, not the code, moved. Search, typo tolerance and partial
  matching stay within budget at 1M.
- The neighborhood is the clearest case of cold against warm pages: 130 ms at p95 for the
  first pass over random keys, 20 ms for the second pass over the same keys, 65 to 77 ms on
  the copy and the overlay, whose rows are other pages. The overlay costs 20% more than the
  copy at p50 and p95 in this run.
- Writes: a copy is a 165 ms request and a 408 s background job (2,710 batches, 142 ms at p50,
  4 million rows); deleting it a 37 ms request and a 377 s job; an overlay 49 ms; a commit of
  1,000 changes 3.5 s at p50.
- Agent trace, through Keeper: +1 to +6% at p95 and p50 for get by key, search and the
  neighborhood, within the run's noise.

### Open issues

- **Typo-tolerant title search** (fixed since this run, see
  [1M Records: word lexicon](#1m-records-word-lexicon)). `title % query` on the GIN trigram index `(domain_id, branch,
  title gin_trgm_ops)` is lossy: the index keeps a row when it holds enough of the query's
  trigrams (3 of 9 for `supplier` at 0.3), whatever the length of the title. On this fixture
  the index returns 154,298 rows for `supplier` and every one fails the recheck: the sentence
  titles of two Kinds (33 to 64 characters, 243,000 Records) contain the word but are never
  30% similar to it. The bitmap build (60 to 100 ms) and the heap recheck dominate;
  `LIMIT search_candidates` cannot stop a bitmap scan. Two alternatives were measured and not
  adopted. A GiST trigram index rechecks exactly and stops at the limit when matches exist,
  but a query without matches walks the index (100 ms for `supplier`). Bounding the title's
  trigram count (a similarity of at least t needs at most |query| / t trigrams in the title)
  with nested partial GIN indexes on `cardinality(show_trgm(title)) <= 16, 32, 48`, which the
  security definer function would pick through a literal bound, took p50 from 49 to 9 ms and
  p95 from 246 to 102 ms over 300 workload words: still twice the budget, for three more
  indexes to maintain on every commit. The decision taken: trigram matching on the distinct
  words of each branch (migration `0018`), the approach of the pg_trgm documentation for
  unique words.
- **Copy branches at 1M Records** (fixed since this run). Creating a copy (`INSERT ...
  SELECT` of the Records, Links and history of `main`) took 85 to 95 s, and deleting it also
  took longer than 30 s: both exceeded the default `database.statement_timeout_ms`, so `POST
  .../branches` with `mode: copy` and branch deletion failed at this size. Copies and
  deletions of large branches now run as jobs in batches, each well within the timeout
  ([Background branch copies and deletions](#background-branch-copies-and-deletions)); the
  1M-Record run has not been repeated since.

## Record lineage in one statement (100k Records)

Lineage at a branch head is one statement since migration `0032`: the materialized Record keeps
the Revision behind each value (`mat_records.lineage_revisions`), and the Record, those
Revisions, the provenance entries, the Proposals, the creating change and the count of changes
come back in one row. Before, Keeper read the Record, then the newest
`read.history.lineage_max_changes` (10,000) changes of the Record, then provenance and
Proposals, and a value set before that window named no Revision.

Measured on 2026-10-22 on the 100k-Record dataset above (scale 540, 4 extra Links per Part),
same machine, while other workloads kept it busy (load average about 28): get by key took
1.9 ms at p50 against 1.1 in the baseline, so read the absolute figures as inflated. The three
reads ran in turn call by call with the same random keys (1,000 timed calls after 100 warm-up
calls per role), so they saw the same load; "before" is the previous implementation on the
same database. Times include resolving the branch head (Domain, ref, head Revision).

| Operation | Role | p50 | p95 | p99 |
|-----------|------|----:|----:|----:|
| get by key | owner | 1.95 | 3.60 | 5.17 |
| record lineage, before | owner | 6.20 | 11.36 | 15.25 |
| record lineage | owner | 4.42 | 8.18 | 11.28 |
| get by key | rls | 1.92 | 4.04 | 7.00 |
| record lineage, before | rls | 5.47 | 11.23 | 16.47 |
| record lineage | rls | 4.27 | 8.43 | 11.67 |

- Lineage is 22 to 29% faster at p50 and 25 to 28% at p95; relative to get by key it went
  from about 3x to about 2.2x. The full harness in the same conditions gave the same order
  (lineage now below the history page, which was above it before).
- Its cost no longer depends on the number of changes of the Record: the history range is read
  only to count the changes (an index-only scan, stopped at `read.history.lineage_count_max`)
  and, for rows written before migration `0032` (null `lineage_revisions`) that the
  `store.lineage_backfill` job has not filled yet, to find the Revisions they lack among the
  newest `read.history.lineage_scan_changes` changes, read once per request.
- Lineage at another Revision (`at`) runs the same statement on a snapshot of the Revision
  (`reading.snapshots`); it was not measured here.

## 10M Records

The M5 target: the harness at 10,001,100 Records and 30,327,660 Links on `main`, run on
2026-10-22. Every read stays index-driven (plans as at 1M), but on this machine only the reads
that touch one or two pages per call meet their budget; the neighborhood, full-text search and
the history reads do not ([Reading the results](#10m-records-reading-the-results)). The
machine was shared with other test workloads during the whole run (load average 14 to 209 on
15 cores, median 116), so read every figure as an upper bound.

### Method

```sh
make perf-10m        # Postgres of tests/perf/compose.yaml, then the command below
make perf-up PERF_VM=16g  # only start it, sized for a Docker VM of 16 GiB (default 8g)
make perf-down       # deletes the Postgres volume and the seeded databases
```

which runs, against the Postgres of `tests/perf/compose.yaml` (its own compose project
`lorebase-perf`, port 55410, volume `data`):

```sh
LOREBASE_TEST_DATABASE_URL=postgresql://perf:perf@127.0.0.1:55410/postgres \
uv run python tests/perf/bench.py --scale 54060 --replicas 10 --extra-links 4 \
  --iterations 1000 --warmup 100 --branch-iterations 1 --copy-batches 200 --no-pair-copy \
  --seed-database perf_seed_10m --in-place --out build/perf/perf-10m.md
```

- **Fixture.** `--replicas 10` loads a tenth of the dataset (scale 5,406: 1,000,110 Records,
  3,032,766 Links, the 1M fixture) through the commit pipeline, then copies its materialized
  rows (`mat_records`, `mat_links`, `mat_history`) nine times with renumbered keys in set-based
  SQL (`harness.replicate`): Supplier `S-1001` becomes `S-163181` in the first copy, foreign-key
  values and Link ends follow, unique values (VAT numbers) get the copy's number, Risk flag
  ULIDs a first character of their own. The word lexicon is counted again from the search
  vectors, the per-Attribute indexes of `main` and the GIN indexes are built once at the end.
  Only the materialized head is copied, which is what every read reads; the Revisions, trees
  and objects stay those of the first tenth, so the copies' rows point to its Revisions.
- **Why.** Through the commit pipeline the 10M load ran at 10 to 20 s per commit of 10,000
  changes on this machine once the first million Records were in, about ten hours in all:
  every commit inserts 10,000 objects and tree nodes under random content hashes, so each one
  is a random index read and write once those indexes outgrow memory, at about 0.26 ms per page
  read in this VM. The pipeline itself was made faster on the way (see
  [Fixes](#10m-records-fixes-found-on-the-way)); the replicated fixture takes about an hour after
  the 1M load.
- `--seed-database perf_seed_10m` keeps the fixture; `--in-place` measures it directly (a
  template copy would need another 63 GB) and removes the branches a run creates, so it can be
  measured again without loading (the run's commits stay on `main`). An interrupted load
  resumes where it stopped. The fixture is kept on the perf Postgres; `make perf-down` deletes
  it.
- 1,000 timed calls per read and role after 100 warm-up calls, random keys; `owner` (not
  subject to row-level security) first, then `rls` (subject to `domain_isolation`).
- `--copy-batches 200` measures the background copy of `main` for 200 batches (request,
  batches, throughput) and deletes what was copied: a full copy of 80 million rows would take
  hours here. `--no-pair-copy` compares the overlay with `main` itself instead of with a full
  copy. Paired measurements alternate which branch reads first since this run; in this run
  `main` always read first, so the overlay read pages `main` had just loaded.
- Agent trace overhead: the Keeper application in process (`tests/perf/trace_overhead.py`),
  each request with and without `X-Lorebase-Trace: on`, response cache and rate limits off.

### Environment

Apple M5 Pro, 15 cores, 24 GiB, Darwin 25.6.0 arm64, Python 3.12.13. Postgres 16.15 with
pgvector (`pgvector/pgvector:pg16`) in Docker Desktop, VM of 8 GiB; `shared_buffers` 2GB,
`effective_cache_size` 6GB, `work_mem` 4MB, `maintenance_work_mem` 512MB, `max_wal_size` 16GB
(`tests/perf/compose.yaml`). The database is 64 GB: `mat_history` 23 GB, `mat_records` 20 GB, `mat_links` 13 GB with their indexes.

### Reads (milliseconds)

| Operation | Target p95 | owner p50 | owner p95 | owner p99 | rls p50 | rls p95 | rls p99 |
|-----------|-----------:|----------:|----------:|----------:|--------:|--------:|--------:|
| get by key | 5.00 | 4.03 | **6.08** | 7.49 | 5.02 | **7.15** | 9.42 |
| list page of 50 | 20.00 | 7.39 | 9.59 | 11.17 | 10.34 | 15.85 | 19.41 |
| links of a record | 10.00 | 6.87 | 9.15 | 10.58 | 10.70 | **15.97** | 20.46 |
| 2-hop neighborhood | 30.00 (50.00 in 6.14) | 63.32 | **95.64** | 109.83 | 114.25 | **152.84** | 186.98 |
| query by indexed attribute | 25.00 | 2.84 | 5.10 | 6.85 | 6.62 | 11.99 | 17.68 |
| full-text search | 50.00 | 36.36 | **58.72** | 97.23 | 66.35 | **110.92** | 170.85 |
| typo-tolerant full-text search | 50.00 | 52.64 | **97.44** | 156.33 | 63.78 | **127.35** | 177.40 |
| partial-match full-text search | 50.00 | 160.03 | **262.83** | 329.46 | 168.80 | **272.95** | 332.85 |
| record history page | 10.00\* | 11.75 | **17.36** | 24.20 | 12.61 | **27.75** | 43.69 |
| record lineage | 10.00\* | 11.24 | **16.70** | 22.05 | 13.54 | **23.66** | 51.97 |

\* No budget in design 8.7, held to the budget of Links of a Record. Bold: over budget.

### Agent trace overhead (milliseconds)

| Request | off p50 | on p50 | off p95 | on p95 |
|---------|--------:|-------:|--------:|-------:|
| get by key | 17.15 | 16.25 | 40.86 | 33.09 |
| full-text search | 98.57 | 94.72 | 174.33 | 162.49 |
| 2-hop neighborhood | 132.49 | 42.06 | 190.10 | 86.22 |

200 timed calls each; all 660 traced calls were written as steps. No overhead shows: the step
is captured and queued on the request and written off the request path, and the differences are
noise or, for the neighborhood, the second call of each pair reading the pages the first one
loaded (the trace-on call went second in this run). Through Keeper a read costs 10 to 60 ms
more than the store primitive here (the application in process, under this machine's load).

### Writes (milliseconds)

| Operation | Runs | p50 | p95 | max |
|-----------|-----:|----:|----:|----:|
| branch creation (copy), request | 1 | 146.90 | 146.90 | 146.90 |
| branch copy, 200 batches | 1 | 34,633 | 34,633 | 34,633 |
| branch copy, one batch | 200 | 89.78 | 293.65 | 5,708 |
| branch deletion, request | 1 | 74.46 | 74.46 | 74.46 |
| branch deletion, background job | 1 | 6,262 | 6,262 | 6,262 |
| branch deletion, one batch | 43 | 153.26 | 218.55 | 343.92 |
| branch creation (overlay) | 20 | 42.18 | 69.17 | 76.60 |
| commit of 1000 changes | 5 | 3,295 | 4,047 | 4,047 |

- Branch creation answers in 42 ms (overlay) and 147 ms (copy, which starts as an overlay)
  at 10M Records, as at 100k: neither depends on the size of the Domain.
- The background copy read 168,132 rows in 200 batches, 4,855 rows per second, each batch
  within the statement timeout (p95 0.29 s, slowest 5.7 s while other workloads held the
  machine). At that rate the 80 million rows of `main` take about 4.6 hours here; the copy is
  readable and writable from the request on.
- A commit of 1,000 Part updates takes 3.3 s at p50 (0.65 to 2.4 s at 1M, 0.4 s at 100k):
  validation and materialization read the 1,000 Records and their neighbors, now mostly from
  disk. Design 6.14 asks for 10,000 Record changes in under 5 s; the seed commits of 10,000
  changes averaged 9.8 s at 1M Records while the 10M load ran beside them, and were not
  measured alone at 10M.

### Overlay against `main` (milliseconds)

The overlay `agent/perf` got 1,000 Part updates and `main` 1,000 others since the fork (2,000
rows of the overlay's own). `main` read first in each pair, so the overlay read warm pages; the
table compares the cost of the overlay statements on warm pages, not the branches.

| Operation | Role | main p50 | overlay p50 | main p95 | overlay p95 |
|-----------|------|---------:|------------:|---------:|------------:|
| get by key | rls | 4.21 | 4.13 | 12.22 | 10.12 |
| links of a record | rls | 9.98 | 6.60 | 20.97 | 16.23 |
| 2-hop neighborhood | owner | 110.16 | 15.52 | 180.52 | 29.56 |
| 2-hop neighborhood | rls | 108.97 | 19.56 | 175.60 | 52.40 |
| query by indexed attribute | rls | 6.27 | 6.85 | 20.95 | 20.66 |
| full-text search | rls | 72.09 | 82.79 | 121.45 | 151.53 |
| record lineage | rls | 11.08 | 9.99 | 29.18 | 28.33 |

Search on the overlay costs 15 to 25% more than on `main` (it merges the overlay's matches with
the base's), the other reads the same or less on warm pages. The full table is in the run's
report (`build/perf/perf-10m.md`).

### 10M Records: reading the results

- **One-page reads meet their budget.** Get by key, a page of 50, Links of a Record and the
  attribute query read one index range and one or a few heap pages: 5 to 16 ms at p95, at or
  within budget for `owner` and within 1 to 6 ms of it for `rls`. Get by key takes 4 to 5 ms at
  p50 where it took 1.1 at 100k: the same plan, a heap page missing from memory and this
  machine's load (a 1,110-Record database measured 4 ms at p50 for the same read during the
  run).
- **The neighborhood is bound by pages read from disk.** A call reads about 400 pages that
  are not in shared buffers (130 Records and 130 Links at random keys), 0.26 ms each here:
  100 to 150 ms at p95. The same keys read a second time take 15.5 ms at p50 and 29.6 at p95
  (the overlay rows above). See [2-hop neighborhood on branches](#2-hop-neighborhood-on-branches).
- **Full-text search grows with the matches of the term.** The GIN index returns every match
  of a term before `store.reads.search_candidates` bounds the ranking: `bearing` matches
  240,890 Records here and the bitmap alone takes 129 ms. The generator writes titles from 695
  distinct words (median 6,200 Records per word, 90th percentile 175,000), so the workload's
  words are frequent; real titles are far more varied. `gin_fuzzy_search_limit` was tried and
  returns nothing on the composite `(domain_id, branch, search)` index. Partial matching runs
  the full match, an OR query and the term count, so it pays this three times.
- **History and lineage** read the history range of a Record and the Revisions, provenance and
  Proposals it names: five to eight index lookups, each a page from disk at this size (17 to
  28 ms at p95, against 8 to 10 at 1M).
- With the reference hardware of design 6.14 (8 vCPU, 32 GB, NVMe) the hot indexes and a
  larger share of the heap fit in memory and a page read costs a fraction of this VM's; this
  run does not show whether the neighborhood and search budgets hold there.

### 10M Records: fixes found on the way

Pushed during this work, each measured and covered by the store and core tests:

| Cause | Fix | Effect |
|---|---|---|
| Existing Links looked up by `(from, to)` row-value `IN` lists: Postgres planned one bitmap index scan per pair (0.35 s of planning for 1,000 pairs) | One semi-join on two unnested arrays (`bulk.pairs_in`) | 485 ms to 13 ms per chunk of 1,000 pairs |
| Cardinality and required-Link checks summed the batch's Links once per key (quadratic in the batch) | Counts per key, once | 15 s of a 140 s profiled load gone |
| Search vectors of a commit's Records refreshed by filtering every Record of the Kind on the commit's Revision | Refreshed by key | 7 s to milliseconds per statement for a Kind of 200,000 Records |
| Unique values checked with `values -> attr IN (...)`, which the unique index on `values ->> attr` does not match | The index's expression and predicate | A minute to milliseconds per 1,000 values at 160,000 Suppliers |
| The background copy counted the base's 85 million rows for its progress report, past the statement timeout: copies failed at 10M | The planner's estimate, replaced by the rows read at the switch | Copies start at 10M |
| Canonical JSON wrote strings character by character | Strings without characters to escape in one step | 18 s to 1.4 s of a profiled load |

Harness changes: the dataset is generated in shards of at most scale 6,000 (bounded memory),
`replicate`, the GIN indexes built after the load, materialized rows sent through `COPY` from
500 rows on during the load, resuming an interrupted load, `--in-place`, `--seed-only`,
`--copy-batches`, `--no-pair-copy`, the agent trace measurement and paired measurements that
alternate which side reads first.

### 2-hop neighborhood on branches

At 1M Records the neighborhood was over budget on the copy and the overlay (29 to 42 ms at
p95) and within it on `main`. Measured again for this run, the cause is the same at both sizes
and on every branch: pages missing from memory, not the plan. One call reads about 130
Records and 130 Links with index scans only, about 950 buffers; what changes is how many of
them are already in shared buffers or the VM's page cache.

- Buffers per call (`EXPLAIN (ANALYZE, BUFFERS)` of the statement, 30 calls with fresh random
  keys, plain SQL as `owner`, copy branch of the 1M fixture): the hydration of the returned
  Records read 248 pages from outside shared buffers (one index scan per node, about one heap
  page each) and the inbound Links of the walk 152 (one heap page per Link found through the
  reverse index), of 427 read in all.
- Time per page read at 10M on this machine: 0.26 ms (`track_io_timing`, 209 reads in 54 ms).
  About 400 reads per call is 100 ms, which is what the cold calls measure.
- The same keys read a second time: in the overlay comparison of this run each call read the
  keys `main` had just read, and took 15.5 ms at p50 and 29.6 at p95 against 110 and 181 for
  `main` (the harness now alternates which branch reads first).
- At 1M Records, with the reads alternating fairly, the copy measured 64.5 ms and the overlay
  77.2 ms at p95 (`rls`), and `main` 20.5 ms on keys the first pass had just read
  ([1M Records: run with the 10M harness](#1m-records-run-with-the-10m-harness)).

Options evaluated:

| Option | Measured | Result |
|---|---|---|
| Header-only hydration through a covering index `(domain_id, branch, kind_id, key) INCLUDE (title)`, values on request | 1M copy branch, 30 calls: hydration reads 86 pages instead of 248 (index-only scan; the index is 215 MB against 3.3 GB of heap); the walk's inbound Links still read 139 | A third fewer page reads per call, not enough cold (about 70 ms), and it changes the response of `POST /neighborhood` (nodes without values) and needs a second index on every branch of every partition, maintained on each commit |
| Physical locality (`CLUSTER`, ordered copies) | `mat_records` and `mat_links` are already written in primary-key order by the load and by the copy job; the neighbors of a Record are other Records with unrelated keys | Nothing to gain: each neighbor is a different page whatever the order |
| Lighter payloads for Links too (walk without Link values) | The inbound walk would need `from_key` in the reverse index to run index-only; the covering reverse index with values was withdrawn because of the 2.7 KB entry limit | Same API change as above, plus a wider reverse index |

None of them brings a cold neighborhood within 30 ms on this machine, and each changes the
API or adds an index to every branch, so none was adopted. The budget holds when the pages a
neighborhood touches are in memory, which the reference hardware of design 6.14 (32 GB) gives
for 1M Records and this 8 GiB VM does not; at 10M Records (58 GB of tables and indexes) only
the hot part of the graph fits on either. The opt-in `values: false` on `POST /neighborhood`
now exists without the covering index; it shrinks the response to a third and leaves the time
unchanged ([Headers-only neighborhoods](#headers-only-neighborhoods)).

## Search on very common terms

Measured on 2026-10-23 with `tests/perf/focus.py` on the seed databases with realistic text
(`--vocabulary zipf`: names and descriptions drawn from 40,000 terms with Zipf frequencies),
`perf_seed_1m_zipf` (1,000,110 Records) and `perf_seed_10m_zipf` (10,001,100 Records). The
30 most common words of the workload match 27,063 to 268,883 Records at 1M and 270,630 to
2,688,830 at 10M. Each query is run twice in turn, alternating which goes first: "before"
ranks at most `store.reads.search_candidates` (1,000) matches whatever the terms
(`search.common_term_docs: 0`), "lexicon" sizes the ranking from the word lexicon. Search as
Keeper's lexical search calls it (all Kinds, typo corrections, partial matching), plain store
calls as `owner`. "Truncated" counts the calls of 300 (100 at 10M) whose candidates were cut.

The machine was shared with other workloads throughout (load average 21 to 29 on 15 cores
during the 1M runs, 41 to 48 during the 10M run), so the absolute times vary by a factor of 2
to 3 between runs: compare the two columns of one run, not runs with each other.

1M Records, p50 / p95 in milliseconds:

| Settings (`docs` / `candidates`) | Query | before | lexicon | truncated, before / lexicon |
|---|---|---:|---:|---:|
| 2,000 / 1,000 | title word | 13.9 / 48.3 | 16.2 / 53.8 | 50 / 29 |
| 2,000 / 1,000 | common word | 35.9 / 74.9 | 36.5 / 73.5 | 300 / 300 |
| 2,000 / 1,000 | two common words | 28.9 / 82.0 | 32.9 / 74.1 | 251 / 251 |
| 2,000 / 1,000 | title word and a common one | 12.8 / 39.4 | 14.7 / 45.6 | 43 / 43 |
| 2,000 / 1,000 | misspelled title word | 23.5 / 50.2 | 25.7 / 61.2 | 38 / 18 |
| 5,000 / 1,000 | title word | 13.7 / 49.5 | 16.3 / 63.1 | 50 / 17 |
| 5,000 / 1,000 | misspelled title word | 26.8 / 81.6 | 29.3 / 86.4 | 38 / 8 |
| 20,000 / 5,000 | title word | 41.2 / 315.9 | 51.7 / 1,066.6 | 50 / 5 |
| 20,000 / 5,000 | common word | 66.3 / 196.5 | 146.8 / 732.1 | 300 / 300 |
| 20,000 / 5,000 | two common words | 48.9 / 153.4 | 133.8 / 413.5 | 251 / 148 |
| 20,000 / 5,000 | title word and a common one | 18.1 / 63.7 | 21.4 / 133.6 | 43 / 38 |
| 20,000 / 5,000 | misspelled title word | 28.2 / 88.7 | 33.9 / 139.4 | 38 / 3 |

10M Records, p50 / p95 in milliseconds:

| Settings | Query | before | lexicon | truncated, before / lexicon |
|---|---|---:|---:|---:|
| 2,000 / 1,000 | title word | 42.1 / 186.5 | 47.9 / 237.8 | 144 / 115 |
| 2,000 / 1,000 | common word | 248.3 / 506.2 | 251.7 / 511.0 | 300 / 300 |
| 2,000 / 1,000 | misspelled title word | 73.7 / 401.3 | 86.3 / 526.7 | 141 / 101 |
| 5,000 / 1,000 | title word | 67.9 / 343.1 | 102.6 / 861.5 | 144 / 68 |
| 20,000 / 5,000 | title word | 147.0 / 744.5 | 495.3 / 3,279.6 | 55 / 13 |
| 20,000 / 5,000 | common word | 352.8 / 748.1 | 526.6 / 1,238.8 | 100 / 100 |
| 20,000 / 5,000 | two common words | 269.5 / 640.3 | 954.1 / 2,266.0 | 100 / 91 |
| 20,000 / 5,000 | title word and a common one | 81.0 / 455.5 | 87.1 / 1,378.1 | 29 / 14 |
| 20,000 / 5,000 | misspelled title word | 166.0 / 521.3 | 365.6 / 2,791.3 | 52 / 7 |

(The 2,000 and 5,000 runs at 10M used 300 calls per case, the 20,000 run 100.)

- With the first defaults (20,000 and 5,000) almost every title-word search ranked every
  match, but p95 grew 1.6 to 3.7 times at 1M and 1.7 to 5.4 times at 10M: ranking 5,000
  candidates of a common word, and every match of a word in up to 20,000 Records, reads their
  rows and search vectors. The defaults are now 2,000 and 1,000: common-term queries rank as
  many candidates as before, rare terms (within 2,000 Records) have every match ranked, and
  the cost is 0.6 to 4 ms at p50 at 1M and 3 to 13 ms at 10M. Raise `search.common_term_docs` where the
  hardware keeps the search vectors in memory.
- Searches of very common words stay over budget at 10M either way: the GIN bitmap of a word
  in 270,000 to 2.7 million Records is built before any ranking. Narrowing the query (a Kind,
  a filter, a less common word) is what brings them down; `truncated_candidates` tells the
  caller when to.

## BM25 search

Measured on 2026-10-23 with `tests/perf/bm25.py` on `perf_seed_1m_zipf` (1,000,110 Records,
Zipf text as in [search on very common terms](#search-on-very-common-terms)), the perf Postgres
on the ParadeDB image (`make perf-up PERF_VM=16g PERF_SEARCH=bm25`: Postgres 16.15,
`pg_search` 0.26.3, `shared_buffers` 6GB) in a Docker VM of 16 GiB on an Apple M5 Pro shared
with other work (load average about 9). Search as Keeper's lexical search calls it (typo
corrections, partial matching, ranking sized by the word lexicon for `postgres`), owner role,
200 timed calls per case after 20, both backends called in turn with the same queries; cold: a
new random query per call, warm: the same twenty queries in turn
([search](search.md#bm25-ranking) describes the backend).

Reproduce:

```sh
make perf-up PERF_VM=16g PERF_SEARCH=bm25
LOREBASE_TEST_DATABASE_URL=postgresql://perf:perf@127.0.0.1:55410/postgres \
  uv run python tests/perf/bm25.py --database perf_seed_1m_zipf --out build/perf/bm25-1m.md
```

Index: built in 15 to 19 s (concurrently, partition by partition), 75 to 99 MB depending on
the segments it ends with; the GIN search index of the same rows is 96 MB, the table 1,958 MB.

`main`, after `VACUUM`, p50 / p95 in milliseconds:

| Query | Calls | postgres | bm25 | truncated, postgres / bm25 (of 200) |
|---|---|---:|---:|---:|
| title word | cold | 9.1 / 21.1 | 12.3 / 17.8 | 19 / 0 |
| title word | warm | 9.8 / 16.2 | 12.8 / 18.9 | 0 / 0 |
| common word | cold | 23.2 / 48.7 | 15.7 / 27.3 | 200 / 0 |
| common word | warm | 19.2 / 34.8 | 13.8 / 19.8 | 200 / 0 |
| two common words | cold | 19.4 / 40.1 | 14.4 / 32.6 | 164 / 0 |
| two common words | warm | 20.7 / 39.8 | 15.9 / 39.3 | 170 / 0 |
| title word and a common one | cold | 8.6 / 24.5 | 11.2 / 25.4 | 29 / 0 |
| title word and a common one | warm | 8.1 / 17.3 | 10.8 / 21.8 | 20 / 0 |
| misspelled title word | cold | 13.0 / 25.1 | 19.2 / 27.9 | 9 / 0 |
| misspelled title word | warm | 16.4 / 31.5 | 22.0 / 40.0 | 10 / 0 |
| phrase of two common words | cold | 18.5 / 42.2 | 21.0 / 55.4 | 33 / 0 |
| phrase of two common words | warm | 15.5 / 35.0 | 17.8 / 43.6 | 40 / 0 |

`main` while another connection commits 100 Part updates every second (15 commits during the
run), cold:

| Query | postgres | bm25 |
|---|---:|---:|
| title word | 9.5 / 22.4 | 21.6 / 36.6 |
| common word | 20.8 / 40.2 | 20.5 / 38.5 |

An overlay with 108 rows of its own (100 Part updates on it, 100 on `main` after the fork):

| Query | Calls | postgres | bm25 |
|---|---|---:|---:|
| title word | cold | 9.9 / 19.9 | 28.6 / 39.6 |
| common word | cold | 21.9 / 40.6 | 35.7 / 52.9 |
| two common words | cold | 21.1 / 42.6 | 36.7 / 71.1 |
| misspelled title word | cold | 21.9 / 44.9 | 65.8 / 95.4 |
| phrase of two common words | cold | 27.5 / 74.0 | 64.8 / 176.4 |

Commits of 100 Part updates on `main` (20 timed after 20 untimed, each a value no commit wrote
before): 244 / 611 ms at p50 / p95 without the index, 223 / 359 with it; within the noise of a
shared machine, so maintaining the index costs no measurable time at this size.

- Every match is ranked: no BM25 answer was truncated, against every common-word query and
  most two-word queries of the default ranking. Very common words are 20 to 35% faster at p50
  and up to 45% at p95; rare words are about 3 ms slower: a top-k scan of `pg_search` costs 2
  to 4 ms whatever the term, where a rare word costs the GIN index about a millisecond.
- **Writes slow BM25 down until `VACUUM`.** The index keeps the versions of updated rows until
  `VACUUM` marks them deleted, and each scan then checks more of them: right after 5 commits of
  100 updates a rare-word scan took 15 to 16 ms instead of 3 to 4, back to 3 to 4 ms after
  `VACUUM` of the partition. Under the steady writes above a rare word costs 22 ms at p50
  instead of 12. Where BM25 is on and Records change often, have autovacuum visit
  `mat_records` sooner than its default 20% of changed rows, for example
  `ALTER TABLE mat_records_p<n> SET (autovacuum_vacuum_scale_factor = 0.01,
  autovacuum_vacuum_threshold = 1000)` on the partitions (open: not applied by
  `storage apply`, not measured at scale).
- **Overlays cost more with BM25.** The overlay's rows and the base's are two scans per query
  arm, each paying the fixed cost of a scan, and the base's reads one more row for each row the
  overlay holds; typo corrections add an arm, so a misspelled word on an overlay is four scans.
  Before the functions turned parallel workers off, the planner started two workers for the
  overlay's own rows (19 ms of a 33 ms call). Keep `search.backend: postgres` where most
  searches run on overlays.
- Phrases of two common words: every row holding both words is a candidate, the rows with them
  in sequence ranked first; the recheck of four times the page answered every call exactly.
- **Not measured:** 10M Records (`make perf-up PERF_VM=16g PERF_SEARCH=bm25`, then the command
  above with `--database perf_seed_10m_zipf`), the role subject to row-level security, and
  Keeper over HTTP.

## Headers-only neighborhoods

`values: false` on `POST /neighborhood` returns Record headers and Link endpoints. Measured on
2026-10-23 in the same runs (`tests/perf/focus.py`, Keeper in process with the read cache
off, 2-hop neighborhood of a random Part, `main` and an overlay branch created for the run).
Cold: every call on a new random Part, the two variants on different Parts, alternating
which goes first. Warm: one Part read once, then by both variants in turn.

1M Records (load average 21 to 29), 300 calls per case, p50 / p95 in milliseconds and median
response size:

| Branch | Pages | values | `values: false` | bytes, values / headers |
|---|---|---:|---:|---:|
| `main` | cold | 126.7 / 270.7 | 125.5 / 296.7 | 100,720 / 32,424 |
| `main` | warm | 55.4 / 123.6 | 53.7 / 121.5 | 100,192 / 32,398 |
| overlay | cold | 110.2 / 266.1 | 109.2 / 229.3 | 101,374 / 32,644 |
| overlay | warm | 59.8 / 151.9 | 57.0 / 156.2 | 101,877 / 32,938 |

10M Records (load average 38 to 48, peaking above 200 at the end of the overlay pass), 100
calls per case:

| Branch | Pages | values | `values: false` | bytes, values / headers |
|---|---|---:|---:|---:|
| `main` | cold | 238.9 / 413.1 | 243.1 / 430.9 | 103,271 / 33,932 |
| `main` | warm | 52.1 / 130.7 | 56.2 / 137.4 | 104,067 / 33,896 |
| overlay | cold | 252.5 / 459.7 | 246.5 / 475.9 | 100,116 / 32,294 |
| overlay | warm | 49.7 / 101.9 | 49.8 / 138.9 | 100,871 / 32,726 |

- The response is a third of the size (68% smaller), which is what an agent's context pays
  for. The time is the same within noise: the store reads the same Record and Link rows
  either way (the covering index evaluated in
  [2-hop neighborhood on branches](#2-hop-neighborhood-on-branches) was not added), and
  dropping the values saves only their serialization, within the noise of these runs (-3 to +4 ms at p50).
- The covering index stays not adopted: it would cut the hydration's page reads by about two
  thirds for the headers-only variant only, at the cost of a second index on every branch of
  every partition, and the cold neighborhood would still be over budget on this machine.

## Record history and lineage: a narrower index

Evaluated on 2026-10-23 at 10M Records and not adopted. The history range of a Record is read
through the primary key of `mat_history` `(domain_id, branch, partition, element_key,
revision_id)` (6.4 GB at 10M, table 15 GB, 40,012,160 rows) and joined to `revisions` (409
rows, always in memory). `EXPLAIN (ANALYZE, BUFFERS)` of the first history page for 30 random
Records of the 10M fixture (load average about 47): one history row per Record (median), 2
pages read from outside shared buffers for it (median, 5 at most): the leaf of the primary key
and the heap page, 2.0 ms of I/O at the median, 3.7 ms per statement at p50 and 10.7 at p95.

- A narrower or covering index can save at most the heap page of those two: the payload of a
  row (hashes and the provenance JSON, about 400 bytes) would have to move into the index,
  which would then be about as large as the table and maintained on every history row of
  every commit. The upper levels of the primary key are already in memory.
- The rest of the 17 to 28 ms at p95 of the harness is outside this statement: resolving the
  branch head, the Record row, the Revisions' provenance and the object bodies of the change,
  each a page from disk at this size, plus this machine's load. A narrower history index does
  not touch them, so its gain (about 1 ms) is below the noise of the run and does not justify a
  second index on the largest table of a commit; the commit cost was therefore not measured.

## Working set

What the agent read path keeps touching, measured per relation, and how much of it a storage
profile keeps. Measured on 2026-10-23 with `tests/perf/working_set.py` on copies of the seed
databases (`perf_seed_1m`, `perf_seed_10m`, the 1M and 10M fixtures of the sections above),
same machine and Postgres as above (`shared_buffers` 2GB in a Docker VM of 8 GiB).

### Method

```sh
LOREBASE_TEST_DATABASE_URL=postgresql://perf:perf@127.0.0.1:55410/postgres \
uv run python tests/perf/working_set.py --database <copy> --iterations 200 --warmup 20 \
  --buffercache --seed <fresh> [--commits 8] --out build/perf/ws.md --json build/perf/ws.json
```

- **Sizes.** `pg_relation_size` of every table (hash partitions folded into their table), its
  TOAST relation and each index (`pg_total_relation_size`, `pg_indexes_size` agree), divided by
  the Records of `main`; column widths from `pg_stats` (`avg_width`, `null_frac`,
  `n_distinct`) and the average row of a 1% sample.
- **Pages per read.** For each read of the harness (`harness.read_operations`, the reads of
  the 10M table), `--iterations` calls on one connection, one transaction each, random keys:
  the deltas of `pg_statio_user_tables` and `pg_statio_user_indexes` (heap, TOAST and every
  index; the backend's statistics flushed with `pg_stat_force_next_flush()` before each
  snapshot) give the pages each call touched per relation, from shared buffers (`hit`) or not
  (`read`). Unlike `EXPLAIN (ANALYZE, BUFFERS)`, which sees a security definer function as one
  node, this attributes the pages the neighborhood and search functions read inside. The
  relations some read touched are the **hot set**.
- **Cold and warm.** Cold: keys drawn from a seed no earlier run of the database used
  (`--seed`), so their pages are not in shared buffers. Warm: the same keys again right after.
  At 10M the database is eight times the VM's memory, so cold pages come from disk or the
  VM's page cache; at 1M most of the hot set fits in the page cache.
- **Residency.** `pg_buffercache` after the reads: the shared buffers each relation holds.
- **Commits.** `--commits` times commits of 1,000 Part updates after the reads (the bench's
  `commit of 1000 changes`).

The machine was shared with other workloads throughout: load average 45 to 105 on 15 cores
during the 1M runs and 40 to 75 during the 10M runs. Latencies move by a factor of 2 between
runs at the same load; compare sizes and pages, which do not depend on the load, and read the
latencies as upper bounds.

### Where the bytes are (before)

At 10M Records the agent read path touches 18 relations, 57 GB, 6,165 bytes per Record:

| relation | 10M | bytes per Record | read by |
|---|---:|---:|---|
| `mat_records` heap | 17.0 GB | 1,824 | every Record read, search ranking |
| `mat_history` heap | 16.5 GB | 1,766 | history, lineage |
| `mat_links` heap | 8.7 GB | 939 | Links of a Record, neighborhood |
| `mat_history` primary key | 6.8 GB | 726 | history, lineage |
| `mat_links` primary key | 3.5 GB | 378 | neighborhood |
| `mat_records` primary key | 1.4 GB | 145 | get, page, neighborhood, lineage |
| `objects` heap and key | 1.5 GB | 159 | history, lineage |
| `mat_links` reverse index | 1.0 GB | 108 | Links of a Record, neighborhood |
| per-Attribute indexes (14) | 0.9 GB | 96 | attribute queries, foreign key Links |
| GIN on `values` | 0.6 GB | 65 | none of the reads (DSL equality filters) |
| search GIN | 0.2 GB | 19 | search |

- **Lineage maps were half of a Record row.** `pg_stats` of `mat_records`: `lineage` 495
  bytes, `lineage_revisions` 370 and `values` 398 of a 1,600-byte row, 4.5 rows per 8 kB page;
  `lineage` has 467 distinct values in 10M rows and `lineage_revisions` 5,272. `mat_links.lineage`
  is 81 of about 300 bytes. Rows under 2 kB are never compressed or moved to TOAST
  (`TOAST_TUPLE_THRESHOLD`; a lower `toast_tuple_target` does not apply below it, checked on
  20,000 rows), so `SET STORAGE` and lz4 change nothing for these rows.
- **The 1M fixture was half empty.** `pgstattuple_approx` of the 1M copy: 54% free space in
  `mat_records`, 49% in `mat_links`, 51% in `mat_history` (pages emptied by the rewrites of the
  lineage backfill and the loads, kept by the table): 3.4 kB of heap per Record where the rows
  are 1.6 kB. Every page a read touches holds half as many rows as it could.
- **Unused indexes.** `ix_mat_records_values` (0.6 GB at 10M) served none of the reads, and 10
  of the 14 per-Attribute indexes had no scan over the whole benchmark history
  (`pg_stat_user_indexes`).
- **Key widths.** `kind_id` and `relation_id` are 31-byte text ids in every row and in the
  primary keys; integer surrogates would shrink `mat_links`' primary key by about a third, but
  every statement, the row-level security policies and the definer functions name them; not
  changed (see below).

### What changed

| Change | Adopted | Effect (1M, measured) |
|---|---|---|
| Shared lineage maps (migration `0036`, store schema notes) | yes, `compact` and `balanced` | `mat_records` row 1,551 to about 700 bytes; 4,328 distinct maps take 2.5 MB |
| Online compaction (`storage apply --compact --reindex`) | yes, all profiles, on request | `mat_records` heap 3,269 to 799 MB, `mat_links` 1,611 to 672 MB, `mat_history` 3,059 to 1,515 MB, their indexes a third to a half smaller |
| GIN on `values` optional | off in `compact` | 111 MB at 1M, 624 MB at 10M; equality on Attributes without an index scans the Kind |
| Per-Attribute indexes of unique and foreign key Attributes only | `compact` | 90 to 15 MB at 1M; the attribute query reads the Kind (12,700 pages per call instead of 47) |
| Lexicon trigram index optional | off in `compact` | 7 MB; typo corrections compare the lexicon's words (1,527 pages per call instead of 636) |
| `halfvec` HNSW index | `compact` only | index 43% smaller; recall@10 6 points lower (below) |
| TOAST compression lz4 or pglz | per profile | no effect on these rows (under 2 kB); applies to large values |
| `mat_records` fill factor 90 | `fast` | room for HOT updates; 10% more heap |
| Search vectors in a side table | no | 86 bytes of 1,600; ranking reads the vector of every candidate, a join would add a page per candidate |
| Integer keys for Kinds and Relations | no | about a third of `mat_links`' primary key; touches every statement, policy and definer function |
| `toast_tuple_target` / `SET STORAGE` on the row | no | no effect below 2 kB |

Each was adopted only if it shrinks the hot set without slowing commits by more than 5%:
commits of 1,000 Part updates with the lineage map triggers on and off, alternating, 12 each on
the 1M copy: p50 256 and 257 ms, mean 292 and 268 ms (one first-run outlier of 591 ms with the
triggers on).

### 1M Records: profiles

The 1M copy as seeded (before), then after `storage apply --compact --reindex` in each
profile. Hot set and per-relation sizes:

| relation | before | balanced | compact | fast |
|---|---:|---:|---:|---:|
| `mat_records` heap | 3,269 MB | 799 MB | 802 MB | 1,827 MB |
| `mat_records` primary key | 262 MB | 76 MB | 76 MB | 76 MB |
| GIN on `values` | 136 MB | 111 MB | none | 113 MB |
| per-Attribute indexes | 179 MB | 90 MB | 15 MB | 90 MB |
| `mat_links` heap | 1,611 MB | 672 MB | 672 MB | 802 MB |
| `mat_links` primary key | 797 MB | 252 MB | 252 MB | 252 MB |
| `mat_history` heap | 3,059 MB | 1,515 MB | 1,515 MB | 1,517 MB |
| `mat_history` primary key | 1,525 MB | 476 MB | 476 MB | 477 MB |
| `mat_lineage_maps` | none | 2 MB | 4 MB | 6 MB |
| **hot set** | **12.5 GB (13,120 B/Record)** | **5.5 GB (5,791)** | **5.5 GB (5,714)** | **6.7 GB (7,006)** |
| all relations | 19.0 GB | 11.9 GB | 11.8 GB | 13.1 GB |

`fast` keeps the maps in the rows (1,915 bytes of heap per Record); its compaction stopped when
no free space was left below the last pages, so its heap is 15% above a fresh load of inline
rows. Reads, p50 / p95 in milliseconds, cold then warm (load average in brackets):

| read | before (72) cold | warm | balanced (82) cold | warm | compact (95) cold | warm | fast (50) cold | warm |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| get by key | 9.1 / 33.8 | 5.4 / 11.1 | 3.9 / 22.2 | 3.1 / 6.4 | 3.5 / 13.6 | 8.1 / 23.6 | 6.0 / 26.8 | 3.4 / 16.6 |
| list page of 50 | 16.6 / 59.0 | 12.2 / 87.7 | 6.1 / 24.1 | 8.1 / 31.5 | 5.7 / 16.0 | 7.0 / 40.2 | 6.2 / 14.2 | 4.3 / 7.4 |
| links of a record | 22.1 / 74.0 | 14.9 / 84.3 | 6.0 / 14.1 | 3.6 / 14.6 | 4.7 / 7.3 | 2.9 / 5.1 | 6.4 / 10.1 | 4.7 / 8.4 |
| 2-hop neighborhood | 291.5 / 718.5 | 73.0 / 285.5 | 42.5 / 93.0 | 20.3 / 45.1 | 27.1 / 50.8 | 13.6 / 28.0 | 47.1 / 74.4 | 14.0 / 32.7 |
| query by indexed attribute | 13.6 / 84.2 | 7.8 / 25.5 | 4.7 / 26.5 | 6.3 / 22.1 | 10.7 / 80.9 | 10.7 / 77.3 | 4.2 / 6.4 | 4.5 / 10.6 |
| full-text search | 22.6 / 83.5 | 20.8 / 46.5 | 14.6 / 45.8 | 15.3 / 41.5 | 15.9 / 47.5 | 15.0 / 30.0 | 13.3 / 20.4 | 14.2 / 25.1 |
| typo-tolerant search | 20.2 / 69.0 | 26.0 / 122.8 | 14.7 / 35.4 | 14.0 / 32.2 | 20.7 / 41.7 | 19.8 / 46.5 | 13.4 / 24.4 | 13.3 / 22.0 |
| partial-match search | 61.3 / 234.7 | 50.1 / 134.1 | 32.0 / 82.0 | 31.4 / 70.2 | 36.1 / 68.0 | 36.4 / 67.0 | 29.3 / 45.7 | 29.3 / 40.2 |
| record history page | 30.5 / 89.1 | 20.4 / 96.6 | 10.6 / 28.2 | 8.9 / 25.0 | 11.1 / 25.3 | 8.3 / 15.0 | 10.1 / 15.0 | 8.0 / 16.8 |
| record lineage | 15.8 / 50.7 | 15.9 / 66.1 | 8.6 / 23.6 | 10.0 / 22.0 | 8.0 / 17.0 | 9.1 / 22.4 | 8.1 / 11.1 | 7.5 / 10.3 |

Pages read from outside shared buffers per cold neighborhood: 211 before, 138 balanced, 70
compact, 128 fast. Commits of 1,000 Part updates after the reads: p50 901 ms (balanced), 2,108
(compact, at load 95 to 105), 686 (fast, load 45 to 69); the alternating on/off measurement
above is the comparison to trust.

- Compaction is most of the gain at 1M (the fixture was half empty); shared maps add the rest
  and are what keeps a freshly loaded table small (rows of 700 instead of 1,600 bytes).
- Resolving a shared map costs a primary-key lookup in `mat_lineage_maps` (a few MB, always in
  shared buffers) per Record row returned: a page of 50 touches 173 pages instead of 26, a
  neighborhood 1,342 instead of 950, all hits; the latency stays below the inline rows' because
  the Record pages that are read from disk are half as many.
- `compact`: the attribute query and typo corrections scan instead of using an index, 5 to 10
  ms more at this size; everything else reads as in `balanced`.
- `storage apply` on the 1M copy: switching to shared maps rewrote 1,000,110 Records and
  2,248,896 Links in 26 minutes (other Links have maps of at most 64 bytes); `--compact
  --reindex` took 7 minutes; switching to `fast` (inline rewrite and compaction) 14 minutes.
  Reads went on throughout.

### 10M Records

Before, on a copy of `perf_seed_10m` (migrated to `0035`, fresh keys, load average 40 to 75),
p50 / p95 in milliseconds, with pages per call and pages read from outside shared buffers:

| read | cold | warm | pages | read |
|---|---:|---:|---:|---:|
| get by key | 4.9 / 14.0 | 2.9 / 5.6 | 6 | 1.4 |
| list page of 50 | 9.0 / 17.5 | 4.2 / 21.4 | 31 | 15.2 |
| links of a record | 10.0 / 16.9 | 3.9 / 6.0 | 27 | 13.8 |
| 2-hop neighborhood | 122.4 / 190.3 | 14.4 / 26.1 | 954 | 345.7 |
| query by indexed attribute | 3.8 / 6.5 | 4.0 / 8.6 | 45 | 0.1 |
| full-text search | 56.6 / 119.6 | 56.2 / 92.3 | 4,526 | 0.0 |
| typo-tolerant search | 56.8 / 103.6 | 56.8 / 116.6 | 3,015 | 0.5 |
| partial-match search | 148.7 / 231.0 | 145.3 / 248.8 | 9,405 | 0.2 |
| record history page | 10.0 / 15.9 | 8.5 / 14.7 | 22 | 4.9 |
| record lineage | 7.4 / 10.1 | 7.6 / 11.2 | 47 | 0.0 |

The cold neighborhood reads 346 of its 954 pages from outside shared buffers: the read set
(57 GB) is seven times the VM. The 10M fixture was written once by `replicate` and is not
bloated: its Record heap is 1,824 bytes per Record (rows of 1,600 bytes, 4.5 per page).

After, measured on 2026-10-23 on the same copy in its `balanced` end state (each table
rewritten through the lineage map triggers, then `VACUUM FULL`: the end state of `apply
--compact --reindex`), then after `storage apply` to `compact`, fresh keys each, Docker VM of
16 GiB with `shared_buffers` 6GB, `effective_cache_size` 12GB, `work_mem` 8MB,
`maintenance_work_mem` 1GB (`make perf-up PERF_VM=16g`), load average 9 to 15. The read set
(44 GB) is still about three times the VM.

| | before | balanced | compact |
|---|---:|---:|---:|
| `mat_records` heap | 17.0 GB | 7.8 GB | 7.8 GB |
| `mat_links` heap | 8.7 GB | 6.6 GB | 6.6 GB |
| **hot set** | **57 GB (6,165 B/Record)** | **44.1 GB (4,729)** | **43.3 GB (4,651)** |

Reads, p50 / p95 in milliseconds, cold then warm, with pages read from outside shared buffers
per cold call:

| read | balanced cold | warm | read | compact cold | warm | read |
|---|---:|---:|---:|---:|---:|---:|
| get by key | 4.9 / 10.7 | 3.2 / 7.4 | 2.7 | 3.5 / 10.1 | 3.8 / 7.7 | 2.6 |
| list page of 50 | 6.3 / 13.8 | 3.6 / 6.0 | 17.7 | 6.8 / 21.1 | 3.4 / 7.3 | 16.8 |
| links of a record | 9.1 / 19.2 | 3.1 / 5.3 | 15.5 | 8.0 / 17.7 | 2.3 / 4.2 | 8.1 |
| 2-hop neighborhood | 99.8 / 145.4 | 9.8 / 20.9 | 383.0 | 95.7 / 159.0 | 9.4 / 16.6 | 272.8 |
| query by indexed attribute | 3.5 / 7.5 | 3.3 / 5.2 | 5.0 | 5.9 / 708.2 | 7.4 / 828.8 | 0.0 |
| full-text search | 35.4 / 58.3 | 36.9 / 56.7 | 5.4 | 41.0 / 75.1 | 40.7 / 76.7 | 8.2 |
| typo-tolerant search | 39.0 / 69.9 | 40.1 / 82.2 | 3.6 | 40.3 / 88.1 | 40.2 / 86.0 | 0.3 |
| partial-match search | 91.9 / 117.7 | 89.6 / 122.5 | 0.6 | 99.0 / 148.8 | 107.3 / 167.4 | 0.7 |
| record history page | 7.9 / 10.3 | 6.9 / 11.4 | 5.5 | 7.3 / 11.2 | 4.2 / 6.8 | 5.6 |
| record lineage | 6.2 / 9.7 | 5.8 / 8.2 | 0.1 | 4.9 / 7.5 | 4.9 / 8.5 | 0.1 |

- The hot set is 23% smaller than before (4,729 against 6,165 bytes per Record), close to the
  4.9 kB the 1M widths predicted; `compact` saves another 0.8 GB (the values index and the
  per-Attribute indexes it leaves out).
- The cold neighborhood went from 122 to 100 ms at p50 (`balanced`; the before-run had 2 GB of
  shared buffers and a load of 40 to 75, so part of the gain is the machine).
- `compact` at 10M: the attribute query scans the Kind (190,000 pages per call, p95 0.7 s,
  all from shared buffers once read); every other read is as in `balanced`.
- Producing the end state: the Records rewrite took 30.5 minutes and its `VACUUM FULL` 9.6;
  the Links rewrite 27 minutes in ten parallel page ranges (WAL-bound at about 24 MB/s, with
  the maps stored first in one statement so the ranges did not wait for each other's map
  inserts) and its `VACUUM FULL` 9.4 minutes. `storage apply` to `compact` afterwards took
  13.5 minutes, almost all of it the rewrite pass checking every page and finding no row left
  in the other form.

Commits of 1,000 Part updates, six after each read run at load 9 to 13: p50 2,711 ms in
`balanced`, 2,332 ms in `compact`. Commits are slower at 10M than at 1M in both profiles,
and `compact` is not slower than `balanced` under the same load: the 2,108 ms of the 1M
`compact` run was the load (95 to 105) of that run. The six runs are sequential, not
alternating, as switching profiles at 10M rebuilds the values index.

### Embeddings: half precision

`tests/perf/halfvec_recall.py`: 30,000 Records of `perf_seed_1m_zipf` (title and string values)
and 300 queries (title words, two- and three-word phrases, misspelled words, titles less a
word) embedded with BAAI/bge-small-en-v1.5 (384 dimensions, the default model); recall@10
against exact cosine search in float32, pgvector 0.8.7, HNSW m=16, ef_construction=64.

| index | size | bytes per vector | recall@10, ef_search 40 | ef_search 100 |
|---|---:|---:|---:|---:|
| exact search, `vector` | none | | 1.000 | |
| exact search, `halfvec` | none | | 0.998 | |
| HNSW `vector` (default) | 61.4 MB | 2,048 | 0.532 | 0.597 |
| HNSW `halfvec` | 35.1 MB | 1,171 | 0.472 | 0.541 |

Half precision alone costs 0.2 points of recall; the HNSW graph built on it 6 points (HNSW
recall is low for both on this corpus of templated texts and short, out-of-corpus queries).
That is not negligible, so `halfvec` is the `compact` choice only; `balanced` and `fast` keep
full precision. Stored vectors keep full precision in every profile, so switching back costs
an index build and no data.

## Storage profiles

`store.storage.profile` (`compact`, `balanced`, `fast`, default `balanced`; compose
`LOREBASE_STORAGE_PROFILE`, Helm `config.store.storage.profile`) decides what the database
keeps besides the rows. Every feature works in every profile; each other key of
`store.storage` overrides one choice ([configuration](reference/configuration.md)).

| choice | compact | balanced | fast |
|---|---|---|---|
| lineage maps | shared | shared | inline |
| GIN on Record values | off | on | on |
| per-Attribute indexes | unique and foreign key | all | all |
| trigram index of the typo lexicon | off | on | on |
| HNSW index of embeddings | `halfvec` | `vector` | `vector` |
| TOAST compression | pglz | lz4 | lz4 |
| `mat_records` fill factor | 100 | 100 | 90 |
| prewarm after `storage apply` | off | off | on |
| BM25 index of search (`bm25_index`, needs `pg_search`) | off | off | off |

What leaving something out costs: an equality filter on an Attribute without an index scans its
Kind (`compact`), typo corrections compare the lexicon's words of the searched Kinds
(`compact`), semantic search ranks with a slightly worse graph (`compact`), inline maps make
Record rows twice as large (`fast`, which saves the lookup of the map, a buffer hit).

### Switching

```sh
python -m lorebase_store.storage status                    # choices, what the database has, hot sizes
python -m lorebase_store.storage apply [--dry-run]          # bring the database in line
python -m lorebase_store.storage apply --compact --reindex  # and give free space back
python -m lorebase_store.storage apply --verify             # scan every lineage row again
python -m lorebase_store.storage prewarm [--fraction 0.8]   # load the hot relations
```

(`docker compose --profile maintenance run --rm storage apply` in the compose stack.) The
database keeps what it has until `apply` runs: reads accept both forms of a lineage map, use
whichever HNSW index exists and work without the optional indexes. `apply` builds indexes with
`CREATE INDEX CONCURRENTLY`, rewrites rows in batches of `store.storage.batch_rows` (progress in
`storage_tasks`; an interrupted run resumes), and waits at most
`store.storage.lock_timeout_ms` for a table lock (trigger switches, dropping the partitioned
values index), deferring the step to the next run otherwise; reads go on throughout. Run it as
the maintenance role (the owner of the tables). It exits with status 3 while a step is
deferred.

A lineage rewrite that ends with no row left in the other form is recorded per partition in
`storage_tasks` (`lineage:<partition>`: `complete` and the `watermark`, the oldest running
transaction when the pass started). A later `apply` with the same target skips the full scan
(13.5 minutes at 10M Records, even with nothing to rewrite): once the triggers are in the state
the profile wants, every writer stores the wanted form (shared: the triggers turn maps into
references; inline: nothing writes a reference), so it only looks at the rows written since the
watermark, by transaction id, without reading their maps, and moves the watermark on. Rows it
finds start a pass limited to them. Rows written while the triggers were off out of band, or
frozen by `VACUUM` before a run looked at them, are not seen: `--verify` scans every row and
resets what is recorded.

Upgrading to revision `0036`: the migration leaves an existing install's lineage maps inline
(processes of the previous version may still be running); once every process runs the new
version, `storage apply` shares them, and `--compact --reindex` gives the space back.

### Prewarm

`storage prewarm` loads the hot relations into shared buffers with `pg_prewarm`, most valuable
first: refs, Revisions, schemas, provenance, lineage maps and the lexicon, then the primary
keys of Records and Links, the reverse Link index, the search index, per-Attribute indexes, the
HNSW index, the Record and Link heaps, then history, up to `store.storage.prewarm_fraction` of
`shared_buffers`. On the 1M copy it loaded 1,638 MB (80% of 2 GB) in the `fast` apply.
`autoprewarm` keeps it across restarts and failovers: `shared_preload_libraries =
'pg_prewarm'` (compose `POSTGRES_PRELOAD_LIBRARIES=pg_prewarm`, Helm
`ci/storage-fast-values.yaml`) dumps the list of cached blocks every
`pg_prewarm.autoprewarm_interval` and reloads them at start. The extension ships with Postgres
(contrib); `prewarm` creates it, which needs a role allowed to.

### Sizing

Memory that keeps the whole agent read set resident (the hot set above: Record, Link and
history heaps, their indexes, objects read by history), per profile, from the measured bytes
per Record after `apply --compact --reindex`, plus a quarter for the operating system,
connections, sorts and growth:

Bytes per Record of the agent read set: the core reads (Records, Links and their indexes:
get, pages, Links, neighborhoods, attribute queries, search) and everything including history
and lineage. Measured at 1M and 10M; 100M is ten times 10M (an estimate).

| | compact | balanced | fast |
|---|---:|---:|---:|
| core read set, bytes per Record | 2,210 | 2,290 | 3,690 |
| whole read set, bytes per Record (1M measured) | 5,714 | 5,791 | 7,006 |
| whole read set, bytes per Record (10M measured; `fast` estimated) | 4,651 | 4,729 | 6,350 |
| 1M Records: core / whole read set | 2.2 / 5.5 GB | 2.3 / 5.5 GB | 3.7 / 6.7 GB |
| 1M Records: memory to keep the whole set resident | 7 GB | 7 GB | 8.5 GB |
| 10M Records: core / whole read set | 22 / 43 GB | 23 / 44 GB | 37 / 64 GB |
| 10M Records: memory to keep the whole set resident | 54 GB | 55 GB | 80 GB |
| 100M Records: core / whole read set | 220 / 433 GB | 229 / 440 GB | 369 / 635 GB |
| 100M Records: memory to keep the whole set resident | 540 GB | 550 GB | 795 GB |

With less memory than the whole read set, history and lineage reads go to disk first; with
less than the core read set, the neighborhood does. `fast` trades about 60% more memory for
the core reads for no map lookups and HOT room in the Record pages: choose it when the memory
is there anyway, `balanced` otherwise, `compact` on a small box (an attribute query or a typo
correction scans instead of using an index).

- `shared_buffers`: as much of the hot set as it can hold without starving the page cache;
  `effective_cache_size` tells the planner what both hold together.
- Huge pages: with `shared_buffers` of 8 GB and more, `huge_pages = on` (with pages reserved on
  the host or the node, `vm.nr_hugepages` or `hugepages-2Mi` in Kubernetes) saves the page
  tables of every backend; `try` elsewhere.
- Below the read set, reads that miss memory read about one page from disk per Record and per
  Link they return (0.26 ms each on this VM, [10M Records](#10m-records)): the cold
  neighborhood is the read that feels it first.

Recommended Postgres settings (compose `.env.example`, Helm `ci/storage-<profile>-values.yaml`):

| | compact | balanced | fast |
|---|---|---|---|
| memory for Postgres | what there is (4 GiB example) | 16 GiB example | the read set + 25% (64 GiB example) |
| `shared_buffers` | 1/8 of it (512MB) | 1/4 (4GB) | 2/5 (24GB) |
| `effective_cache_size` | 1/2 (2GB) | 3/4 (12GB) | 3/4 (48GB) |
| `huge_pages` | off | try | on |
| `shared_preload_libraries` | | | `pg_prewarm` (autoprewarm) |

## Web app: Canvas (5,000 nodes, 10,000 edges)

Measured by `pnpm perf:canvas` in `apps/web` on the generated graph of the mock API (`VITE_MOCK_PERF_NODES=5000`, `VITE_MOCK_PERF_EDGES=10000`, `VITE_CANVAS_MAX_NODES=5000`): a production build served by `vite preview`, opened in headless Chromium with a software WebGL (SwiftShader), 1440x900, median of 3 runs, 2026-10-22, same machine as above. Time to first render is navigation start to the second animation frame after the canvas exists. The pan is 80 mouse moves with the button down; frame time is the gap between animation frames while it runs. Software WebGL makes every frame slower than on a GPU, so read the numbers as a comparison between the two rows, not as what a laptop shows.

| | before (layout on the main thread) | after (worker layout) |
|---|---|---|
| time to first render | 1,675 ms | 612 ms |
| main thread blocked before it (long tasks past 50 ms) | 1,497 ms (longest task 1,361 ms) | 463 ms (longest task 513 ms) |
| pan right after first render, mean frame | 218 ms | 151 ms |
| pan right after first render, p95 frame | 617 ms | 500 ms |
| pan on the settled graph, mean frame | not applicable (layout done before) | 122 ms |
| layout | 100 iterations, then fixed | runs in a worker until it settles or 20 s |

Before, the 100 ForceAtlas2 iterations ran on the main thread before the first draw and the layout never improved after that. Now the graph is drawn from its seed positions at once, the worker refines it while it is on screen, and Barnes-Hut, edge hiding while moving, no edge labels and fewer node labels apply at this size. The frame times during a pan are dominated by the software rasterizer in both rows; the win measured here is the time to first render and a free main thread during the layout.
