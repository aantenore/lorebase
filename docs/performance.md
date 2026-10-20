# Performance baseline

Latency of the agent read paths on a materialized head (design 8, "Agent read budgets"), measured by `tests/perf/bench.py`. Every read is one transaction that scopes row-level security to the Domain and runs one store primitive (`lorebase_store.reads`, `lorebase_store.traverse`); times are client side over a local socket, so they include the driver and the RLS scoping round trip.

## Dataset

- `supply` Domain from `examples/supply/scripts/make_supply_data.py --scale 540`, plus 4 extra `supplied_by` and `stocked_at` Links per Part.
- 99,900 Records and 302,940 Links on `main`, loaded through the commit pipeline in 45 commits (269 s, then `VACUUM ANALYZE`).
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
- run: 2026-10-20, Postgres in Docker on the same machine, default container settings.

## Reads (milliseconds)

`owner`: a role not subject to row-level security (superuser in the container). `rls`: a role subject to the `domain_isolation` policies, like the application role in production.

| Operation | Target p95 | owner p50 | owner p95 | rls p50 | rls p95 |
|-----------|-----------:|----------:|----------:|--------:|--------:|
| get by key | 5.00 | 1.49 | 2.43 | 2.96 | 4.78 |
| list page of 50 | 20.00 | 3.07 | 5.64 | 4.69 | 9.33 |
| links of a record | 10.00 | 2.61 | 4.25 | 20.06 | 28.06 |
| 2-hop neighborhood | 30.00 | 9.53 | 20.46 | 100.44 | 136.73 |
| query by indexed attribute | 25.00 | 3.14 | 4.61 | 7.70 | 16.15 |
| full-text search | 50.00 | 4.43 | 6.39 | 58.25 | 68.14 |

## Writes (milliseconds)

| Operation | Runs | p50 | p95 |
|-----------|-----:|----:|----:|
| branch creation (copy) | 3 | 10007.44 | 12210.42 |
| commit of 1000 changes | 5 | 433.34 | 557.86 |

## Plans

Scan nodes of each read's statements (EXPLAIN with the parameters of one call).

| Operation | owner | rls |
|-----------|-------|-----|
| get by key | Index Scan mat_records_p3_pkey | Index Scan mat_records_p3_pkey |
| list page of 50 | Index Scan mat_records_p3_pkey | Index Scan mat_records_p3_pkey |
| links of a record | Bitmap Heap Scan<br>Bitmap Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b<br>Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx | Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Seq Scan |
| 2-hop neighborhood | Bitmap Heap Scan<br>Bitmap Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b<br>Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_links_p3_pkey<br>Index Scan mat_records_p3_pkey | Bitmap Heap Scan<br>Bitmap Index Scan mat_records_attr_e90c8d94b5cd137b3bb8375b<br>Index Scan mat_links_p3_domain_id_branch_relation_id_to_key_idx<br>Index Scan mat_links_p3_pkey<br>Index Scan mat_records_p3_pkey |
| query by indexed attribute | Index Scan mat_records_attr_71d8b499dff05dab64f394fe | Index Scan mat_records_p3_pkey |
| full-text search | Bitmap Heap Scan<br>Bitmap Index Scan mat_records_p3_search_idx | Seq Scan |

## Reading the results

- With the owner role every read is within its budget, and every plan is an index scan.
- Key, page and stored-Link reads compare text columns only; text equality and ordering are
  leakproof, so they keep their index plans under row-level security.
- Per-Attribute, foreign-key and full-text conditions call functions Postgres does not mark
  leakproof (`->>`, casts, `@@`). With a role subject to RLS the planner applies the policy
  first and cannot use them as index conditions: attribute queries filter the Kind's rows,
  search scans the Domain's partition, and inbound foreign-key Links (`issued_to`: the
  purchase orders of a supplier) filter every purchase order. That is what pushes Links of a
  Record, the neighborhood and search over budget in the `rls` columns. Design 8.7 ("Agent
  read budgets") lists the options.
- Search ranks at most `store.reads.search_candidates` matches, so very common terms cost a
  bounded amount.
- Branch creation copies the Records, Links and history rows of `main` (about twice the Records
  and Links) and builds the new branch's per-Attribute indexes in the same transaction.

Regenerate with:

```sh
uv run python tests/perf/bench.py --scale 540 --extra-links 4 --out docs/performance.md
```
