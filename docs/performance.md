# Performance baseline

Latency of the agent read paths on a materialized head (design 8, "Agent read budgets"), measured by `tests/perf/bench.py`. Every read is one transaction that scopes row-level security to the Domain and runs one store primitive (`lorebase_store.reads`, `lorebase_store.traverse`); times are client side over a local socket, so they include the driver and the RLS scoping round trip.

The sections from Dataset to Reading the results are the 100k-Record run of 2026-10-22, after the word lexicon of migration `0018` (typo-tolerant search); [Word lexicon: before and after](#word-lexicon-before-and-after-100k-records) compares it with the code before. The run on the 1M-Record fixture (M1) is in [1M Records](#1m-records): every read is within budget there except the 2-hop neighborhood on branches other than `main` and Record lineage against the budget proposed for it; typo-tolerant search, over budget in that run, is within it with the lexicon ([1M Records: word lexicon](#1m-records-word-lexicon)).

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
| record lineage | 10.00\* | 3.54 | 3.86 | 4.40 | 3.78 | 4.56 | 6.41 |

\* No budget in design 8.7; held to the budget of Links of a Record, the other per-Record index range read.

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
| record lineage | Index Scan ix_proposals_domain_id_target_ref_status<br>Index Scan mat_records_p3_pkey<br>Index Scan objects_p3_pkey<br>Index Scan pk_domains<br>Index Scan pk_mat_history<br>Index Scan pk_provenance<br>Index Scan pk_revision_parents<br>Index Scan pk_revisions<br>Seq Scan refs | Index Scan ix_proposals_domain_id_target_ref_status<br>Index Scan mat_records_p3_pkey<br>Index Scan objects_p3_pkey<br>Index Scan pk_domains<br>Index Scan pk_mat_history<br>Index Scan pk_provenance<br>Index Scan pk_revision_parents<br>Index Scan pk_revisions<br>Seq Scan refs |

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
- Records batches are the slowest (up to 0.5 s: the GIN indexes on search vectors, values and
  titles). A batch holds the branch lock, so a commit on the copy waits for at most that long.
- Deleting is a 8 ms request; the job removes the rows in 8.6 to 12.3 s, batches under 0.2 s.
- At 1M Records (not measured in this run: the `--seed-database` fixture of [1M Records](#1m-records)
  was not available, and loading it takes about 33 minutes), the batches stay the same size and
  the copy and deletion jobs scale with the rows, about ten times longer in total; no
  transaction depends on the size of the Domain any more.

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
  the size of the Domain. Lineage makes about eight statements (Domain, ref and head Revision,
  then the Record, its history range, provenance and Proposals) and sits at its proposed
  budget: 9.3 ms at p95 on `main` with 2GB of buffers, 9.8 to 11.0 on branches and with 128MB.
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
- Lineage has no budget in design 8.7; with the one proposed here it is at the limit on
  branches. Its statements are index lookups; fewer round trips (the head Revision and the
  Record in one statement) would give the margin.

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
