# Performance baseline

Latency of the agent read paths on a materialized head (design 8, "Agent read budgets"), measured by `tests/perf/bench.py`. Every read is one transaction that scopes row-level security to the Domain and runs one store primitive (`lorebase_store.reads`, `lorebase_store.traverse`); times are client side over a local socket, so they include the driver and the RLS scoping round trip.

## Dataset

- `supply` Domain from `examples/supply/scripts/make_supply_data.py --scale 540`, plus 4 extra `supplied_by` and `stocked_at` Links per Part.
- 99,900 Records and 302,940 Links on `main`, loaded through the commit pipeline in 45 commits (145 s, then `VACUUM ANALYZE`).
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
| get by key | 5.00 | 1.01 | 1.09 | 1.17 | 1.26 |
| list page of 50 | 20.00 | 1.39 | 1.48 | 1.52 | 1.87 |
| links of a record | 10.00 | 1.22 | 1.37 | 1.36 | 1.65 |
| 2-hop neighborhood | 30.00 | 4.39 | 5.58 | 4.52 | 5.05 |
| query by indexed attribute | 25.00 | 1.61 | 1.74 | 1.58 | 1.84 |
| full-text search | 50.00 | 1.96 | 2.33 | 1.88 | 2.32 |
| full-text search with typo-tolerant titles | 50.00 | 9.13 | 20.92 | 9.57 | 21.59 |

## Writes (milliseconds)

| Operation | Runs | p50 | p95 |
|-----------|-----:|----:|----:|
| branch creation (copy) | 3 | 7175.49 | 7314.06 |
| branch creation (overlay) | 20 | 5.53 | 6.71 |
| commit of 1000 changes | 5 | 383.66 | 485.97 |

## Overlay branches (milliseconds)

`agent/perf` (overlay) and `perf-copy` (copy) forked from `main` and got the same commit of 1000 Part updates; `main` then committed 1000 other Part updates. The overlay stores 2,000 rows (its changes, its derived and copied-on-write rows), the copy 402,840. Reads alternate between the two branches per operation, 300 timed calls each.

| Operation | Role | copy p50 | overlay p50 | copy p95 | overlay p95 | overhead p50 | overhead p95 |
|-----------|------|---------:|------------:|---------:|------------:|-------------:|-------------:|
| get by key | owner | 1.01 | 1.03 | 1.10 | 1.24 | +2% | +13% |
| get by key | rls | 1.17 | 1.20 | 1.32 | 1.27 | +2% | -3% |
| list page of 50 | owner | 1.42 | 1.45 | 1.55 | 1.57 | +2% | +1% |
| list page of 50 | rls | 1.61 | 1.59 | 1.75 | 1.69 | -2% | -4% |
| links of a record | owner | 1.24 | 1.56 | 1.46 | 1.74 | +26% | +19% |
| links of a record | rls | 1.40 | 1.61 | 1.57 | 1.75 | +15% | +12% |
| 2-hop neighborhood | owner | 4.46 | 6.71 | 6.13 | 8.21 | +50% | +34% |
| 2-hop neighborhood | rls | 4.60 | 6.91 | 5.25 | 8.36 | +50% | +59% |
| query by indexed attribute | owner | 1.64 | 1.94 | 1.78 | 2.37 | +19% | +34% |
| query by indexed attribute | rls | 1.82 | 2.15 | 1.99 | 2.58 | +18% | +30% |
| full-text search | owner | 2.35 | 4.72 | 2.99 | 5.18 | +101% | +73% |
| full-text search | rls | 2.71 | 5.22 | 3.67 | 8.51 | +93% | +132% |
| full-text search with typo-tolerant titles | owner | 15.88 | 18.03 | 42.70 | 49.08 | +14% | +15% |
| full-text search with typo-tolerant titles | rls | 17.67 | 19.09 | 45.80 | 52.80 | +8% | +15% |

## Plans

Scan nodes of the statements of one call of each read, statements run inside the read functions included (`auto_explain`).

| Operation | owner | rls |
|-----------|-------|-----|
| get by key | Index Scan mat_records_p3_pkey | Index Scan mat_records_p3_pkey |
| list page of 50 | Index Scan mat_records_p3_pkey | Index Scan mat_records_p3_pkey |
| links of a record | Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b | Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b |
| 2-hop neighborhood | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b<br>Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_links_p3_pkey<br>Index Scan mat_records_p3_pkey | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b<br>Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_links_p3_pkey<br>Index Scan mat_records_p3_pkey |
| query by indexed attribute | Index Scan mat_records_attr_71d8b499dff05dab64f394fe | Index Scan mat_records_attr_71d8b499dff05dab64f394fe |
| full-text search | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_search_idx | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_search_idx |
| full-text search with typo-tolerant titles | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_search_idx<br>Bitmap Index Scan mat_records_p3_title_idx | Bitmap Heap Scan mat_records_p3<br>Bitmap Index Scan mat_records_p3_search_idx<br>Bitmap Index Scan mat_records_p3_title_idx |

## Reading the results

- Both roles run the defaults: search, incoming Links, the neighborhood and attribute queries
  go through the security definer functions of revision `0005` (`lb_search`, `lb_links_in`,
  `lb_neighborhood`, `lb_query_by_attribute`; store schema notes). The `rls` role gets the
  owner's index plans inside them, and every read of both roles is within its budget.
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
- Search ranks at most `store.reads.search_candidates` matches, so very common terms cost a
  bounded amount. Typo-tolerant titles (trigram similarity, Keeper's lexical search) add the
  matches of the trigram index on titles to the candidates.
- Times come from a laptop shared with other work; repeated runs differ by a few milliseconds
  at p95, up to ten for typo-tolerant search.
- Branch creation copies the Records, Links and history rows of `main` (about twice the Records
  and Links) and builds the new branch's per-Attribute indexes in the same transaction.
- An overlay branch (AC-16, store schema notes) is created by inserting its ref and copying
  the compiled schema row: its cost does not depend on the size of the Domain.
- Overlay reads resolve the overlay's own rows over the base rows as of the fork point in one
  statement: an index scan of each branch, a primary-key probe of the overlay per base row
  read and a one-row check that the ref is still that overlay. Keyset pages and attribute
  queries are cut per half, lookups by key are lateral (store schema notes), so the plans are
  those of the copy run twice on few rows; `test_overlay_reads_keep_index_plans` checks them.
- Against the 20% overhead target: key reads, pages, Links of a Record (p95) and typo-tolerant
  search are within it; attribute queries (+0.3 to 0.6 ms), the neighborhood (+2 to 3 ms) and
  plain full-text search (+2.5 to 5 ms) are above it, and every read stays far within its
  budget of design 8.7. Search pays twice for the GIN index on `search`, which is shared by
  every branch of the partition: each half reads all matches of the partition and keeps its
  branch's (the composite `(domain_id, branch, search)` index of design 8.7 "Deferred" would
  remove that). The neighborhood plans one lateral lookup per hop and half.
- The overlay and the copy below got the same commits; `main` then moved, and its commit
  copied the previous state of the rows it changed into the overlay (copy on write).

Regenerate with:

```sh
uv run python tests/perf/bench.py --scale 540 --extra-links 4 --out docs/performance.md
```
