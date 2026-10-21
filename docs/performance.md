# Performance baseline

Latency of the agent read paths on a materialized head (design 8, "Agent read budgets"), measured by `tests/perf/bench.py`. Every read is one transaction that scopes row-level security to the Domain and runs one store primitive (`lorebase_store.reads`, `lorebase_store.traverse`); times are client side over a local socket, so they include the driver and the RLS scoping round trip.

## Dataset

- `supply` Domain from `examples/supply/scripts/make_supply_data.py --scale 540`, plus 4 extra `supplied_by` and `stocked_at` Links per Part.
- 99,900 Records and 302,940 Links on `main`, loaded through the commit pipeline in 45 commits (151 s, then `VACUUM ANALYZE`).
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
| get by key | 5.00 | 1.21 | 1.63 | 1.54 | 1.86 |
| list page of 50 | 20.00 | 1.63 | 1.87 | 3.62 | 5.41 |
| links of a record | 10.00 | 1.58 | 2.34 | 3.00 | 4.54 |
| 2-hop neighborhood | 30.00 | 5.05 | 7.19 | 5.90 | 8.29 |
| query by indexed attribute | 25.00 | 2.00 | 3.02 | 1.80 | 2.40 |
| full-text search | 50.00 | 2.38 | 3.46 | 2.58 | 4.02 |
| full-text search with typo-tolerant titles | 50.00 | 10.86 | 28.66 | 11.27 | 32.10 |

## Writes (milliseconds)

| Operation | Runs | p50 | p95 |
|-----------|-----:|----:|----:|
| branch creation (copy) | 3 | 8008.24 | 8275.43 |
| commit of 1000 changes | 5 | 416.94 | 501.41 |

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

Regenerate with:

```sh
uv run python tests/perf/bench.py --scale 540 --extra-links 4 --out docs/performance.md
```
