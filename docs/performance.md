# Performance baseline

Latency of the agent read paths on a materialized head (design 8, "Agent read budgets"), measured by `tests/perf/bench.py`. Every read is one transaction that scopes row-level security to the Domain and runs one store primitive (`lorebase_store.reads`, `lorebase_store.traverse`); times are client side over a local socket, so they include the driver and the RLS scoping round trip.

## Dataset

- `supply` Domain from `examples/supply/scripts/make_supply_data.py --scale 540`, plus 4 extra `supplied_by` and `stocked_at` Links per Part.
- 99,900 Records and 302,940 Links on `main`, loaded through the commit pipeline in 45 commits (161 s, then `VACUUM ANALYZE`).
- 300 timed calls per read after 30 warm-up calls, random keys from the dataset.

## Environment

- machine: Darwin 25.6.0 arm64
- python: 3.12.13
- cpu: Apple M5 Pro
- cores: 15
- memory: 24 GiB
- postgres: 16.15 (Debian 16.15-1.pgdg12+2)
- shared_buffers: 128MB
- work_mem: 4MB
- run: 2026-10-21, Postgres in Docker on the same machine, default container settings.

## Reads (milliseconds)

`owner`: a role not subject to row-level security (superuser in the container). `rls`: a role subject to the `domain_isolation` policies, like the application role in production.

| Operation | Target p95 | owner p50 | owner p95 | rls p50 | rls p95 |
|-----------|-----------:|----------:|----------:|--------:|--------:|
| get by key | 5.00 | 1.01 | 1.07 | 1.17 | 1.25 |
| list page of 50 | 20.00 | 1.40 | 1.57 | 1.55 | 1.65 |
| links of a record | 10.00 | 1.22 | 1.30 | 1.35 | 1.58 |
| 2-hop neighborhood | 30.00 | 4.38 | 5.67 | 4.51 | 5.11 |
| query by indexed attribute | 25.00 | 1.54 | 1.71 | 1.78 | 1.86 |
| full-text search | 50.00 | 2.16 | 2.58 | 2.32 | 2.75 |
| full-text search with typo-tolerant titles | 50.00 | 10.20 | 22.32 | 10.65 | 22.64 |

## Writes (milliseconds)

| Operation | Runs | p50 | p95 |
|-----------|-----:|----:|----:|
| branch creation (copy) | 3 | 7280.91 | 7315.67 |
| branch creation (overlay) | 20 | 11.71 | 13.83 |
| commit of 1000 changes | 5 | 364.60 | 547.02 |

## Overlay branches (milliseconds)

`agent/perf` (overlay) and `perf-copy` (copy) forked from `main` and got the same commit of 1000 Part updates; `main` then committed 1000 other Part updates. The overlay stores 2,000 rows (its changes, its derived and copied-on-write rows), the copy 402,840. Reads alternate between the two branches call by call, with the same random keys, 300 timed calls each.

| Operation | Role | copy p50 | overlay p50 | copy p95 | overlay p95 | overhead p50 | overhead p95 |
|-----------|------|---------:|------------:|---------:|------------:|-------------:|-------------:|
| get by key | owner | 1.02 | 1.04 | 1.13 | 1.16 | +2% | +3% |
| get by key | rls | 1.17 | 1.19 | 1.27 | 1.29 | +2% | +1% |
| list page of 50 | owner | 1.48 | 1.47 | 1.62 | 1.63 | -1% | +1% |
| list page of 50 | rls | 1.65 | 1.60 | 1.80 | 1.72 | -3% | -5% |
| links of a record | owner | 1.29 | 1.58 | 1.45 | 1.79 | +23% | +23% |
| links of a record | rls | 1.44 | 1.76 | 1.57 | 1.91 | +22% | +22% |
| 2-hop neighborhood | owner | 5.06 | 6.49 | 6.57 | 7.73 | +28% | +18% |
| 2-hop neighborhood | rls | 4.98 | 6.28 | 6.13 | 7.60 | +26% | +24% |
| query by indexed attribute | owner | 1.92 | 2.27 | 2.16 | 2.51 | +18% | +16% |
| query by indexed attribute | rls | 1.90 | 2.29 | 2.29 | 2.64 | +21% | +15% |
| full-text search | owner | 2.38 | 2.92 | 2.77 | 3.29 | +23% | +19% |
| full-text search | rls | 2.64 | 3.18 | 3.07 | 3.64 | +20% | +19% |
| full-text search with typo-tolerant titles | owner | 11.16 | 11.80 | 25.27 | 27.04 | +6% | +7% |
| full-text search with typo-tolerant titles | rls | 11.53 | 12.08 | 25.97 | 27.75 | +5% | +7% |

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
| full-text search with typo-tolerant titles | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_domain_id_branch_search_idx<br>Bitmap Index Scan mat_records_p3_domain_id_branch_title_idx | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_domain_id_branch_search_idx<br>Bitmap Index Scan mat_records_p3_domain_id_branch_title_idx |

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
- Search reads the GIN indexes on `(domain_id, branch, search)` and `(domain_id, branch,
  title gin_trgm_ops)` (revision `0011`, `btree_gin`): the Domain and branch are index
  conditions, so a search reads its branch's matches only, however many branches share the
  partition (here `main`, a copy and an overlay). It ranks at most
  `store.reads.search_candidates` matches, so very common terms cost a bounded amount.
  Typo-tolerant titles (trigram similarity, Keeper's lexical search) add the branch's
  trigram matches on titles to the candidates.
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
