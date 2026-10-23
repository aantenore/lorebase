# Validation record

Every validation run of the project in one place: what was run, where, how, what it showed, what it found and what was fixed. Each section links to the document that holds the detail; this page does not replace them and takes its numbers from them. Where the repository does not record a figure or a date, the section says so rather than filling the gap.

| Section | What it covers |
|---------|----------------|
| [How to reproduce](#how-to-reproduce) | The commands behind every run below |
| [Performance baselines](#performance-baselines) | Read, write and branch latency at 100k, 1M and 10M Records; Canvas layout at 5,000 nodes |
| [End-to-end](#end-to-end) | The five-day scenario and the web app's Playwright suite |
| [Security reviews](#security-reviews) | Each review round, its findings and what remains |
| [Interop and connectors](#interop-and-connectors) | LinkML, SQL Sources, JSON, Parquet and HTTP JSON |
| [Live model runs](#live-model-runs) | Tier 2 extraction and schema suggestions with Claude Haiku 5.5 and LFM 2.5 8B, fastembed embeddings and hybrid search; Guide's Explore, the process agent and extraction with Claude Haiku, Sonnet and Opus 5.5 |
| [Fresh-clone quickstart check](#fresh-clone-quickstart-check) | The quickstart followed from a fresh clone: friction found, fixes, measured times |

## How to reproduce

The commands assume a checkout, [uv](https://docs.astral.sh/uv/), Docker with compose for everything marked "Docker", and, for the web app, Node with pnpm. Run `make sync` once (it installs the workspace packages, the document parsers and the development tools). `make help` lists every target.

| Run | Command | Needs |
|-----|---------|-------|
| Unit tests, no external services | `make test-unit` | none |
| Integration tests, including the SQL Source tests | `make test-int` | Docker |
| Performance harness at a small scale | `make test-perf` | Docker |
| Benchmarks at 1M and 10M Records on their own Postgres (`tests/perf/compose.yaml`; the seeded databases are kept, `make perf-down` deletes them) | `make perf-1m`, `make perf-10m` | Docker; about 15 GB of disk for 1M and 80 GB for 10M |
| Dependency advisories | `make security` (`security-python`, `security-web`) | network |
| Five-day scenario | `make demo` (`KEEP=1` leaves the stack up, `make demo-down` removes it) | Docker |
| Documentation build, strict | `make docs` | none |
| Tests impacted by the current diff | `make test-changed` | none |

The harnesses that have no make target:

```sh
# 100k Records (the committed baseline); writes the report
uv run python tests/perf/bench.py --scale 540 --extra-links 4 --out docs/performance.md

# Branch copy and deletion jobs on the same dataset
uv run python tests/perf/bench.py --scale 540 --extra-links 4 --branch-iterations 2

# 1M Records. --seed-database keeps the loaded dataset in a database of its own
# and measures a copy of it, so only the first run pays the load (about 33 minutes)
LOREBASE_TEST_DATABASE_URL=postgresql://USER:PASSWORD@127.0.0.1:PORT/postgres \
uv run python tests/perf/bench.py --scale 5406 --extra-links 4 --iterations 1000 --warmup 100 \
  --branch-iterations 2 --seed-database perf_seed_1m

# Web app: Canvas layout benchmark and the Playwright suite (in apps/web)
pnpm perf:canvas
pnpm e2e

# Targeted suites named in the sections below
uv run pytest packages/cli/tests/test_linkml.py
uv run pytest packages/scribe/tests/integration/test_sql_sources.py -m integration
uv run pytest tests/tooling/test_helm_db_roles.py        # needs a helm binary
```

Other bench.py flags: `--iterations`, `--warmup`, `--branch-iterations`, `--commit-iterations`, `--commit-changes`, `--overlay-iterations`, `--overlay-changes`, `--trace-iterations` and `--trace-warmup` (agent trace overhead through Keeper), `--replicas` (load a share through the commit pipeline and copy its materialized rows), `--seed-only`, `--in-place` (measure the seed database itself), `--copy-batches` and `--no-pair-copy` (bound the branch copies at 10M). Which suites run where in CI is described in each section.

## Performance baselines

The detail, with every table, plans and open issues, is in [performance](performance.md).

### 100k Records

- **Date:** 2026-10-22.
- **Environment:** Apple M5 Pro, 15 cores, 24 GiB, Darwin 25.6.0 arm64; Python 3.12.13; Postgres 16.15 in Docker on the same machine with `shared_buffers` 128MB, `effective_cache_size` 4GB and `work_mem` 4MB. The laptop ran other work, and repeated runs differ by a few milliseconds at p95 (up to ten for typo-tolerant search).
- **Method:** the `supply` Domain from the example generator at `--scale 540` plus four extra Links per Part: 99,900 Records and 302,940 Links on `main`, loaded through the commit pipeline in 45 commits (148 s). 300 timed calls per read after 30 warm-up calls, random keys. Each read is one transaction that scopes row-level security to the Domain and runs one store primitive. `owner` is a role that bypasses row-level security; `rls` is subject to the `domain_isolation` policies, like the production application role.

p95 in milliseconds against the budgets of design 8.7:

| Read | Budget | owner | rls |
|------|-------:|------:|----:|
| get by key | 5 | 1.31 | 1.39 |
| list page of 50 | 20 | 1.80 | 1.81 |
| links of a Record | 10 | 1.94 | 1.94 |
| 2-hop neighborhood | 30 | 5.43 | 6.01 |
| query by indexed attribute | 25 | 1.95 | 3.23 |
| full-text search | 50 | 3.15 | 2.80 |
| typo-tolerant full-text search | 50 | 4.12 | 4.80 |
| record history page | 10\* | 3.16 | 3.19 |
| record lineage | 10\* | 3.86 | 4.56 |

\* Design 8.7 sets no budget for these; they are held to the budget of Links of a Record.

Writes, same run: branch creation by copy 8,162.80 ms at p50 (3 runs), by overlay 13.17 ms (20 runs), a commit of 1,000 changes 397.32 ms at p50 and 527.19 ms at p95.

**Plain SQL against definer functions.** With `store.reads.use_definer_functions: false`, a role subject to row-level security cannot use conditions built from functions Postgres does not mark leakproof as index conditions. The baseline of 2026-10-20 measured, at p95 for that role, 28.06 ms for Links of a Record, 136.73 ms for the neighborhood and 68.14 ms for search. The default path through the security definer functions of revision `0005` brings every read within budget.

**Overlay against copy.** An overlay branch and a copy branch got the same commit of 1,000 Part updates, then `main` committed 1,000 others; the overlay stores 2,000 rows, the copy 402,840. Reads alternated between the two. At 100k Records the overlay costs between -4% and +32% at p50, with the largest overhead on typo-tolerant search (+32%) and Links of a Record (+18 to +28%), and every read far within budget. Before revisions `0011` and `0012` and the overlay's own indexes, search cost +93 to +101% at p50 and the neighborhood +50%.

**Typo-tolerant search, before and after the word lexicon** (migration `0018`, same machine, 100k Records), p50 and p95 in milliseconds:

| | before | after |
|---|---|---|
| owner | 11.64 / 31.06 | 3.09 / 4.12 |
| rls | 11.24 / 26.91 | 3.35 / 4.80 |
| overlay, rls | 11.86 / 28.19 | 4.92 / 6.17 |

The price is on the write side: a commit of 1,000 changes went from 355.22 to 397.32 ms at p50 and branch creation by copy from 7,712.82 to 8,162.80 ms. Without the lexicon triggers the same commit measured 262 to 297 ms against 320 to 388 ms with them at scale 60.

**Background copy and deletion** (migration `0017`, 100k dataset, default 30 s statement timeout and 5,000-row batches): the request that creates a copy takes 76.65 ms at p50 where the single-transaction copy took 7.3 s; the background job takes 9.7 s against 8.0 s for the single `INSERT ... SELECT`; one batch takes 47.63 ms at p50 and 531.93 ms at most. Deleting a branch is a 7.81 ms request plus a job of 8.6 to 12.3 s.

### 1M Records

- **Date:** 2026-10-22.
- **Environment:** the same machine; Postgres 16.15 with pgvector in Docker Desktop with an 8 GiB VM, database 13 GB after loading. Two runs: container defaults (`shared_buffers` 128MB) and `shared_buffers` 2GB with `effective_cache_size` 6GB (the figures below).
- **Method:** 1,000,110 Records and 3,032,766 Links loaded in 408 commits of up to 10,000 changes (1,961 s with index builds and `VACUUM ANALYZE`); 1,000 timed calls per read and 100 warm-up calls. History and lineage are timed as Keeper serves them, without the HTTP layer.

p95 in milliseconds, first run (before the lexicon):

| Read | Budget | owner | rls | rls, 128MB |
|------|-------:|------:|----:|-----------:|
| get by key | 5 | 3.53 | 3.20 | 4.76 |
| list page of 50 | 20 | 4.43 | 4.66 | 5.17 |
| links of a Record | 10 | 4.27 | 4.65 | 5.61 |
| 2-hop neighborhood | 30 | 16.98 | 9.48 | 21.03 |
| query by indexed attribute | 25 | 4.22 | 5.07 | 4.73 |
| full-text search | 50 | 7.79 | 8.14 | 12.24 |
| typo-tolerant search (whole-title trigram match) | 50 | 116.37 | 119.38 | 156.52 |
| record history page | 10\* | 7.82 | 7.71 | 8.33 |
| record lineage | 10\* | 9.36 | 9.33 | 10.98 |

Writes: branch creation by copy 84,529 ms at p50 (94,825 ms with 128MB), by overlay 23.68 ms; a commit of 1,000 changes 2,367 ms at p50 and 4,276 ms at p95 (1,357 ms at p50 with 128MB), against 0.4 s at 100k.

**Overlay against copy at 1M.** Key, page, Links, attribute, full-text, history and lineage reads stay within budget on all three branches, with the overlay close to the copy (for example 3.67 against 4.19 ms at p50 for a page, rls). Over budget on branches: the 2-hop neighborhood (29 to 34 ms at p95 on the copy and the overlay, against 30), and typo-tolerant search (70 to 123 ms at p95).

**With the word lexicon** (same fixture, 2GB buffers, reloaded in 1,831 s against 1,961 s), p95 in milliseconds:

| Read | owner | rls |
|------|------:|----:|
| full-text search | 5.85 | 6.04 |
| typo-tolerant full-text search | 7.55 | 7.91 |
| typo-tolerant, copy branch | 11.80 | 12.99 |
| typo-tolerant, overlay branch | 12.35 | 13.98 |

Typo-tolerant search went from 116 to 123 ms to 7.6 to 14 ms at p95, 15 times less on `main`, and the overlay stays within 10% of the copy. The lexicon triggers cost about 5% of a commit at this size (four commits of 1,000 updates, warm, twice: 654 to 727 ms with the triggers, 642 to 749 ms without).

### Issues found and fixed

| Finding | Resolution |
|---------|------------|
| At 1M Records, typo-tolerant title search ran at 1.4 to 3.4 times its budget: the trigram GIN index on titles is lossy and returned 154,298 rows for one word, all failing the recheck | Two alternatives were measured and not adopted (a GiST trigram index; nested partial GIN indexes by trigram count, which reached 102 ms at p95 for three more indexes). Fixed by trigram matching on the distinct words of each branch (migration `0018`); re-measured at 7.6 to 14 ms |
| A copy branch at 1M Records took 85 to 95 s and its deletion more than 30 s, so both failed through the API at the default statement timeout | Copies and deletions of large branches run as jobs in batches (migration `0017`); measured at 100k. The 1M run has not been repeated since |
| A semi-join in the history phase of the background copy scanned every Revision of the Domain per row and made that phase four times slower | The batch probes `revisions` by primary key per row |
| Search on an overlay cost +93 to +101% at p50; the neighborhood +50% | Revisions `0011` (GIN index on domain, branch and search vector) and `0012`, plus the overlay's own per-Attribute indexes; `test_overlay_reads_keep_index_plans` checks the plans |
| At 10M Records commits slowed with the Links and the Kinds they wrote: Link lookups by `(from, to)` planned one index scan per pair, cardinality checks were quadratic in the batch, search vectors were refreshed by scanning the Kind and unique values bypassed their index | A semi-join on two arrays, counts per key, refresh by key, the unique index's expression; 67 to 48 s for a 28,000-Record load, and statements of 7 s and a minute at 10M down to milliseconds |
| The background copy of a branch failed at 10M Records: its first step counted the base's 85 million rows, past the statement timeout | The planner's estimate until the switch, then the rows read |
| Plain SQL under row-level security lost its index plans | Reads go through the security definer functions by default (revision `0005`) |

### Open at 1M Records

The 2-hop neighborhood on branches other than `main` (29 to 42 ms at p95 across runs, against 30) and lineage at its proposed budget (9.3 ms on `main`, 9.8 to 11.0 on branches and with 128MB). The plans are index scans only; the time goes to pages missing from shared buffers and from the VM's page cache, and the budget assumes the 32 GB reference hardware of design 6.14. The 1M run has not been repeated since the background copy jobs. A commit measured right after two 4-million-row copies took 5.5 s at p50 because the page cache was full of them; measured again warm it took 0.65 to 0.75 s.

A covering reverse Link index (2026-10-22: `INCLUDE (from_key, "values", ...)`, so the inbound
hop reads the index alone) cut the blocks the neighborhood reads by 12% at 100k Records, and
those read from outside shared buffers on branches by a third, but it was withdrawn (revision
`0026` restores the plain index): a B-tree entry is limited to about 2.7 KB, so Links with large
`values` could no longer be written, and without `values` the hop reads the heap anyway. What
remains is the heap read of the Records the neighborhood returns, about one page each.

### 10M Records

- **Date:** 2026-10-22.
- **Environment:** the same machine, shared with other test workloads throughout (load average
  14 to 209 on 15 cores, median 116); Postgres of `tests/perf/compose.yaml` (`shared_buffers`
  2GB, `effective_cache_size` 6GB) in the 8 GiB Docker VM; database 64 GB.
- **Method:** `make perf-10m`. 10,001,100 Records and 30,327,660 Links: the 1M fixture loaded
  through the commit pipeline, its materialized head copied nine times with renumbered keys
  (`--replicas 10`; the commit pipeline alone needed about ten hours here). 1,000 timed calls
  per read and role after 100 warm-up calls; the measured branch copy stops after 200 batches
  and the overlay is compared with `main`.

p95 in milliseconds:

| Read | Budget | owner | rls |
|------|-------:|------:|----:|
| get by key | 5 | 6.08 | 7.15 |
| list page of 50 | 20 | 9.59 | 15.85 |
| links of a Record | 10 | 9.15 | 15.97 |
| 2-hop neighborhood | 30 | 95.64 | 152.84 |
| query by indexed attribute | 25 | 5.10 | 11.99 |
| full-text search | 50 | 58.72 | 110.92 |
| typo-tolerant full-text search | 50 | 97.44 | 127.35 |
| partial-match full-text search | 50 | 262.83 | 272.95 |
| record history page | 10\* | 17.36 | 27.75 |
| record lineage | 10\* | 16.70 | 23.66 |

Writes: an overlay branch in 42 ms and a copy request in 147 ms at p50, the background copy at
4,855 rows per second (batches 90 ms at p50, 294 ms at p95), deletion a 74 ms request; a commit
of 1,000 changes 3.3 s at p50. The agent trace adds nothing measurable through Keeper (+1 to
+6% at 1M with fair alternation, within noise at 10M).

Not met: the neighborhood, full-text search (all three variants), history and lineage, and get
by key by 1 to 2 ms; the roadmap item stays open. The neighborhood reads about 400 pages per
call that are not in memory at 0.26 ms each (warm: 15.5 ms at p50, 29.6 at p95); search
enumerates every GIN match of a frequent term before ranking a bounded number (the generator's
titles use 695 distinct words). Detail, the neighborhood options evaluated at 1M and 10M, and
the commit and copy fixes found on the way: [performance](performance.md#10m-records).

**Re-run at 1M Records** with the same harness, the same evening (load average median 21):
search, typo tolerance and partial matching within budget (14 to 37 ms at p95 on all branches),
the neighborhood 20 ms at p95 warm and 65 to 77 ms on the copy and the overlay, get by key 7.5
to 8.3 ms. Detail: [performance](performance.md#1m-records-run-with-the-10m-harness).

**Focused comparisons, 2026-10-23** (`tests/perf/focus.py`, seeds with realistic Zipf text;
load average 21 to 29 during the 1M runs and 41 to 48 during the 10M run, so only the paired
columns of one run compare):

- Search sized by the word lexicon: with the first defaults (`search.common_term_docs` 20,000,
  `common_term_candidates` 5,000) title-word search cut 50 truncated rankings of 300 to 5 at
  1M, but p95 went from 316 to 1,067 ms (1M) and from 745 to 3,280 ms (10M). The defaults are
  now 2,000 and 1,000: 16.2 against 13.9 ms at p50 and 54 against 48 at p95 for a title word
  at 1M, truncated rankings 50 to 29 (misspelled words 38 to 18). Very common words stay over
  budget at 10M with or without it (250 ms at p50): the GIN bitmap is built before ranking.
- `values: false` on the 2-hop neighborhood: responses 68% smaller (100 KB to 32 KB at 1M),
  time unchanged within noise (-3 to +4 ms at p50) cold and warm, on `main` and on an overlay, at 1M and at 10M (cold 239 to 253 ms at p50 at 10M either way).
- A narrower `mat_history` index for history and lineage was evaluated at 10M and not
  adopted: the history range reads two pages from outside memory (index leaf and heap), and
  an index that saved the heap page would carry the row's payload, about as large as the
  table, for about 1 ms of the 17 to 28 ms at p95.

Detail: [performance](performance.md#search-on-very-common-terms).

**Working set and storage profiles, 2026-10-23** (`tests/perf/working_set.py` on copies of
the seeds; load average 45 to 105 at 1M, 40 to 75 at 10M): at 1M Records the hot set of the
agent read path went from 12.5 GB (13,120 bytes per Record) to 5.5 GB in `balanced` and
`compact` and 6.7 GB in `fast` after `storage apply --compact --reindex`; the cold 2-hop
neighborhood from 291 to 42 ms at p50 (`balanced`), with 211 against 138 pages read from
outside shared buffers. Commits of 1,000 Part updates with the lineage map triggers on and off
alternating: p50 256 and 257 ms. A half-precision HNSW index is 43% smaller and 6 points of
recall@10 lower, so only `compact` uses it. At 10M Records (Docker VM of 16 GiB, `shared_buffers` 6GB,
`effective_cache_size` 12GB, load 9 to 15) the hot set went from 57 GB (6,165 bytes per
Record) to 44.1 GB (4,729) in `balanced` and 43.3 GB (4,651) in `compact`, measured; the cold
2-hop neighborhood from 122 to 100 ms at p50 (96 in `compact`). Commits of 1,000 Part updates
at 10M under the same load: p50 2,711 ms `balanced`, 2,332 `compact` (the 2.1 s of the 1M
`compact` run was its load). Detail: [performance](performance.md#working-set).

### Canvas, 5,000 nodes and 10,000 edges

- **Date:** 2026-10-22, median of 3 runs.
- **Environment:** the same machine; a production build served by `vite preview`, headless Chromium with software WebGL (SwiftShader), 1440x900. Software WebGL makes every frame slower than on a GPU, so the figures compare the two rows with each other and are not what a laptop shows.
- **Method:** `pnpm perf:canvas` on the generated graph of the mock API (`VITE_MOCK_PERF_NODES=5000`, `VITE_MOCK_PERF_EDGES=10000`, `VITE_CANVAS_MAX_NODES=5000`). Time to first render runs from navigation start to the second animation frame after the canvas exists; the pan is 80 mouse moves with the button down.

| | layout on the main thread | layout in a worker |
|---|---|---|
| time to first render | 1,675 ms | 612 ms |
| main thread blocked before it (long tasks over 50 ms) | 1,497 ms (longest 1,361 ms) | 463 ms (longest 513 ms) |
| pan right after first render, mean frame | 218 ms | 151 ms |
| pan right after first render, p95 frame | 617 ms | 500 ms |
| pan on the settled graph, mean frame | not applicable | 122 ms |

Before, 100 ForceAtlas2 iterations ran on the main thread before the first draw and the layout never improved afterwards. Now the graph is drawn from its seed positions at once, the worker refines it for up to 20 s, and Barnes-Hut, edge hiding while moving, no edge labels and fewer node labels apply at this size. Frame times during a pan are dominated by the software rasterizer in both rows; what the run shows is the time to first render and a free main thread. Detail: [performance](performance.md#web-app-canvas-5000-nodes-10000-edges).

## End-to-end

### Five-day scenario

- **Detail:** [demo](demo.md). The scenario is design 3.2 as a script: a fictional pilot run for five days against a real stack (Postgres, Keeper, Scribe) with the `lorebase` command line, a scripted MCP client for Herald and a webhook receiver on the host.
- **Environment:** the stack is its own compose project, `lorebase-demo`, with Keeper on port 18080 and Postgres on 15432. Every model task uses the deterministic fake provider, so no model is downloaded and no secret is used.
- **Method:** `make demo`. Each day ends with assertions printed as `check ok: ...`; a failed one prints `assertion failed: ...` and exits with code 1, as does a failing CLI command. The run ends with a table of seconds per day, also on failure.
- **Timings recorded in the demo page** (a laptop, undated): building the image and starting the stack 374 s on the first build, dominated by pulling base images; the five days 45 s, document and checks included. With the image cached, only Postgres, the migrations and Keeper have to start.
- **Checks:** the scenario runs 79 checks. In CI the first pass (run 39738332331) took about 3 min 13 s on a cold cache; locally a run takes about 41 s once the stack is up. The scenario also builds the `lorebase-ml` image, and that run is green (run 39808555188).

What each day asserts:

| Day | Asserted |
|-----|----------|
| 1 | `main` holds the curated schema, reached through the merged Proposal of `model/initial`, and is protected |
| 2 | every CSV row is a Record or a Link on `main`; the derived value answers a query; search finds Parts; the extraction Proposal holds the Contract and its Link from tier 0, pending, and after the decision both are on `main` with `extraction` Lineage |
| 3 | the Proposal merged, its Conflicts were raised and none is open; the hand-decided value and the non-conflicting changes of both teams are on `main`; time travel shows day 2 |
| 4 | the Source ran twice; the second run found two changed rows and its Proposal merged by policy; the change feed lists them; the webhook delivered with a valid signature |
| 5 | Herald lists its tools; queries and context packs answer; a critical flag is refused with `review-required` and, with a task, opens a Proposal; the rate limit serves a burst and then answers 429 with `Retry-After`; both flags reach `main` only through merged Proposals; a direct write to `main` is refused; the database restored from a bundle has the head and the Record count of the live one |

**CI.** The End-to-end workflow (`.github/workflows/e2e.yaml`) runs it on pushes to `main`, nightly, on demand and on pull requests that change `examples/`, `packages/`, `deploy/compose/` or `deploy/docker/`. It builds the backend image from the checkout with a layer cache, starts the stack with `stack.sh up` (`DEMO_NO_BUILD=1`), runs `run.sh`, writes the timing table into the job summary and, on failure, uploads the compose logs, the transcript and the work files (without keys and secrets) as the artifact `five-day-demo-logs`. The job is limited to 20 minutes.

**Findings of the scenario about the product,** all recorded as behavior the script works around or states (see [demo](demo.md#what-the-demo-showed-about-the-product)): a required Attribute with a default is backfilled by a schema change but not applied by imports and Source syncs; imports replace whole rows; an Operation restricted by `permissions` needs a service principal named like the permitted subject; the first administrator key comes only from `python -m lorebase_keeper bootstrap`; a critical flag is refused even on a work branch because its review policy counts on every branch; webhooks deliver only events committed after registration. Running the scenario also showed that the compose stack had to run Keeper and Scribe as a role subject to row-level security, which the second security pass fixed (F6, below).

### Web app, Playwright

- **Detail:** [security review](security-review.md#fixed-in-the-second-pass), F6; the suite itself is `apps/web/e2e`.
- **Method:** `pnpm e2e` in `apps/web` starts a compose stack of its own (project `lorebase-e2e`, own image name and host ports) with Postgres, the application role, the migrations and Keeper, seeds the supply Domain and drives the web app in live mode against that Keeper. Set `LOREBASE_E2E_URL` to use a Keeper that is already running instead.
- **Coverage:** 11 tests in five files: browsing (3), Branches (2), schema changes (3), Conflict resolution in Review (1) and Settings (2).
- **CI:** the `web-e2e` job of the CI workflow runs it with a Postgres service and uploads the Playwright report as an artifact; the aggregate job of CI requires it.
- **Result recorded:** after the second security pass the suite passed on the stack (11 tests), where `pg_stat_activity` showed Keeper connected as `lorebase_app` with `rolsuper` and `rolbypassrls` false. An earlier commit (`Keep e2e and the chart working with the security fixes`) had to set the e2e stack's bind address and `allow_insecure` explicitly after F3 made anonymous mode on a non-loopback address a startup error.

## Security reviews

Documents: [security review](security-review.md) (findings, fixes, tool results), [threat model](threat-model.md) (STRIDE per trust boundary, residual risks by priority) and the security notes of [deployment](deployment.md#web-sign-in) and [operations](operations.md). Every fixed finding has its own commit with a test that fails without the fix. Where a round has no findings count below, the repository records the fixes and their tests but not a finding-by-finding list; the commit history is the source.

| Round | Date (of its commits) | Scope | Outcome |
|-------|------|-------|---------|
| First pass and second pass | 2026-10-21 | The trust boundaries of `keeper`, `store`, `scribe`, `inference` and `herald`, `deploy/` and the workflows, at commit `4bd2d26` | 23 findings, all fixed |
| Web sign-in (OIDC, Keeper as backend-for-frontend) | 2026-10-22 | The session cookie boundary | Threats tabulated with mitigations; one hardening commit after the first implementation |
| Threat model and its hardening | 2026-10-22 | All nine trust boundaries by STRIDE | Residual risks ranked; twelve fixed since |
| Sensitivity masking and PII vault | 2026-10-22 | Reads, history, erasure, bundles | Four rounds, then a vault and bundle review and its re-review; every High and Medium fixed ([rounds](#pii-vault-and-sensitivity-masking)); residual risks 2 to 7 of the threat model |
| Outbox and Operation events | 2026-10-22 | Delivery state, sensitive values in events | Three High findings, all fixed ([details](#outbox-and-operation-events)) |
| Shared rate limits, read audit, off-database anchor | 2026-10-22 | Rate limiting, audit | Fixed; a follow-up commit adds outage and freshness handling |
| Helm database roles | 2026-10-22 | The chart's database connections | Installed on a kind cluster |

### The 23-finding review

Method: reading of the boundaries (unauthenticated to authenticated, narrowed grant to Domain-wide, Domain to installation, agent to human-reviewed write, external data to model prompt, Keeper to Postgres role), `make security` (pip-audit on the lock file and `pnpm audit --audit-level=high`), `uvx bandit -r packages -q -ll`, `pnpm audit` in `apps/web`, and a grep of every logging call near tokens, keys, DSNs and secrets.

| Severity | Count | Findings |
|----------|------:|----------|
| Critical | 1 | F1: a Source could resolve any secret reference, including the database DSN |
| High | 2 | F2: Source errors echoed a resolved secret; F3: `auth.mode: none` could be served on a non-loopback bind address |
| Medium | 4 | F4: webhook targets in shared address space accepted; F5: a webhook target could stall delivery for every Domain; F6: the compose stack connected as the Postgres superuser; F7: `change-me` fallbacks for the LiteLLM and Grafana secrets |
| Low | 13 | F8 to F20: the `*` Domain id, index drop jobs, the per-task inference key, persisted CI credentials, definer function arguments, the table schema check of revision `0005`, bundle import limits, schema inference sources, the missing API key allowlist, plain-HTTP OIDC, CSV Source host checks, the Helm network policy default, failed authentications not rate limited |
| Info | 3 | F21 to F23: provider error text and proxy variables, Domain id literals, event streams authorized once |

F1 to F5 and F8 to F11 were fixed in the first pass; F6, F7 and F12 to F23 in the second. **Tool results:** pip-audit and `pnpm audit` found no known vulnerabilities; bandit reported 42 medium and 0 high, of which 39 are B608 on SQL assembled from constants, whitelisted keywords or checked identifiers, one is a `SafeLoader` subclass and the B104 hits are in tests, with no real hit; no logging call at INFO or above emits a token, key, pepper, DSN or password.

Limits that remained after the second pass: the per-prefix failed-authentication bucket can throttle a known key (`per_key_prefix_enabled: false` removes it); bundle headers of pax and GNU long names are bounded by the archive size only; the `O_NOFOLLOW` walk is POSIX only; schema inference sources are bound to Domains but not to schemas or tables.

### Web sign-in

Reviewed as a new boundary: the session cookie. The security review tables ten threats with their mitigations (script theft of tokens, session id theft from the database, cookie injection, CSRF, login CSRF and replayed ID tokens, session fixation, open redirects, orphaned sessions, redirect URI manipulation, metadata over plain HTTP). One hardening commit followed the implementation: it limited sign-in routes per client address, moved the CSRF check before the session lookup so a forged request neither extends nor refreshes a session, and bound refreshed ID tokens to the sign-in nonce. Tests cover state, nonce, PKCE, audience, tampered and expired state cookies, CSRF, `Origin` and `Sec-Fetch-Site`, idle and absolute expiry, rotation, refresh, refused refresh, logout revocation, open redirects and cookie flags, with an integration test as a role without `BYPASSRLS`. Remaining limits: the CSRF token is readable by any origin that `keeper.cors` allows with credentials; sessions survive a provider outage until it ends or a timeout applies; one sign-in at a time per browser; a refresh that fails mid-way waits `refresh_lease_s`; reverse proxies that log full URLs hold single-use authorization codes.

### Threat model and its hardening

The threat model covers nine boundaries (browser, API clients, authorization inside Keeper, outbound reads, Herald and MCP, Postgres roles, webhooks, inference providers, supply chain). Its first list of residual risks led to these fixes, each with tests: audit log append-only for the application role (revision `0015`); document URL intake that checks and pins addresses; PDF and DOCX parsed in a child process with CPU, memory, time and size limits and no network; webhook signing keys sealed at rest (revision `0016`); security headers from Keeper and the web image; a dependency audit in CI with a reviewed allowlist that started empty because both audits found nothing; base images pinned by digest; and the Helm chart connecting as the application role instead of the table owner. Then came shared rate limits across Keepers (revision `0022`), read auditing, signed audit checkpoints outside the database, and sensitivity masking with the PII vault (revision `0019`).

### PII vault and sensitivity masking

Values of `pii`, `confidential` and `restricted` Attributes are masked on every read path unless the caller holds the matching clearance; filters, ordering and grouping on them need it too; they stay out of search vectors, the lexicon, snippets and titles. Attributes with `vault: true` store an encrypted value in the vault and a token in the Record, and erasure by Record key or value clears it for every Revision and is audited in the same transaction. The residual risks recorded in the threat model are the repository's list of what the review rounds left open: inline sensitive values and Record keys stay in history; erasure misses tokens held under an earlier key and matches the canonical value exactly; vault keys sit in the services' configuration and `vault.token_key` cannot be rotated; small value oracles remain (the plain SHA-256 parameter digest of Operation invocations without the vault keys, Kind health listing which Records share a sensitive value, validation codes revealing a format); branches forked before a tag keep stale titles and search vectors until rewritten, which verification reports as `sensitivity_stale`; the Operation analysis is conservative; and `lorebase admin erase --value V` puts the value in the shell history unless `--value -` is used.

The feature went through four security review rounds, then a later review of the vault and of bundles, and a re-review of that. Every High and Medium finding was fixed, with a test that fails without the fix.

| Round | High | Medium | Findings |
|-------|-----:|-------:|----------|
| 1 | 5 | 3 | High: masking taken from a schema the caller chose; derived copies of sensitive values; extraction items exposing values; plaintext outside the vault; bundle import bypassing tokenization. Medium: conflict maps failing open; sensitive keys; a unique-value oracle |
| 2 | 1 | 4 | High: branch-local tag removal combined with a derived copy. Medium: search text, Kind health, recorded parameters, key and unique checks on branch tags. Fixed at the root with effective tags: the tags of the default branch are unioned with the branch's own, so a branch cannot lift a protection |
| 3 | 2 | several | High: title filtering; Operation target, from and to expressions and error messages that quoted values. Medium: stale Operations, migration messages, a tag-removal race, cache keys |
| 4 | 0 | several | Medium: snippets with hidden titles, agent writes, field resolution by Kind, an early 304 |
| Vault and bundle review | 1 | 6 | High: erasure missed a concurrent key change. Medium: unlimited takeovers; erasure denial of service through lineage; an import oracle, closed by the new `pii.import` permission; export with erased renames; lineage and tombstones missing from bundles; tombstones ignored on import |
| Re-review | n/a | n/a | One finding: an extraction bypass of `pii.import`, fixed |

Round 3 and round 4 counts of Medium findings are not given: the fixes and their tests are in the commit history, the finding-by-finding list is not.

### Outbox and Operation events

A committed Operation execution writes its `operation.<name>` events in the transaction of its Revision, invocation record and audit entry, so an event exists exactly when the execution committed. Each outbox consumer owns its pending rows (filled by a trigger in the writing transaction; migration `0020`), so consumers no longer take events from each other and a late commit is not skipped. Sensitive values reach a webhook only when it was created with matching clearances, vault values are opened when the delivery is sent (an erased one arrives as `webhooks.erased_value`), and every attempt carries an idempotency key: delivery is at least once and unordered. A security review of the outbox found three High issues, all fixed: a forged `$sensitive` wrapper in event data, agent task writes that emitted events before review, and strict name checks that broke stored schemas.

### Rate limits, read audit and audit anchors

Shared token buckets (UNLOGGED `rate_limit_buckets`, revision `0022`) so all Keepers together never exceed a limit, tested with concurrent leases; failed-authentication buckets fail closed. Read auditing on its own hash chains so read traffic does not touch the chains of actions; reads that showed or masked a sensitive value are never sampled out. Ed25519-signed checkpoints of the chains written to a file, HTTP endpoint or S3-compatible bucket, so cutting the tail of a chain or recomputing it is detected; the Signature Version 4 signer is tested against the published example. A follow-up commit keeps clients already known in during a database outage (a reading at most `stale_reading_s` old still allows them), reserves queue room for reads of sensitive values and requires fresh checkpoints. Residual: the read stream is evidence of what it holds, not proof that nothing else was read, and entries newer than the latest checkpoint (one hour by default) are not covered.

### Helm database roles on a live cluster

The chart's database roles were installed on a kind cluster with an external Postgres: install, upgrade and `helm test`. Keeper and Scribe connected as `lorebase_app` with `rolsuper` and `rolbypassrls` false, no `CREATE` on `public` and no `UPDATE` or `DELETE` on `audit_log`, and the tables stayed owned by the maintenance role. The CloudNativePG mode was not run. Outside that run the chart has been checked by `helm lint --strict`, `helm template` of every profile and `kubeconform --strict` in CI, and by `tests/tooling/test_helm_network_policy.py` and `tests/tooling/test_helm_db_roles.py` (skipped without a `helm` binary); the hook ordering, the Ollama sidecar and NetworkPolicy behavior are untested end to end ([kubernetes](kubernetes.md#validation-status)). Finding fixed: the chart used one DSN, so the services connected as the table owner, row-level security held only through `FORCE` and the audit revoke did not apply to them; it now takes two DSNs and a `db-roles` init container, and refuses one user for both unless `database.allowSharedRole` is set.

### Residual risks

The open items are ranked in the threat model under [residual risks, by priority](threat-model.md#residual-risks-by-priority): webhook signing keys stored usable unless `webhooks.encryption_key` is set; erasure covering vault values only; vault keys in the services' configuration; small value oracles; branches forked before a tag; conservative Operation analysis; erasure from the command line; and the limits of the read audit and of checkpoints. The per-finding limits of the first review are in the security review.

## Operations

### Job release and reclaim in containers

Checked on a throwaway compose stack (own project name, ports and volume; Postgres, migrations and one or two Scribe containers on an image built from the repository, with the defaults `scribe.worker.shutdown_grace_s` 30, `shutdown_cancel_s` 5, `heartbeat_interval_s` 30 and `store.jobs.stale_after_seconds` 90). An `audit_verify` job was held in `running` by an `ACCESS EXCLUSIVE` lock on `audit_log` taken from another session; releasing the lock let it finish.

- `docker stop` (SIGTERM, default grace): the container stopped in 31 s with exit code 0 (30 s grace for the job, then it was cancelled). The log showed `worker stopping` with one running job and `job released on shutdown`; the job was `queued` with `attempts` 0, due at once, and none was left `running`. Starting the container again ran the job to `succeeded` on its first attempt.
- `docker kill` (SIGKILL) of the Scribe holding the job, with a second Scribe running: the job stayed `running` under the dead worker until its lock was 90 s old, then the second Scribe reclaimed it 91 s after the kill (`attempts` 2, new `locked_by`) and ran it to `succeeded` once the lock was gone. With a single Scribe nothing reclaims the job until it is started again.

Per-Attribute index builds sent no heartbeat when run with `run_index_jobs`, so a build longer than the stale threshold could have been reclaimed; it now sends one like the worker and `python -m lorebase_store.jobs work` do (`test_index_jobs_send_heartbeats_while_building`).

## Interop and connectors

Documents: [LinkML](linkml.md), [Scribe](scribe.md#sources), [schema inference](inference.md). The work dates from 2026-10-21 and 2026-10-22 (commit dates). The repository does not record timings or pass counts for these suites, so the tables name what each test checks.

### LinkML round trip

- **Method:** `uv run pytest packages/cli/tests/test_linkml.py`, unit tests with mocked HTTP for the Domain-facing commands. The translation needs PyYAML (`lorebase-cli[linkml]`) and does not use the LinkML runtime.

| Check | Test |
|-------|------|
| The supply example schema exports and imports back to the same schema | `test_supply_schema_round_trips_through_linkml` |
| Operations, Derivations and Sources, which LinkML cannot express, travel as annotations and round trip | `test_features_without_a_linkml_equivalent_round_trip_through_annotations` |
| Element ids survive, so a rename stays a rename | `test_round_trip_keeps_ids_so_renames_stay_renames` |
| Kinds, Traits, Relations (a pair of slots linked by `inverse`) and enums map as documented | `test_export_maps_kinds_traits_relations_and_enums` |
| The PersonInfo schema of the LinkML project imports as a valid schema, and what is left out is reported with its path | `test_personinfo_imports_as_a_valid_lorebase_schema`, `test_personinfo_import_reports_what_it_leaves_out` |
| Documents that cannot be mapped are refused | `test_export_refuses_documents_it_cannot_map`, `test_import_refuses_what_is_not_a_schema` |
| `--apply` merges into the current schema: Operations, Derivations, Sources and unrepresentable fields are kept, elements match by id or name, missing ones are kept with a warning unless `--prune` | `test_merge_*`, `test_schema_import_apply_merges_into_the_current_schema` |

**Found and fixed:** applying an import replaced the Domain's schema, which would drop everything LinkML cannot carry. A follow-up commit made `--apply` merge into the current schema instead and added `--prune`.

### SQL Sources

- **Method:** `uv run pytest packages/scribe/tests/integration/test_sql_sources.py -m integration` runs against real servers started with testcontainers and is skipped without Docker. Images: `LOREBASE_TEST_MYSQL_IMAGE` (default `mysql:8.0`) and `LOREBASE_TEST_MSSQL_IMAGE` (default `mcr.microsoft.com/mssql/server:2022-latest`, an amd64 image that is emulated on ARM hosts). MariaDB is covered by the MySQL connector and its own session settings (`max_statement_time`); the integration test names MySQL and SQL Server only. The unit tests in `packages/scribe/tests/test_sql_connectors.py` need no server.

| Check | Test |
|-------|------|
| A table streams in watermark order | `test_a_table_is_streamed_in_watermark_order` |
| An incremental run reads only rows past the watermark (`datetime2` keeps microseconds) | `test_an_incremental_run_reads_rows_past_the_watermark` |
| Scope filters and query Sources | `test_scope_filter_and_query_sources` |
| A wrong password is a read error that does not contain the password | `test_a_wrong_password_is_a_read_error_without_the_password` |
| A missing table is a read error | `test_a_missing_table_is_a_read_error` |
| A table is profiled for schema inference | `test_a_table_is_profiled_for_inference` |

Behavior these tests and the Scribe page pin down: MySQL and MariaDB sessions are `READ ONLY` and bounded by `max_execution_time`; SQL Server has no read-only transaction, so the login must be able to read only (documented); a `query` is wrapped in a subselect, and a top-level `ORDER BY` there gets `OFFSET 0 ROWS` (unit tests of the generated T-SQL: `packages/scribe/tests/test_sql_connectors.py`); a `rowversion` watermark is stored as hex and bound as bytes (same file).

### JSON, JSON Lines, Parquet and HTTP JSON

- **Method:** unit tests only, with no external service: `packages/scribe/tests/test_http_json_connector.py` `test_record_connectors.py`, `packages/store/tests/test_profiling_record_files.py` (including `records_path`), `packages/store/tests/test_profiling_sql.py` (MySQL and SQL Server profiling through SQLite stand-ins), `packages/keeper/tests/test_schema_inference.py` (uploads, named MySQL and SQL Server sources, `schema:preview`) and the CLI tests for imports. Parquet needs the `parquet` extra; without it the upload is refused with a 422 naming it.
- **HTTP JSON checks:** one page pinned to the checked address; a bare array as the records; header secrets must come from allowed `secret://` references and literal credentials are refused; cursor, page, offset and `Link` header pagination; a repeated cursor ends the read; a `next` link to another host is refused; more pages than `max_pages` fail the read instead of committing part of a snapshot; 429 and 5xx retried with backoff and `Retry-After`, with a limit; a slow server times out; private addresses refused even when listed, and allowed only by an explicit setting; scheme and host allowlists; responses over `max_bytes` refused; invalid JSON fails; incremental reads send and apply the watermark.
- **Found and fixed in the same change:** records without a header are checked against every key the run saw (a column that appears late), and file Sources open under the allowed roots without following links, the same guarded access as CSV ([security review](security-review.md), F18).

## Live model runs

**Date:** 2026-10-22. **Models:** `claude-haiku-5-5` through Anthropic's OpenAI-compatible
endpoint, `lfm2.5:8b` (8.5B mixture of experts, Q4_K_M, 16k context) on Ollama 0.32.1, and
`BAAI/bge-small-en-v1.5` (384 dimensions) with fastembed. Until then tier 2 and the embedding
jobs had only run against the deterministic fake provider in tests.

### Setup

Everything ran in process against a scratch Postgres 16 with pgvector (the integration test
harness: `LOREBASE_TEST_DATABASE_URL` pointing at a local `pgvector/pgvector:pg16`
container), on an Apple M5 Pro laptop. The `supply` Domain was seeded like
`packages/scribe/tests/integration/test_extraction.py` does: Suppliers, Parts, Plants and
Contracts without C-3025, no `supplied_by` or `stocked_at` Links. The five documents were the
four of `examples/supply/documents/` and `examples/five_day_demo/documents/contract-c-3099-*`,
each stored as an upload and extracted by its own job. The runs used throwaway tests built on
the fixtures of `packages/scribe/tests/integration/` and `packages/keeper/tests/integration/`
that are not committed; the committed opt-in test
`test_live_model_extracts_the_contract` (`LOREBASE_LIVE_LLM=1`, `LLM_BASE_URL`, `LLM_MODEL`)
runs tier 2 on one contract against a model of your choice.

Model settings, in a file outside the repository (the key and workspace id are file
references, never values):

```yaml
inference:
  llm:
    base_url: https://api.anthropic.com/v1/
    model: claude-haiku-5-5
    api_key: secret://file//path/to/anthropic.key
    headers: {anthropic-workspace-id: secret://file//path/to/anthropic.workspace}
    temperature: null
    json_schema_strict: true
    extra_body: {thinking: {type: disabled}}
    max_tokens: 4096
---
inference:
  llm:
    base_url: http://localhost:11434/v1
    model: lfm25-bench-16k:latest      # lfm2.5:8b with num_ctx 16384
    max_tokens: 4096
    extra_body: {reasoning_effort: none}
```

Extraction ran with `scribe.extraction.tiers: [0, 2]` (the rules alone for the baseline),
`auto_accept_threshold: 0.55`, schema discovery on (the default), tier 1 off. Embeddings ran
with the defaults (`fastembed`, `BAAI/bge-small-en-v1.5`), with
`search.embedding_template: "{name}. {summary} Category: {category}."` on Part, through
Scribe's `EmbeddingPass` and Keeper's `POST /search`.

What was measured:

- **Schema compliance:** answers that parse and validate against the answer schema on the
  first attempt, and after the repair call.
- **Facts proposed:** records (one per record, plus each non-key value) and links in the
  model's answers.
- **Evidence verified:** facts whose quote is found in the chunk. **Kept:** facts that also
  pass value grounding, type checks and entity resolution.
- **Items:** Proposal items of the five jobs, and how many carry tier 2 provenance (tier 2
  supersedes a tier 0 item for the same fact).
- **Tokens** as reported by the endpoint, **time** per job end to end.

### Extraction (tier 2)

| | Rules only (tier 0) | `claude-haiku-5-5` | `lfm2.5:8b`, no reasoning |
|---|---|---|---|
| Structured output mode | n/a | prompt only (strict schema refused, see below) | `json_schema` |
| Schema-valid answers, first attempt / after repair | n/a | 5 of 6 / 5 of 5 | 5 of 5 / 5 of 5 |
| Facts proposed by the model | n/a | 168 | 67 |
| Evidence verified | n/a | 98 % | 43 % |
| Kept after grounding and resolution | n/a | 80 % | 37 % |
| Proposal items (tier 2) | 17 (0) | 28 (22) | 17 (0) |
| Records referred to by key and resolved | n/a | 33 of 33 | 1 not resolved |
| Links proposed / not resolved | n/a | 30 / 10 | 3 / 3 |
| Tokens in / out, five jobs | 0 | 42,856 / 12,500 | 6,377 / 7,853 |
| Time per job | 0.1 to 0.2 s | 6.5 to 13.6 s | 5 to 37 s |
| Cost of the five jobs | 0 | 0.011 USD | 0 |

- **Haiku 5.5** added values the rules have no cue for (payment terms, Incoterm, currency,
  quantities) and restated most of what the rules find, with correct dates and numbers. The
  discarded facts were inferences the evidence check exists to stop: ISO country codes from
  "Poland", Part names copied from the candidate list, statuses inferred from dates. Its
  unresolved links point at purchase orders and Plants that the seeded Domain does not hold.
  One answer was not valid JSON at first and the repair call fixed it. Input tokens are high
  because in prompt-only mode the answer schema travels in the system prompt (about 5,000
  tokens per call).
- **LFM 2.5 8B** produced schema-valid JSON every time (Ollama constrains decoding with the
  schema), but most of its evidence was not in the text: it paraphrased, merged sentences or
  copied schema descriptions as quotes. Every such fact was discarded, so its jobs proposed
  exactly what the rules proposed. With reasoning on, a job took 23 to 62 s, and the
  answers were no better.
- The rules baseline found 17 items in 0.7 s for all five documents; tier 2 is worth its cost
  on prose only with a model that quotes faithfully.

### Schema suggestions

| | Rules only | `claude-haiku-5-5` | `lfm2.5:8b` |
|---|---|---|---|
| Suggestions over five documents | 8 | 19 | 8 |
| From the model | n/a | 12 (one shared with the rules) | 0 |

Haiku's suggestions were sensible Attributes of Contract and Supplier with correct quotes and
samples: `termination_notice_days` (two documents, sample 30), `renewal_notice_days` (90),
`defect_threshold_percent` (0.2), `replacement_lead_time_working_days` (15),
`minimum_order_quantity_for_unit_price` (200), `requested_price_increase`. `buyer` was merged
with the rules' observation into one suggestion with support 3. Weak points:

- Near duplicates are not merged when the model names the same fact differently in two
  documents: `renewal_notice_days` and `renewal_proposal_notice_days` (same sample, same
  sentence shape), `termination_notice_days` and `termination_for_cause_notice_days`.
  **Fixed** (see [after the runs](#fixed-after-the-runs)): both pairs now fold into one
  suggestion each.
- A Kind `Proposal` came from the sentence "open a Proposal to flag the supplier as high
  risk", and a Kind `Buyer` duplicates the Attribute `buyer`. **Fixed**: neither is
  suggested any more.

LFM's unlisted facts all failed evidence verification, so it added no suggestion.

### Embeddings and hybrid search

The embedding pass embedded the 40 Parts in one model call: 52 s on the first run, which
includes downloading the model, then 1.4 s. Top results for Kind Part, limit 5, Keeper in
process:

| Query | Lexical | Semantic and hybrid, top 3 |
|---|---|---|
| electric drive motor | none | Servo motor 11 kW, Gear motor 11 kW, Servo motor 0.75 kW |
| ring that stops oil leaking | none | O-ring NBR 70 30x3, O-ring FKM 90 18x2.5, O-ring 40x62x8 |
| keeps dust out of compressed air | none | Air filter cartridge 10 micron, Air filter cartridge H-360, Air filter cartridge 10 micron |
| measure heat | none | Temperature probe 0-250 bar, Temperature probe PT100 150 mm, Servo motor 0.75 kW |
| screw fastener | none | Hex bolt M16, Flat washer M10x60, Hex bolt M6x30 |
| filter 10 micron | 3 hits, Oil filter first | hybrid: Oil filter 10 micron, Air filter cartridge 10 micron (x2), then 25 micron filters |

The ranking is sound: every semantic top result is the right product family. Lexical search
needed every query word, so descriptive queries found nothing and hybrid search then equalled
the semantic ranking; on keyword queries hybrid keeps the exact matches first and fills up
with semantic neighbours. Search answered in 30 to 110 ms after the first query (415 ms).

**Fixed** (see [after the runs](#fixed-after-the-runs)): with ranked partial matching the
same queries find, lexically, top 3 for Kind Part, every hit `match: partial`:

| Query | Lexical before | Lexical after |
|---|---|---|
| electric drive motor | none | Gear motor 11 kW, Servo motor 11 kW, Induction motor 0.75 kW |
| ring that stops oil leaking | none | O-ring 40x62x8, O-ring FKM 90 18x2.5, O-ring NBR 70 30x3 |
| keeps dust out of compressed air | none | Air filter cartridge 10 micron, Air filter cartridge F-120, Air filter cartridge H-360 |
| measure heat | none | none (no word in common with the Parts) |
| screw fastener | none | none (no word in common with the Parts) |
| filter 10 micron | 3 hits, Oil filter first | unchanged, `match: all` |

Hybrid search now fuses these lexical lists with the semantic ones instead of returning the
semantic ranking alone; queries whose words the Parts do not hold still rank by the vector.

### Tokens and cost

All Anthropic calls of the validation, probes and the runs before the fixes included:
184,278 input and 44,152 output tokens, 0.04 USD at 0.10 / 0.50 USD per million tokens.
Requests the endpoint refused (HTTP 400) are not billed. Ollama and fastembed ran locally at
no cost.

### Problems found and fixed

| Problem | Effect | Fix |
|---|---|---|
| Anthropic refuses keys not scoped to a workspace without an `anthropic-workspace-id` header | every request failed with HTTP 400 | `inference.llm.headers` (and per task), values may be secret references |
| Haiku 5.5 refuses `temperature` | HTTP 400 on every call | `temperature: null` leaves the field out |
| The endpoint requires `response_format.json_schema.strict: true` | `json_schema` refused, two wasted requests per job | `json_schema_strict` setting; schemas above 24 optional properties still step down, as documented |
| Thinking (Haiku by default, LFM unless disabled) spends `max_tokens` before the answer | empty answers | `extra_body` passes `thinking` or `reasoning_effort` through; the error now says a reasoning model may have used the budget |
| One chunk with an invalid answer failed the whole extraction job | the tier 0 items of the document were lost too | the chunk is skipped with a warning and counted as `model_invalid_answers`; its tokens still count |
| Timestamps answered without an offset (`2024-01-23T00:00:00`) were discarded as invalid, and timestamps were grounded as text | `approved_at` never extracted | values without offset are read as UTC; timestamps are grounded by their day |

Each fix has regression tests with recorded answers (`packages/inference/tests/test_chat_endpoint.py`,
`packages/scribe/tests/test_extraction_rules.py`,
`packages/scribe/tests/integration/test_extraction.py`).

### Fixed after the runs

The three problems the runs left open, fixed on 2026-10-22. The model runs were not repeated
(they cost tokens and need the keys); each fix is checked by tests built on the names,
samples and answers recorded in the runs, and the lexical search table above was measured
again on the same `supply` data (Postgres 16, default `simple` text search configuration,
`search.partial` defaults).

| Problem | Before | After | Tests |
|---|---|---|---|
| Lexical search needs every query word | descriptive queries: no hit; hybrid equal to semantic | ranked partial matches after every full match (`search.partial`), `match: all\|partial` per hit, corrections per term, phrases and negations kept; 3 of 5 descriptive queries find the right family, keyword queries unchanged | `packages/store/tests/test_reads.py`, `test_overlay.py`, `packages/keeper/tests/integration/test_read_api.py` |
| Near-duplicate Attribute suggestions | `renewal_notice_days` + `renewal_proposal_notice_days`, `termination_notice_days` + `termination_for_cause_notice_days`: 4 suggestions | 2 suggestions, the variants kept as labels (`schema.suggestions.merge_similarity` 0.6; `start_date`/`end_date` stay apart) | `packages/core/tests/infer/test_suggestions.py`, `packages/store/tests/test_suggestion_fold.py` |
| Noise Kind suggestions | Kinds `Proposal` and `Buyer` listed | not suggested (`kind_stoplist`, a Kind named like an Attribute); a new Kind needs `kind_min_attributes` (2) Attribute suggestions or observations to be listed | the same, and `packages/scribe/tests/test_extraction_discovery.py` |
| Tier 2 links without required link Attributes | `stocked_at` proposed without `quantity`; the answer schema had no place for link values | the answer schema carries each Relation's link Attributes (required ones required) and the prompt asks for them; values are grounded like record values; a Link still missing one stays pending with `match.missing` | `packages/scribe/tests/test_extraction_link_values.py` |
| Structured output step-down per job | a strict-only endpoint refused `json_schema` again at the start of every job (two wasted requests each) | the working mode is remembered per `base_url`, model and output settings for the process | `packages/inference/tests/test_chat_endpoint.py` |

Search cost at 100k Records, before and after, is in
[performance](performance.md#partial-matching-100k-records).

### Agent-facing features with Haiku, Sonnet and Opus 5.5

**Date:** 2026-10-22. **Models:** `claude-haiku-5-5`, `claude-sonnet-5-5` and `claude-opus-5-5`
through the OpenAI-compatible endpoint. Three runs: Guide's `/explore` (the backend of the web
app's Explore panel), the [process agent](../examples/agent/README.md)
over Herald, and tier 2 extraction with schema discovery.

**Setup.** The quickstart stack (`make env`, `make up` with the `assistant` profile,
`make example-data`) under its own compose project and ports, with three settings in a
configuration file kept outside the repository: `keeper.auth.mode: api_key` (the administrator
key from `python -m lorebase_keeper bootstrap`, the agent's key on the service principal
`agent-risk` as in [day 5 of the demo](demo.md#day-5-agents)), `keeper.trace.enabled: true`, and
one `inference.llm.tasks` entry per model, switched with `guide.agent.task`. The key and the
workspace id were compose secrets read through `secret://file//run/secrets/...`; the agent read
them with `--api-key-file` and `--header-file`. Per-model request options:

| Model | `extra_body` | Price in / out per million tokens |
|-------|--------------|------------------------------------|
| `claude-haiku-5-5` | `{thinking: {type: disabled}}` | 0.10 / 0.50 USD |
| `claude-sonnet-5-5` | `{thinking: {type: between_tools}}` | 2 / 10 USD |
| `claude-opus-5-5` | `{reasoning_effort: low}` | 4 / 20 USD |

All three answer HTTP 400 to `temperature` ("deprecated for this model"); Sonnet refuses
`thinking: disabled` (it names `between_tools` instead) and Opus refuses any way of turning
thinking off.

**Ground truth.** Computed from `examples/supply/data` (the loaded sample) with a script, for
the five preset questions of the Explore panel and three multi-hop questions added for this run:

| Question | Expected answer |
|----------|-----------------|
| Single-source risk | 32 single-sourced Parts; the riskiest sole supplier is S-1025 (74, four Parts) |
| Tier-1 high risk with open orders | none: the four tier-1 suppliers score 16 to 52; only S-1003 (16) has an open order (PO-200016) |
| What if S-1007 stops | P-10025 loses its only supplier, P-10028 keeps one; PO-200043 (issued) to PL-11 is open, PO-200040 received; Parts stocked at PL-20 and PL-11; contract C-3007 |
| History of C-3013 | one change: created by the sample import |
| Who supplies "bering" parts | the six bearings come from S-1005, S-1021, S-1025, S-1015, S-1013 |
| Parts with Kavvik Filtration as only supplier, where stocked | P-10012 PL-17 1,700; P-10018 PL-10 3,520; P-10023 PL-16 4,590; P-10036 PL-03 1,420 (not P-10020, two suppliers) |
| Single-sourced Parts of suppliers on hold or terminated, their plants | P-10013 (S-1021) PL-02; P-10030 (S-1021) PL-05 and PL-16; P-10016 (S-1019) PL-11 |
| Kavvik Filtration's open orders, Parts and plants | PO-200031 (shipped) P-10018 to PL-10; PO-200037 (issued) P-10036 to PL-03 |

Each answer was scored from 0 to 1 as the share of the expected facts it states, 0 when it
claims something false (a multi-sourced Part as single-sourced, a closed order as open); every
score below 1 was read and checked by hand.

#### Explore (Guide)

Each question three times per model, Guide's default budgets (`max_steps` 8,
`max_total_tokens` 40000), `max_tokens_per_call` 2048. "Before" is Haiku on the code as it was;
the problems it showed are in [problems found and fixed](#problems-found-and-fixed-in-these-runs),
and the other columns ran after the fixes.

| Question | Haiku, before | Haiku | Sonnet | Opus |
|----------|--------------|-------|--------|------|
| Single-source risk | 0.19 | 0.52 | 0.74 | 0.52 |
| Tier-1 high risk with open orders | 0.00 | 1.00 | 0.67 | 1.00 |
| What if S-1007 stops | 0.40 | 0.60 | 0.87 | 0.73 |
| History of C-3013 | 0.67 | 1.00 | 1.00 | 1.00 |
| "bering" parts | 0.13 | 0.80 | 1.00 | 1.00 |
| Kavvik as only supplier, stock | 0.00 | 0.92 | 1.00 | 1.00 |
| On hold or terminated suppliers | 0.22 | 1.00 | 0.89 | 1.00 |
| Kavvik's open orders | 0.00 | 0.78 | 0.67 | 1.00 |
| **Mean score** | **0.20** | **0.83** | **0.85** | **0.91** |
| Fully correct runs | 2 of 24 | 15 of 24 | 18 of 24 | 19 of 24 |
| `Not found in the graph.` answers (besides `model` errors) | 13 | 0 | 0 | 0 |
| Runs ended by `error` `model` | 0 | 0 | 2 | 1 |
| Refused tool calls (all runs) | 53 | 9 | 1 | 0 |
| Steps per run | 2 to 8 | 2 to 8 | 1 to 8 | 1 to 8 |
| Tokens per run, in / out (mean) | 26,962 / 1,559 | 22,674 / 1,289 | 23,848 / 1,013 | 19,584 / 1,191 |
| Median time per run | 15.1 s | 11.0 s | 15.7 s | 19.7 s |
| Cost per run | 0.0035 USD | 0.0029 USD | 0.058 USD | 0.10 USD |

- **Variance.** The three runs of a question differed on 5 of 8 questions for Haiku and
  Sonnet and on 2 for Opus (mean spread between best and worst run 0.28, 0.41 and 0.18). The
  single-source question stays partial for every model: the query returns 20 Records a page
  (`max_items`) and the models report the first page with the riskiest supplier rather than
  all 32 Parts.
- **Errors.** Where Haiku lost points it stopped short and said so (an unchecked plant,
  contract or supplier); the checks for false claims found none after the fixes. Sonnet and Opus mostly lost runs to the three `model`
  errors: an answer that did not parse after the repair call. At `max_tokens_per_call` 4096 a
  further run of every question with Sonnet (mean 0.93) and Opus (0.95) had none, which
  points at answers cut off by the completion cap (Opus thinks within it).
- **Trace.** Every run was one session in the agent trace: each Keeper read Guide made appeared
  as a step with its `guide.<tool>` name, route and status, refused queries included (422),
  and each reasoning note as a note step; calls that Guide refused on its own argument check
  never reach Keeper and have no step, as designed. The one hybrid search made two reads.
- **Before the fixes**, 53 tool calls were refused, almost all queries written in the shape
  the `query` tool's description gave (`{"single_sourced": true}`, `{"attr": "tier", "eq":
  "1"}`), and 13 runs answered `Not found in the graph.`, most of them after reading part of
  what they needed, when the step budget ran out. The description fix alone raised Haiku's mean
  to 0.68; the budget, prompt and Link Attribute fixes to 0.83.

#### Process agent

`examples/agent/agent.py` over Herald (stdio) with the agent's key, `--temperature none` and
the `extra_body` above. Tasks: `single-source-risk` (read) three times per model with the
default bounds, once more with `--max-total-tokens 150000`; `what-if S-1007` once (150000);
`flag-critical` once; and the same flagging rule at a risk score of 75 or more (`--prompt` with
`--allow-operation flag_supplier_risk`), since no supplier of the sample reaches 80.

| | Haiku | Sonnet | Opus |
|---|---|---|---|
| `single-source-risk`, correct "no Part qualifies" (no contract ends within 90 days of 2026-10-22) | 1 of 3 (2 hedged); 1 of 1 at 150k | 3 of 3; 1 of 1 | 3 of 3; 1 of 1 |
| Runs ended by the token bound (60000) | 2 of 3 | 3 of 3 | 3 of 3 |
| `what-if S-1007`, expected facts | 4 of 5 (no contract) | 4 of 5 (no contract) | 5 of 5 |
| `flag-critical`: no write, none qualifies | yes | yes | yes |
| Flag at 75: one Proposal for S-1020, `main` unchanged | yes (one refused dry run first) | yes | yes |
| Tokens per run (mean, all runs) | 78,800 | 74,600 | 72,900 |
| Time per run | 15 to 30 s | 16 to 37 s | 16 to 45 s |
| Cost per run (output share estimated at 10 %) | 0.011 USD | 0.21 USD | 0.41 USD |

- Every write went through `lorebase_execute` with a task: the Proposal came from branch
  `agent/agent-risk/custom` to `main`, author `agent-risk`, one RiskFlag of severity
  `critical` linked to S-1020 with a reason made of the facts read (score 77, two `high`
  flags, status active). `main` kept its revision and its 20 RiskFlags after every run. The
  agent's trace session showed each Herald call, the notes, and the Proposal id on the
  committing `execute` step.
- The agent first sent `temperature: 0`, which every model refused: fixed below.
- Each step resends the conversation and Herald's tool catalogue (13 tools), so five or six
  steps used 45,000 to 90,000 tokens; with the default bound of 60000 most runs answered from
  what they had read when the bound stopped them, correctly but without the risk detail.
  With 150000 every run finished on its own in 4 to 7 steps.
- Both summaries reported the RiskFlag key minted by the dry run, which differs from the
  committed one: the agent cannot know the committed key unless it reads the Proposal.

#### Extraction (tier 2) and schema discovery

The setup of [the first runs](#setup): five documents, tiers `[0, 2]`, `auto_accept_threshold`
0.55, discovery on, `max_tokens` 4096 (8192 for Opus). Haiku ran again on the current code for
a like-for-like comparison.

| | Rules only | Haiku | Sonnet | Opus |
|---|---|---|---|---|
| Structured output mode | n/a | prompt only | prompt only | prompt only |
| Facts proposed by the model | n/a | 125 | 103 | 137 |
| Kept after grounding and resolution | n/a | 81 % | 83 % | 83 % |
| Proposal items (tier 2) | 17 (0) | 27 (13) | 25 (13) | 25 (18) |
| Wrong values among tier 2 items | n/a | 1 | 0 | 0 |
| Links without a required value (pending) | n/a | 1 | 0 | 0 |
| Schema suggestions (from the model) | 8 | 19 (12) | 13 (5) | 18 (10) |
| Tokens in / out, five jobs | 0 | 47,511 / 12,038 | 47,145 / 9,077 | 37,014 / 12,039 |
| Time per job | 0.4 to 1.8 s | 7.8 to 14.8 s | 7.1 to 22.0 s | 18.3 to 23.9 s |
| Cost of the five jobs | 0 | 0.011 USD | 0.19 USD | 0.39 USD |

- Every discarded fact failed value grounding or resolution (unresolved Links to purchase
  orders and plants the seeded Domain lacks); none failed the evidence check.
- **Haiku** proposed the most facts but took "agreed unit price 16.30 PLN" of the C-3013
  amendment as a new `unit_cost` of P-10033 (a standard cost in euro), and proposed one
  `stocked_at` Link without its quantity.
- **Sonnet** proposed the fewest facts, all checked items correct; one suggestion,
  `critical_risk_flag` on Supplier, came from the sentence asking Procurement to raise a flag.
- **Opus** found the most Links (also those of the review's sole-source table) and the best
  suggestions: `renewal_notice_days` and `termination_notice_days` with support 2, a Kind
  `Amendment` from two documents, `price_increase_cap` (4 percent), `on_time_delivery_rate`.
- The schema with free-form `args` (Guide) and the extraction schemas both stepped down from
  `json_schema` to prompt-only mode on every model, as described for Haiku above.

#### Recommended settings for hosted setups

The shipped defaults stay on the local Ollama model; these are the settings to use with the
Anthropic endpoint ([inference providers](inference-providers.md#which-model-for-which-task)).

| Task | Model | Settings |
|------|-------|----------|
| Explore (Guide) | `claude-haiku-5-5`; `claude-opus-5-5` where completeness matters more than cost | default budgets; `max_tokens_per_call` 2048 (4096 for Sonnet or Opus) |
| Process agent, reads | `claude-haiku-5-5` | `--max-total-tokens 150000` |
| Process agent, Operations | `claude-sonnet-5-5` | the same; writes still go through Proposals |
| Extraction (tier 2) | `claude-haiku-5-5`; `claude-sonnet-5-5` where a wrong value costs more than the tokens | `max_tokens` 4096 |
| Schema suggestions of a new document set | `claude-opus-5-5` on the extraction task, for a one-off pass | `max_tokens` 8192; the dedicated discovery pass (`discovery.llm_pass`) was not measured |

#### Problems found and fixed in these runs

| Problem | Effect | Fix | Tests |
|---|---|---|---|
| Guide's `query` description gave `where` as `{attr: value}` and `order_by` as `{attr: asc\|desc}` | most refused tool calls; runs out of steps | the DSL's predicate forms and a valid example; bounds stated for `history`, `neighborhood`, `traverse` | `packages/guide/tests/test_tool_catalogue.py` |
| Only the step budget left a turn to answer | `Not found in the graph.` after useful reads when tokens ran out first | the last turn comes when the budget no longer covers a step, the next result and an answer | `packages/guide/tests/test_explore.py` |
| The input estimate assumed 4 characters per token; JSON results measured 2.2 | the final call passed the estimate, the reported total went over, the answer was dropped | the ratio is taken from the endpoint's count of the previous call; the final answer is kept | `test_explore.py` |
| The tokens of an answer that did not parse were not counted | `done.tokens` and the budget missed those calls | counted; the log line names the schema problems | `test_explore.py` |
| `neighborhood` and `traverse` dropped Link Attributes | "the graph does not record stock quantities" | edges carry their values | `test_tool_catalogue.py` |
| The final-turn instruction asked for `found: false` when not enough; checked negatives were read as not found | empty answers for "none qualifies" | partial answers with citations; a checked negative result is an answer | `test_tool_catalogue.py` |
| The process agent always sent `temperature: 0` | HTTP 400 from every Claude model | `--temperature none`, `--extra-body` | `examples/agent/test_agent.py` |
| Herald logged each Keeper URL at INFO through httpx | Record keys in the agent's standard error | the HTTP client's logger kept at WARNING, as in Guide | `packages/herald/tests/test_safety.py` |

#### Tokens and cost of these runs

| Run | Haiku | Sonnet | Opus |
|-----|-------|--------|------|
| Explore (every pass, including the ones before and between the fixes) | 2,418,358 / 136,886 tokens, 0.31 USD | 1,559,224 / 67,536, 3.79 USD | 630,110 / 38,382, 3.29 USD |
| Process agent (output share estimated at 10 %) | 551,755 tokens, 0.08 USD | 596,977, 1.67 USD | 509,960, 2.86 USD |
| Extraction | 59,549 tokens, 0.01 USD | 56,222, 0.19 USD | 49,053, 0.39 USD |
| Probes (parameters, tokenizer ratio, a failure reproduction) | 0.00 USD | 0.15 USD | 0.10 USD |

About 12.8 USD in all, at the prices above; refused requests (HTTP 400) are not billed.

## Fresh-clone quickstart check

**Date:** 2026-10-22. **Environment:** Apple silicon laptop, macOS, Docker Desktop with Docker
29.5.3 and compose 5.1.4 (15 CPUs, 7.7 GB of memory for Docker), uv 0.11, Node 24.15, pnpm
11.13, Ollama on the host for one of the model options. Other compose stacks were running on
the same machine, one of them holding `127.0.0.1:5432`.

**Goal:** someone who has never seen the project goes from zero to a working Lorebase with the
documentation alone: stack up, example data, web app, a first query, an agent over MCP, a real
model, cleanup.

### Method

Two passes, each from a fresh copy of the repository in an empty directory, with its own compose
project name and ports (`COMPOSE_PROJECT_NAME`, `POSTGRES_HOST_PORT`, `KEEPER_HOST_PORT`), as
the quickstart now documents.

1. **Before the fixes**, a `git clone` of `main` at `c032815`, following README.md and
   [quickstart.md](quickstart.md) as they were, word for word: `uv sync`, the core demo, `.env`,
   compose, the CLI loading steps, the web app in live mode through the dev server proxy, Herald
   over stdio (with a small MCP client), Ollama in compose and on the host, document extraction,
   cleanup. Every friction point was written down.
2. **After the fixes**, a copy of the fixed tree, following the new
   [quickstart](quickstart.md) step by step: `make env`, `make up`, `make example-data`, the first
   query, the web app, the Herald check script, the three model options (Ollama in compose,
   Ollama on the host, a hosted endpoint through the key file overlay, with a placeholder key
   against the host Ollama), an extraction, `make down` and `make clean-stack`. The times are
   in the [quickstart](quickstart.md#timings).

All containers, volumes, images built or pulled for the check and the copies of the repository
were removed afterwards.

### Issues found and fixed

| # | Friction on the first pass | Fix |
|---|----------------------------|-----|
| 1 | `docker compose up` failed with `Bind for 127.0.0.1:5432 failed: port is already allocated`; the docs never named the port variables | Ports table in the prerequisites, comments in `.env.example`, a troubleshooting entry |
| 2 | The compose project name is fixed to `lorebase`: a second checkout reuses or replaces the containers and volumes of the first | `COMPOSE_PROJECT_NAME` in `.env.example`, documented |
| 3 | The image tag `lorebase:dev` is shared by every checkout; `up -d` without `--build` started an image built from another checkout | `make up` and the documented command pass `--build` |
| 4 | `curl /readyz` right after `up -d` answered `Empty reply from server`: nothing said to wait | `--wait` in `make up` and the documented command |
| 5 | Editing `.env` by hand for two generated passwords | `make env` (`scripts/quickstart.sh env`), tested |
| 6 | Loading the sample took 12 commands, and left the data on branch `work`: the web app opened on an empty `main` ("This Branch has no Kinds yet"), Herald answered from the empty `main`, and extraction resolves against `main`. Merging needed an approval from a second principal, which a stack without authentication does not have | `make example-data` creates the Domain with `required_approvals: 0`, loads `work` and merges a Proposal into `main`; idempotent |
| 7 | No word on credentials or sign-in for the web app | "Credentials" paragraph in step 2.3 and in 2.6 |
| 8 | No path from the quickstart to an MCP host; the MCP page assumes an API key | Section 3: `mcpServers` entry without a key for the local stack, a check script, the key as `secret://file` once authentication is on |
| 9 | `docker compose --profile local-models run --rm inference-check` (inference-providers.md, compose comment) fails from the repository root: no `--env-file`, no `-f` | Full command in the docs and the compose file comment |
| 10 | The Ollama container publishes `127.0.0.1:11434`, the port of an Ollama already on the host; the image is 7 GB and runs on the CPU, nothing said so | `OLLAMA_HOST_PORT` documented; sizes and the host Ollama option with `--no-deps` |
| 11 | A model server on the host was not reachable from Scribe on Linux | `extra_hosts: host.docker.internal:host-gateway` on Scribe and `inference-check` (checked by `docker compose config` in the tooling test; Linux itself not run) |
| 12 | A hosted key could only go into `.env` as a literal: compose mounts no key file | `deploy/compose/hosted-model.yaml` mounts `secrets/llm_api_key` as a compose secret, read through `secret://file`; tested |
| 13 | `ollama-pull` prints nothing during the download | The docs point at `logs -f ollama-pull` |
| 14 | Extraction with `qwen3:4b` (a reasoning model) failed after 8 minutes with `ReadTimeout` | Model advice and a troubleshooting entry |
| 15 | The sample documents of `examples/supply/documents` yield no items once the sample is loaded, so "upload a document" showed nothing | The quickstart uses `examples/five_day_demo/documents/contract-c-3099-*`, which opens a Proposal with two items |
| 16 | "Coming next" listed Source syncs and the lineage API as missing, both done | Points at the roadmap |
| 17 | No cleanup of profile containers and volumes | `make down`, `make clean-stack` (`--profile "*" down -v`) |

### Verification

- Second pass: every step above succeeded as written. Timings: [quickstart](quickstart.md#timings).
- `tests/tooling/test_quickstart.py` (environment file generation, usage, the hosted model
  overlay and `extra_hosts` rendered by `docker compose config`) and
  `tests/tooling/test_compose_stack.py`: 18 passed.
- `make docs` (strict) passed.

### Web app in compose (follow-up pass)

**Date:** 2026-10-22. The web app was the one step that needed Node.js and pnpm: the web image
would have needed Keeper's CORS setting. The compose file now has a `web` service (the web
image with an nginx proxy of `/api/`, and `/guide/` for the `assistant` profile, rendered from
`deploy/compose/web/proxy.conf.template`), part of `make up`, on `127.0.0.1:8090`.

Checked from a fresh clone of the pushed commit, with its own project name and ports
(`COMPOSE_PROJECT_NAME`, `POSTGRES_HOST_PORT`, `KEEPER_HOST_PORT`, `LOREBASE_WEB_PORT`), no Node.js
involved: `make env`, `make up`, `make example-data`, then `curl` against the web port.

| Step | Time (warm Docker cache) | Result |
|------|--------------------------|--------|
| `uv sync --all-packages` | 2 s | |
| `make up` (backend and web images, Postgres, migrations, Keeper and web healthy) | 38 s | prints `Web app: http://127.0.0.1:PORT` |
| `make example-data` | 5 s | Domain `supply` loaded |
| `GET /` | | 200, the app shell |
| `GET /config.js` | | `VITE_API_MODE=live`, `VITE_KEEPER_BASE_URL=/` |
| CSP header | | `connect-src 'self'` |
| `GET /api/v1/auth/whoami`, `/api/v1/domains/supply/branches` | | 200 through the proxy, Keeper unchanged (no CORS setting) |
| `GET /api/v1/domains/supply/branches/main/events` (SSE) | | first frame at once, heartbeat after 15 s as Keeper sends it: not buffered |
| `GET /supply/explorer` | | 200, the app shell (client-side route) |
| `GET /guide/healthz` without the profile | | 502, as expected |
| `GET /guide/healthz` with `--profile assistant` | | 200; a `POST /guide/explore` with the web origin passes the origin check (401 without credentials), another origin gets 403 |

The first build of the web image adds a `pnpm install` and a Vite build to `make up`. Tests:
`tests/tooling/test_compose_stack.py` and `tests/tooling/test_quickstart.py` (the `web` service,
port and bind variables, the proxy template, Guide's allowed origins). Containers, volumes, the
two images and the clone were removed afterwards.

**Session sign-in through the web service** (2026-10-22). The overlay
`deploy/compose/oidc-mock.yaml` (Keeper in `auth.mode: oidc` with `auth.sessions`, the web app in
session mode, `ghcr.io/navikt/mock-oauth2-server:2.1.10` in Keeper's network namespace) was
started with its own project name and ports (`up -d --build --wait postgres migrate keeper web
oidc-mock`), then one sign-in was driven with `curl` and a cookie jar against the web port:

| Step | Result |
|------|--------|
| `GET /config.js` | `VITE_AUTH_MODE=session` |
| `GET /api/v1/auth/session` before sign-in | 401 |
| `GET /api/v1/auth/login?return_to=/supply/explorer` | 302 to the provider's `authorize` (code flow, PKCE S256, the registered callback) |
| `POST` of the provider's sign-in form (user name and `name`, `email` claims), redirects followed | callback through the proxy, then 302 to `/supply/explorer` on the app origin, 200 |
| `GET /api/v1/auth/session` | 200: a new `usr_` principal (provisioned), display name from the `name` claim, CSRF token |
| `GET /api/v1/auth/whoami`, `/api/v1/domains` with the cookie | 200, `method: session` |
| `POST /api/v1/auth/logout` without the CSRF header, then with it | 403, then 200 |
| `GET /api/v1/auth/session` after sign-out | 401 |

No code change was needed. `tests/tooling/test_compose_stack.py` renders the overlay. Containers,
volumes, network and the three images (backend, web, provider) were removed afterwards.

### Open

- A first machine downloads the base images during `make up`; that time was not measured, the
  build cache being warm.
- The MCP host itself was not driven; the `mcpServers` entry follows [mcp.md](mcp.md) and the
  same command was checked with the MCP client of the Python SDK.
