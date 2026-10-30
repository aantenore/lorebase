# Search

`POST /domains/{d}/search` runs lexical, semantic or hybrid search over the materialized head
of a branch (or a snapshot of a Revision). How each mode works is in
[design 8.4](design.md#84-search); the budgets and measurements are in
[performance](performance.md). This page covers the choice of ranking for lexical search.

## Resolving words before searching

To learn which Kinds, Attributes and stored values the words of a question stand for, use
`POST /domains/{d}/resolve` instead of search: it returns ranked candidates with ready-to-use
predicates from names, schema aliases and the value index (exact, then similar spelling; the
values of the nearest Records only with a `query_vector` and when nothing else matched), and it
runs no query ([design 8.9](design.md#89-resolution-words-to-schema-and-values),
[agents](agents.md#resolve-words-before-querying)). Search then finds Records by their text.

## Traits, lenses and filtered vector search

Beside `kinds`, `POST /search` takes `traits` and `lens` ([design 8.8](design.md#88-read-presets-and-filtered-vector-search)):

- `traits: ["Located"]` searches the Kinds that implement the Trait (`Supplier` and `Plant`)
  that the caller may read; hits keep their concrete `kind`. `kinds` and `traits` together
  search the union. A Trait the caller cannot see is an unknown Trait (422 `unknown_trait`).
- `lens: "supplier_risk"` searches the Kinds and Traits of that [lens](agents.md#lenses-and-traits)
  when the request names neither `kinds` nor `traits`. An unknown lens is 422 `lens_unknown`.
- A lens and a Trait never widen a search: grants and `where` apply as to the same request
  written out, and `where` still needs exactly one Kind among those searched.
- CLI: `lorebase search TEXT --trait Located --lens supplier_risk`. Web app: the Explorer's
  Kind filter lists the Traits, and a lens selector appears beside it (and in the Retrieval lab,
  for both searches and the context pack) when the schema declares lenses.

**Filters before the limit.** The semantic list (semantic and hybrid search) keeps the Domain,
branch, readable Kinds, `kinds`, `traits`, `lens` and `where` in the same statement and before
its `LIMIT`, so it never holds a Record the caller may not read. An approximate HNSW index
returns its nearest neighbours first and applies the filters after, so a selective filter could
leave fewer than `search.candidates` rows, or none. Keeper handles that in one of two ways:

| Case | What runs |
|------|-----------|
| pgvector 0.8 or later and `search.semantic.iterative_scan: auto` (default) | `SET LOCAL hnsw.iterative_scan = relaxed_order` and `hnsw.max_scan_tuples = search.semantic.max_scan_tuples` (default 20000) inside the read transaction; the index scan goes on until the filters leave enough rows. The nearest vectors of the searched Kinds (`search.candidates * search.semantic.overfetch_factor`) are a materialized subquery over `embeddings` alone, joined to the Records and ordered again by exact distance, so the relaxed order never reaches the response; with a `where` the Records are joined first, so the conditions come before the limit |
| older pgvector, or `iterative_scan: off` | the index subquery fetches `search.candidates * search.semantic.overfetch_factor` (default 4) nearest rows; the outer query filters and keeps `search.candidates` |

In both cases the read transaction raises `hnsw.ef_search` to `search.candidates *
search.semantic.overfetch_factor` (800 by default; pgvector caps it at 1000): without iterative
scans an HNSW scan ends after `ef_search` rows whatever its `LIMIT`, and under them each pass
returns at most that many candidates, so recall follows it either way.

**The semantic list is approximate.** The HNSW index returns its nearest neighbours, not the
exact top-k. Measured on 300,000 synthetic clustered vectors (384 dimensions, limit 20, default
`search` settings; [performance](performance.md#filtered-semantic-search)), mean recall@20
against the exact neighbours:

| Search | ef_construction 64 (pgvector default; indexes built before this release) | ef_construction 128 (`store.storage.hnsw_ef_construction` default) |
|---|---|---|
| no filter, iterative scan | 0.910 | 0.988 |
| 40% filter, iterative scan | 0.964 | 0.989 |
| 40% filter, over-fetch | 0.917 | 0.986 |
| one Kind of 1% of the vectors | 1.000 | 1.000 |

At 100,000 vectors ef_construction 64 gives 0.968 to 0.981, 128 gives 0.996 to 0.998. A Kind holding a small share of the
vectors is exact: the planner reads its vectors and sorts them. A request with `where` under
iterative scans joins the Records first and the planner normally sorts the matching Records'
vectors by exact distance (the exact path); it reads the HNSW index there only when it estimates
that cheaper. The build parameters (`store.storage.hnsw_m`, `hnsw_ef_construction`) apply to indexes built
from then on; an existing index keeps its own until rebuilt
([operations](operations.md#rebuilding-the-embedding-index)).

The pgvector version is read once per process (`pg_extension.extversion`) and `/readyz` reports
it in the `vector_search` check, with the case in use. When the list is still shorter than
`search.candidates` because the scan bound was reached while more Records match the filters,
the response says `semantic_truncated: true` (`truncated` in Herald): narrow the Kinds or the
filter, or raise `max_scan_tuples` or `overfetch_factor`. A list short because fewer Records
match is not flagged. The check behind the flag counts at most `search.candidates` matches
without the index, only for a short list.

## Default ranking

Lexical search matches `search @@ websearch_to_tsquery(q)` on the GIN index of the search
vectors (title at weight A, searchable Attributes at their weights, sensitive values left out)
and ranks with `ts_rank_cd`, with typo corrections from the word lexicon and partial matching
of descriptive queries. Ranking reads each candidate's search vector, so a query whose every
term is very common ranks a bounded sample (`search.common_term_*`) and the response says
`truncated_candidates`. On a laptop this keeps rare-term searches within budget at 10M Records,
while searches of very common words stay over it
([search on very common terms](performance.md#search-on-very-common-terms)).

This ranking needs nothing beyond Postgres 16 with pgvector, `pg_trgm` and `btree_gin`, and it
is what every install runs unless configured otherwise.

## BM25 ranking

`search.backend: bm25` ranks lexical search with a BM25 index of
[ParadeDB `pg_search`](https://github.com/paradedb/paradedb) instead: a top-k scan of the
index returns the best matches by BM25 score without reading every match, so a word in 2.7
million Records costs about what a rare word costs, and every match is ranked (no
`truncated_candidates` for ordinary queries). It is optional and off by default.

### Licence

`pg_search` (ParadeDB community edition) is licensed under the **GNU Affero General Public
License v3.0 (AGPL-3.0)**. Lorebase is Apache-2.0 and never ships, links or loads it: the
default images, the compose stack, the Helm chart defaults and the main CI job run
`pgvector/pgvector:pg16`, and the store code only names the extension's SQL types and operator
inside statements it sends when an administrator has installed it. Turning BM25 on means
running a Postgres server that includes an AGPL-3.0 component:

- Using the ParadeDB image as your database server, unmodified, is the common case. If you
  modify `pg_search` and let users interact with that modified server over a network, the AGPL
  requires you to offer them its corresponding source.
- Redistributing the image or the extension (for example inside your own appliance or
  product image) carries the AGPL obligations for that component, including its licence text
  and source offer. No Lorebase artifact (package, image, chart) contains it, so Lorebase's own
  licence and notices do not cover it.
- The extension runs inside the Postgres server process; Lorebase talks to it over the
  Postgres protocol only, as a client of the database.
- Your organisation's policy on AGPL components applies; check it, and the licence terms of
  the ParadeDB release you run, before you enable the backend. ParadeDB sells other licences;
  this project makes no statement about them.

Nothing in this page is legal advice.

### What it needs

- A Postgres 16 server with `pg_search` in `shared_preload_libraries` and pgvector, for example
  the ParadeDB image `paradedb/paradedb:0.26.3-pg16` (Postgres 16.15, `pg_search` 0.26.3,
  pgvector 0.8.6), pinned by digest in the files below. Other `pg_search` versions are untested.
- Migration `0037` (any server: it creates functions only).
- `store.storage.bm25_index: true` and a run of `python -m lorebase_store.storage apply` as the
  maintenance role: it creates the extension (`CREATE EXTENSION pg_search CASCADE`) and builds
  the index `ix_mat_records_bm25`, partition by partition with `CREATE INDEX CONCURRENTLY`,
  then attached to an index on the parent. Writes go on meanwhile; reads keep the default
  ranking until the index is valid. Without the extension the step is skipped and says why.
- `search.backend: bm25` on every Keeper.

Set up with the compose overlay `deploy/compose/search-bm25.yaml`
([deployment](deployment.md#bm25-search-overlay)) or the Helm values file
`ci/search-bm25-values.yaml` ([kubernetes](kubernetes.md#bm25-search)). With an external
Postgres, preload `pg_search` there, set the two keys and run `storage apply`.

### Behaviour

- **Same matches.** The index holds, per Record row, the lexemes of the existing search vector
  (`lb_search_terms(search)`, whitespace-tokenized, each repeated at its positions): the text
  search configuration, its stemming and stop words, the searchable Attributes and the
  exclusion of sensitive values are the ones of the default ranking, and a query matches the
  same Records. Typo corrections (from the word lexicon), partial matching, the Kind filter of
  the caller's grants and snippets work as before.
- **Other scores.** Hits are ordered by BM25 score (term frequency, inverse document
  frequency, length normalization); ties by Kind and key, bytewise. `score` is relative to the
  best hit of the statement (1.0 for the first full match), not the raw BM25 score, and the
  title's weight A is not applied: a word in the title counts like a word in an Attribute.
  Corrected and partial hits still rank after the hits of the query as written.
- **Statistics across Domains.** BM25 statistics are those of the whole index: every Domain,
  branch and Kind of the database. Raw scores would tell a caller how common a word is in text
  it cannot read, so the store returns relative scores: for a one-word query they no longer
  depend on other Domains. The order of the hits of a query of several words, and their
  relative scores, still do: which word weighs more follows how rare each is across the whole
  database. Where Domains hold the data of parties that must not learn anything about each
  other, keep the default ranking or give them separate databases.
- **Queries.** Words, `or` and negation (`-word`) translate exactly to the index's query
  language. A quoted phrase matches every word of it, the rows holding the words in sequence
  ranked first, and the best rows by score are checked again against the search vector
  (positions and distances as Postgres reads them): four times the page (at least 64) first,
  then, when that may have left out a better match, the first `search.common_term_candidates`;
  the result says `truncated_candidates` when even those may have. Queries the store
  cannot translate (nesting deeper than 32 levels, more than 256 alternatives) use the default
  ranking.
- **Overlays.** On an overlay branch the overlay's own rows and the base's rows as of the fork
  are scanned apart; the base's scan reads as many more rows as the overlay holds of the
  searched Kinds, at most `search.common_term_candidates` more, so the result stays exact for
  overlays of that size and says `truncated_candidates` when a larger one may have shadowed a
  match. That is two scans per query (four with typo corrections), so on overlays BM25 is
  slower than the default ranking ([performance](performance.md#bm25-search)).
- **Not affected.** Filtered searches (`where`), semantic and hybrid search keep `ts_rank_cd`
  (they are compiled in Keeper over the bounded candidate lists), and `store.reads.
  use_definer_functions: false` keeps the default ranking: BM25 runs through the security
  definer functions `lb_search_bm25` and `lb_search_bm25_overlay` only, which check the
  Domain against the transaction scope and accept only the query shapes the store builds.

### Operations

- **Readiness.** With `search.backend: bm25`, `/readyz` reports `search_backend`: `ok`
  (`bm25`) or `degraded` with the reason (extension missing or not preloaded, index missing or
  not valid yet, `store.reads.use_definer_functions: false`), and Keeper logs a warning at
  start. Degraded never takes the service out of rotation: lexical search then ranks the
  default way. Each process looks the index up again every 60 s per database server; a BM25
  statement that fails in between (the index dropped meanwhile) falls back to the default
  ranking for that search and logs a warning.
- **Writes and `VACUUM`.** The index is maintained in the writing transaction, like the GIN
  index, at no measurable commit cost at 1M Records. It keeps the versions of updated rows
  until `VACUUM` marks them deleted, and scans slow down meanwhile (a rare word from 3 to about
  15 ms at 1M Records): where Records change often, let autovacuum visit the `mat_records`
  partitions sooner (`autovacuum_vacuum_scale_factor` 0.01, for example;
  [performance](performance.md#bm25-search)). `pg_search` merges its segments in the
  background.
- **Turning it off.** Set `search.backend: postgres` (immediate), then
  `store.storage.bm25_index: false` and `storage apply` to drop the index (a short exclusive
  lock, waiting at most `store.storage.lock_timeout_ms`). The extension stays installed until
  you drop it. Moving back to the default image works once the index and the extension are
  gone.
- **Replicas.** A streaming replica on the same image that preloads `pg_search` serves BM25
  search too (the compose overlay sets up the standby of profile `scale-out` that way); reads
  served by a replica without it rank the default way.
- **Privileges.** `storage apply` refuses to build the index while a role other than the
  owner may create objects in the extension's schemas (`pdb`, `paradedb`): the definer
  functions call functions of those schemas as their owner.
- **Images and volumes.** The ParadeDB image runs on a newer Debian (C library) than
  `pgvector/pgvector:pg16`. Postgres warns about a collation version mismatch when it opens a
  data directory the other image initialized, because text indexes depend on the C library's
  sort order. Start the ParadeDB image on a new volume and move the data with `pg_dump` and
  `pg_restore`, or check every text index first (`amcheck`'s `bt_index_check`) and then run
  `ALTER DATABASE ... REFRESH COLLATION VERSION`.
