# Operations

Routine maintenance of the store: garbage collection, and backup and restore of a Domain. The
commands read the database and `store.*` settings from the Lorebase configuration
(`LOREBASE_CONFIG`, `LOREBASE__DATABASE__DSN`, see `config/lorebase.example.yaml`).

## Garbage collection

Objects, tree nodes and Revisions are immutable, so deleting a branch only removes its ref (and
its materialized rows, in the background for a large branch: see [Large branches](#large-branches)).
Collection reclaims what no ref reaches any more (design 6.13). Per Domain, it marks every
Revision reachable from a branch or tag, from an open Proposal's merge attempts, from the
last-written state of a Source (`source_written`, [Scribe](scribe.md#corrections-that-stick)),
or created less than `store.gc.grace_seconds` ago (default one hour), then the tree nodes and objects those
Revisions reach. It sweeps the rest in batches: unreachable Revisions with their parents,
changesets and history rows, provenance entries nothing refers to any more, tree nodes and
objects, and the embeddings (vectors of any model) whose Record content no branch head holds any
more, in batches of `store.gc.sweep_batch_rows` after the guard of each batch has checked them
against the materialized rows again. Branch deletion is the usual source of garbage; ref log entries stay and may name
Revisions that were collected.

```
python -m lorebase_store.gc --domain supply --dry-run     # counts and bytes, deletes nothing
python -m lorebase_store.gc --domain supply               # collect
python -m lorebase_store.gc --all --grace-seconds 86400   # every Domain, longer grace
python -m lorebase_store.gc --domain supply --enqueue     # queue a `gc` job instead
python -m lorebase_store.jobs work                        # run queued jobs (SKIP LOCKED)
python -m lorebase_store.indexes plan --domain supply     # pending per-Attribute index DDL
```

`jobs work` runs `gc`, `store.index`, `store.materialize` (the background copy of a large copy
branch, or of an overlay branch whose base moved too far), `store.branch_delete` (the rows of
a large deleted branch), `store.sensitivity_refresh` (the titles and search vectors a branch
stored before the default branch tagged an Attribute) and `store.lineage_backfill` (the
lineage revisions of rows materialized before migration `0032`; a starting worker queues the
sweep once, and `python -m lorebase_store.jobs lineage-backfill [--domain D --branch B]
[--recompute]` queues it by hand, `--recompute` also correcting rows of merges made before
merges attributed only the values they change, and the history, Records and Links of
earlier fast-forwards) jobs by default (`--types` narrows it; see [Large
branches](#large-branches)). A fast-forward's history is written one Revision at a time only
for ranges of at most `store.materialize.fast_forward_history_max_revisions` (1000)
Revisions of the source head's first-parent chain, by the merge and by `--recompute` alike;
a longer range, or one reached through a second parent, keeps one history row per element at
the source head, so Record history and the lineage change count show fewer changes there than
a rebuild. Raise the limit to cover longer ranges (each Revision of the range is one insert
in the merge transaction). Index jobs
build and drop per-Attribute indexes; they run as `database.maintenance_dsn` when it is set,
because their DDL needs a role that owns `mat_records` ([store schema
notes](store-schema-notes.md#physical-layout)). `indexes plan` prints that DDL for an
administrator where no such role is configured.

Scribe runs queued `gc` jobs and queues one per Domain at `scribe.scheduler.gc_schedule`
([scribe](scribe.md)).

The report is one JSON object per Domain (`revisions`, `changesets`, `history_rows`,
`provenance`, `nodes`, `node_bytes`, `objects`, `object_bytes`, `embeddings`, mark and sweep
seconds). With `--dry-run`, `embeddings` is the number of vectors that would be deleted. In
code: `collect_garbage(engine, domain_id, dry_run=...)`, `run_gc_job` for the `gc` job type
(`payload: {"dry_run": bool, "grace_seconds": int}`) and `enqueue_gc`. A failing job is retried
up to `store.jobs.max_attempts`.

Safety:

- One collection per Domain at a time (session advisory lock, distinct from branch locks); a
  second one fails with `GcBusyError`. Commits keep running.
- Before each batch of deletions the sweep marks again from the current refs and takes, without
  waiting, the advisory lock of every ref. A commit that landed during the collection is
  therefore kept with everything it reused, and a commit in flight makes the batch wait (up to
  `guard_wait_seconds`, then the collection stops; what was deleted so far is consistent).
  Domains with more than `store.gc.max_guarded_branches` refs need that setting raised (mind
  Postgres `max_locks_per_transaction`) or `guard_branch_locks: false`, which leaves only the
  grace period as protection.
- Assumption: a commit stores its objects, nodes and Revision in one transaction (design 6.6).
  Rows written ahead of their Revision by other means are not protected.
- Run a dry run first on production data and compare it with the expected branch deletions.
  Take a bundle (below) before the first collection of a long-lived Domain.

## Large branches

Copying or deleting a branch takes time in proportion to its rows (about 90 s for a copy at 1M
Records, [performance](performance.md#1m-records)), more than `database.statement_timeout_ms`.
Above `store.materialize.async_copy_min_rows` rows in the source, a copy branch is therefore
created as an overlay at once, readable and writable, and a `store.materialize` job copies its
rows; above `async_delete_min_rows` rows, a deleted branch disappears at once and a
`store.branch_delete` job deletes its rows. Both run in batches of `job_batch_rows` rows, one
short transaction each, resume after a failure or a restart from where they stopped, and at
most `job_concurrency` of them run at once across all workers ([store schema
notes](store-schema-notes.md#large-branches)). Scribe runs them like any other job; without
Scribe, `python -m lorebase_store.jobs work` does.

- **Progress.** `GET /api/v1/domains/{domain}/branches/{branch}` has `materialization_status`
  (`pending`, `running`, `done`, `failed`) and `materialization_progress` (`phase`, `rows_done`
  of `rows_total`, the last `error`). `lorebase branch create NAME --mode copy --wait` waits
  for the copy, printing progress on standard error, and fails when the copy fails. In SQL:
  `SELECT branch, status, phase, rows_done, rows_total, error FROM branch_materializations`.
- **Batch size.** Each batch must finish within `database.statement_timeout_ms`, and holds the
  branch lock while it runs: a commit on the branch, or on its source when it has to copy rows
  into the branch, waits for at most one batch. Batches size themselves between
  `job_batch_min_rows` and `job_batch_rows` to hold the lock about `job_batch_target_ms`, or
  `job_batch_contended_ms` once a commit waited for it (`lorebase.store.branch_jobs.batch.rows`
  and `.batch.duration`, [metrics](metrics.md#store)). Lower `job_batch_contended_ms` when
  commits on branches being copied must wait less; lower `job_batch_rows` when batches come
  close to the timeout.
- **Concurrency.** `job_concurrency` bounds the write load of copies and deletions on the
  database; a job that finds no free slot is queued again after `job_retry_seconds` and counted
  as `deferred` in `lorebase.store.branch_jobs.runs`.
- **While a copy runs.** The branch answers with `materialization: overlay`. Data and schema
  commits, merges and reads work, with overlay performance; a schema change is reconciled by
  the job after its switch (`phase` `prune` to `search`), and until then Records the branch
  did not write since the change keep derived values, titles and search vectors of the
  previous schema. It counts against `keeper.branches.max_materialized_per_domain`.
- **While a build runs.** A large copy that cannot start as an overlay (of an overlay, from an
  older Revision) answers with `materialization: building` until its rows are written from its
  tree. Reads of its head are served like reads at a past Revision (`served_from: tree`, or a
  snapshot); commits and merges into it answer 409 `branch-materializing` with `Retry-After`.
- **A failing copy** is queued again with a delay doubling from `job_retry_seconds` (at most
  `copy_retry_max_seconds`) each time its job uses `store.jobs.max_attempts` (`retried` in
  `lorebase.store.branch_jobs.runs`). After `copy_max_failures` failed jobs the overlay is
  detached from its base and built from its tree (`fallback`, alert
  [LorebaseBranchCopyFallback](#lorebasebranchcopyfallback)), so the deletion of its base never
  waits on it for good. A build that fails as often stays `failed`
  ([LorebaseBranchBuildFailed](#lorebasebranchbuildfailed)). Fix the cause (usually the
  statement timeout: lower `job_batch_rows`), then queue the job again; it resumes from its
  cursor:

  ```sql
  INSERT INTO jobs (type, domain_id, payload) VALUES ('store.materialize', 'supply', '{"branch": "feature"}');
  ```

- **While a deletion runs.** The name stays taken: creating a branch with it answers 409
  `already-exists` with `Retry-After`. A deleted branch that other overlays are based on waits
  for their background copies first (`waiting` in `lorebase.store.branch_jobs.runs`); a copy
  that keeps failing is detached and stops holding it up. Hidden
  branches still being deleted: `SELECT name, deleted_at FROM refs WHERE deleted_at IS NOT
  NULL`. Their heads stay garbage collection roots until the job ends.

## Reconciliation queue

Contradictions between Sources wait in the reconciliation queue ([design 10.6](design.md#106-reconciliation-of-source-contradictions)); Scribe raises them, people decide them. What an operator sets and watches:

- **Sweeps.** `scribe.reconciliation.sweep_interval_s` (0, off, by default) schedules a `reconcile_sweep` per Domain; `POST /api/v1/domains/{d}/reconciliation/sweeps` (`job.manage`) or "Queue a sweep" in the web app queues one on demand. A sweep stops after `sweep_max_changes_per_job` units of work and queues its continuation ([Scribe](scribe.md#reconciliation)).
- **Merge check.** `keeper.reconciliation.merge_check` (merges through Keeper) and `scribe.reconciliation.merge_check` (merges Scribe runs after syncs and extraction) are `off`, `warn` (default) or `block`; a Domain can make it stricter with its `reconciliation_check` setting, never looser. In `block`, a Proposal with open contradictions answers 409 `reconciliation-open` with the candidate ids; decide or dismiss them in the queue.
- **Domain overrides.** A Domain's `reconciliation` setting changes an allowlist of detection settings (thresholds, triggers, stages left out); files, work limits and the semantic judge stay in the configuration. A member that is not allowed is logged and ignored.
- **Metrics.** `lorebase.scribe.reconciliation.candidates` (raised, filtered, claim added, obsolete), `.sweep.duration` and the `.open` gauge per Domain and type ([metrics](metrics.md)). A growing open count means the queue is not being reviewed or a threshold is too loose.

Nothing in the queue changes data on its own; decisions are Revisions (or Proposals) with a `reconciliation` provenance and audit entries `reconciliation.resolve`, `reconciliation.dismiss` and `reconciliation.reopen`.

## Source syncs after the upgrade to corrections that stick

Migration `0040` creates `source_written` and `source_written_pending`, empty; nothing else is
migrated. From then on a Source writes only what changed upstream since it last wrote it, and
an upstream change that meets a value someone else set is a Conflict of the Source's Proposal
(`local_edits: review`, the default) instead of an overwrite ([Scribe](scribe.md#corrections-that-stick)).
What to expect and watch:

- **First run of each Source is compare-only.** It seeds the last-written state (`seeded` in
  the run stats). Values whose Lineage is the Source count as last written. Any other value of
  an owned Attribute (an import, an Operation, another Source) is compared
  only: equal to upstream, it seeds the state; different, it is a local edit. Under the
  default `local_edits: review` every such Attribute is a `DV1` Conflict of the run's
  Proposal, and every Record holding such values that the snapshot no longer has is a `DV2`.
  A Source whose target was loaded by an import, or edited by hand, can therefore meet many
  Conflicts on that first run.
- **`auto_when_clean` Sources stop.** A Source that merged every run without a person before
  the upgrade leaves the first run after it open whenever it meets a local edit, and merges
  again only once the Proposal is reviewed and merged (or closed: the next run raises the same
  Conflicts). Its later runs update the same open Proposal. Look for open Source Proposals
  with Conflicts, or the audit action `source.local_edit_conflict` (one entry per local
  edit, with its Proposal).
- **Before the first run.** Run each Source with `dry_run: true` to see the counts
  (`local_edits`, `seeded`). When the target's differing values are known to be stale
  copies of upstream (an import from the same system), set `local_edits: source_wins` on the
  Source for that first run, as the escape hatch: upstream values overwrite them as before
  the upgrade, each overwrite audited, and the state is seeded; set it back to `review`
  afterwards. `keep` lets values set by a person or an Operation win without review instead.
- **Reviewing in bulk.** `lorebase proposal conflicts PROPOSAL --status open --json` lists
  the Conflicts (their `id` is the fingerprint); `lorebase proposal resolve PROPOSAL --file
  resolutions.json` (a JSON array of `{fingerprint, choice}`, `ours` to keep the target's
  value, `theirs` to take upstream's) stores them all and re-runs the merge, as does
  `POST .../proposals/{p}/conflicts:resolve`; the Review screen of the web app offers the same
  choices. Then merge the Proposal.
- **Old behavior.** Set `local_edits: source_wins` on a Source (in the schema: the Modeler or
  `lorebase schema apply`) to let upstream values overwrite corrections again; `keep` lets a
  person's value win without review.
- **State size.** One row per Source, target branch and Record (and per Record and mapped
  Relation for Links), with hashes only. Rows of a deleted target branch stay until the Source
  is removed; they are small.

## Document versions after the upgrade

Migration `0041` adds `source_key`, `version`, `replaces`, `status` and the retired and purged
columns to `documents` (existing rows read as keyless, version 1, `active`), makes the unique
`(domain_id, sha256)` index partial (keyless rows that are not purged), adds the indexes of
keyed versions, and adds `chunk_hash` to `extraction_cache`. It is metadata only: nothing is
rewritten, and uploads without a `source_key` behave as before. Callers that upload the same
path or URL repeatedly (a Source, a script) should start sending it as `source_key`, so
changed bytes become versions whose evidence is checked ([extraction](extraction.md#document-versions-and-lifecycle)).
The downgrade stops if documents share bytes (versions, or purged rows); remove the extra rows
first.

- **Version checks.** `document_version_check` jobs are queued by extraction jobs of new
  versions and by retirements; each looks at `scribe.extraction.versions.max_values_per_job`
  values and queues its continuation. They raise `stale` candidates in the reconciliation
  queue (signal `evidence_gone`), counted with the other candidates. A Domain turns them off
  with `reconciliation.triggers.document_versions: false`.
- **Retirement** (`document.retire`, maintainers and admins) keeps the document readable and
  stops its extraction; audited `document.retire`.

## Semantic search index after the upgrade

Migration `0044` builds `ix_mat_records_object_hash` on `mat_records (domain_id, branch,
object_hash)`, the probe of the join in semantic search (design 8.8). It builds the index per
hash partition with `CREATE INDEX CONCURRENTLY` (outside a transaction, so writes continue;
an interrupted run leaves an invalid index that the next run drops and rebuilds), then attaches
the partition indexes to the parent. Nothing is rewritten and no restart is needed. The
build time grows with the rows of each partition (a B-tree on three columns; not measured at
tens of millions of Records) and the index takes about 40 bytes per Record and branch row; on
a large install plan the migration for a quiet period and follow `pg_stat_progress_create_index`. The migration role must own `mat_records`.
Writes pay little (commits show no difference; bulk deletes of a branch about 15% longer).
Until the index exists, semantic search still answers, with the cost of the model's vectors.
The downgrade drops the index.

## Rebuilding the embedding index

The HNSW index of `embeddings` (`embeddings_hnsw_<dims>`, or `embeddings_hnsw_half_<dims>` with
`embedding_precision: half`) is built with `store.storage.hnsw_m` (default null: pgvector's 16)
and `hnsw_ef_construction` (default 128; pgvector's own default is 64). The semantic list is
only as good as that graph: on 300,000 clustered vectors ef_construction 64 gives a mean
recall@20 of 0.910 unfiltered, 128 gives 0.988 (index 586 MB either way, build 50 s against
66 s; [performance](performance.md#filtered-semantic-search)). Raising `hnsw_m` costs
size and build time and did not do better there (m 32 with ef_construction 128: 0.974, 608 MB,
124 s).

The settings apply to indexes built from then on: a new installation (migration `0001`) and
`storage apply` building the index of another precision. `storage apply` does not rebuild an
existing index whose parameters differ, so **an installation from before this release keeps its
index (ef_construction 64)** until it is rebuilt. Check with
`SELECT reloptions FROM pg_class WHERE relname = 'embeddings_hnsw_384'` (null: pgvector's
defaults). To rebuild it, as the table owner:

```sql
ALTER INDEX embeddings_hnsw_384 SET (ef_construction = 128);   -- and m = ..., if set
REINDEX INDEX CONCURRENTLY embeddings_hnsw_384;
```

`REINDEX CONCURRENTLY` keeps reads and writes going and builds a second copy beside the first:
plan disk for one more index and `maintenance_work_mem` for the build (the graph is built in
memory up to that size, then much slower), and follow `pg_stat_progress_create_index`. A failed
run leaves an invalid `..._ccnew` index: drop it and run again. Search keeps answering from the
old index until the new one replaces it. Keep `store.storage` in line, so that an index built
later matches.

## Approval rules and automatic merges

`scribe.sources.auto_merge_protected` and `scribe.extraction.direct_merge_protected` (both on by
default) let automatic merges (a Source with `auto_when_clean` or `auto_with_authority`, an
extraction job in `direct` mode) into a protected branch: the operator's trust in automation,
which waives the plain `keeper.proposals.required_approvals` count. They do not waive the rules
that name who must approve. When the target's effective rules include steward approvals
(`required_approvals_from_stewards` above 0) or required roles (`required_roles` not empty),
from `keeper.proposals` or the Domain setting of the same name, the automatic merge is not
performed and the Proposal stays open for review; the Source run's stats say
`merge_blocked: approvals`, an extraction job's result `direct_blocked: approvals`, both with
`blocking_rules`. Installations without steward or role rules see no change. Scribe reads
`keeper.proposals` from the shared configuration file: give Keeper and Scribe the same file.

## Ingestion modes and stewards after the upgrade

Migration `0042` adds `proposals.kinds` (the Kinds a Proposal's changes touch, with a GIN
index), empty for existing rows; each Proposal fills it at its next merge preview (a review,
an item decision, a merge attempt, a new run of its Source). Until then its Kinds are
computed when its steward roles, assignees or approval rule are read, from the Kinds that
differ between its branches' common ancestor and its source head, so steward approvals apply
to existing Proposals at once (the `assigned` filters included). Nothing else changes unless
configured:

- **Ingestion modes.** Without `settings.extraction` on a Domain, extraction accepts and holds
  exactly what it did ([extraction](extraction.md#ingestion-modes)). `scribe.extraction.mode`
  may be `review_all` or `doubts_only`; `direct` there stops Scribe at startup. To forbid
  Domains from choosing `direct` (or any looser value), set
  `scribe.extraction.domain_may_loosen: []`; to keep direct merges out of protected branches,
  `scribe.extraction.direct_merge_protected: false`. Direct merges are in the audit log as
  `proposal.merge` with `mode: direct`; setting changes as `domain.update`. Keeper reports
  `ignored_settings` with the `scribe.extraction` values of its own configuration: give Keeper
  and Scribe the same file.
- **Stewards.** Declare `stewards: [role, ...]` on `domain` and on Kinds in the schema (a role
  the installation does not define is a validation warning, `schema.steward_role_unknown`).
  Assignees are computed from grants when read: listing them for callers with
  `principal.read` scans at most `keeper.stewards.max_grants_scanned` grants and shows
  `max_assignees_listed`. Principals who belong to a group only through an identity provider's
  token are assignees for their own `assigned=me`, but are not listed (their membership is not
  stored).
- **Steward approvals.** `keeper.proposals.required_approvals_from_stewards` (or the Domain
  setting of that name; default 0) requires approvals from assignees on protected branches. A
  Proposal whose Kinds have steward roles but too few assignees answers 409
  `stewards-unavailable` on merge: grant the role or lower the rule.
- **Required roles.** `keeper.proposals.required_roles` (or the Domain setting of that name;
  default empty: off) lists RBAC roles that must each approve a Proposal into a protected
  branch: one approval from a principal holding the role, who may review it on the target. A
  merge without one answers 409 `approvals-required` naming the missing roles.

## Document purge

`POST /api/v1/domains/{d}/documents/{id}/purge {"reason": "...", "confirm": "<id>"}` (or
`lorebase documents purge ID --reason TEXT --confirm ID`; `document.purge`, `admin` only) is
the erasure of a document, irreversible:

- retired first if needed (its evidence check is queued), then its bytes and parsed text are
  deleted from the blob store unless another document holds the same blob (the same bytes
  under another key, or a keyless copy), its chunks deleted, and the cached model answers of
  chunk hashes no other document of the Domain holds deleted;
- the evidence quotes of its Proposal items, reconciliation candidates (claims, signals and
  recorded decisions) and schema suggestion examples are replaced by
  `keeper.reads.sensitivity.erased_value` (`[erased]`); offsets and document ids stay;
- provenance rows stay (job, document id, offsets): Lineage still says a value came from the
  document, and reads `[erased]` for its quote. Values are not changed: retract them through
  the reconciliation queue or an Operation;
- the queued and running extraction jobs of the document are cancelled. The purge locks the
  document row, and a running job locks it again before each write of text, chunks, items,
  candidates or suggestions, so a job that was running stops at its next write (`cancelled`)
  instead of writing content back; a model answer is cached only while its chunk is stored;
- the purge and its audit entry (`document.purge`: reason, counts, hashes, `jobs_cancelled`;
  never content) commit together; purging again erases nothing.

What a purge does not reach: values written into Records (they are Revisions), Revisions of
extraction branches, answers cached before migration `0041` (they hold no chunk hash: delete
them with `DELETE FROM extraction_cache WHERE chunk_hash IS NULL` if needed; they are
recomputed on the next extraction) and backups taken before. Keyless re-uploads of the same
bytes after a purge create a new document.

## Backup and restore of a Domain

A bundle is a portable copy of one Domain's versioned content: all Revisions reachable from a
ref with parents and changesets, the tree nodes and objects they reach, refs and ref log,
provenance entries, and a readable `schemas/<branch>.yaml` per branch head. Materialized
heads are not stored; import rebuilds them from the trees.

```
python -m lorebase_store.bundle export --domain supply --out /backups/supply        # directory
python -m lorebase_store.bundle export --domain supply --out /backups/supply.tar    # tarball
python -m lorebase_store.bundle import --in /backups/supply.tar [--domain supply] [--no-verify]
```

- Export reads one repeatable-read snapshot, so it is consistent while commits continue. Files
  are `.jsonl` (gzip by default, `store.bundle.compression`), written and read in batches of
  `store.bundle.batch_rows`; memory does not grow with the Domain.
- `manifest.json` carries the format version, the Lorebase version, record counts, branch
  heads and the sha256 of every file. Import refuses unknown format versions, checks every
  file hash, recomputes every Revision id, node hash and object hash, and checks that every
  node and object reachable from the refs is present. It runs in one transaction and rebuilds
  the materialized head of each branch that had one, verifying it against its tree
  (`store.bundle.verify_after_import`). A failing import leaves nothing behind.
- Import reads within `store.bundle.max_entries`, `max_member_bytes` (per file, counted as read,
  after gzip), `max_total_bytes` and `max_line_bytes` (per `.jsonl` record and per `.json`
  file), and refuses links, special files, absolute paths and `..` in a tarball or directory;
  links are never followed. Tarballs must be uncompressed `.tar`, as export writes them.
- Bundles are trusted input: hashes prove integrity, not origin. Authors, provenance, the ref
  log, changesets and Domain settings are imported as written, so import only bundles from
  trusted sources.
- The target Domain must not exist, or must have no Revisions and no refs. Revision ids include
  the Domain id, so a bundle can only be restored under the id it was exported with.
- Unreachable data (collected or not yet collected garbage) is not exported, so restoring a
  bundle also compacts the Domain.
- Vault values are not exported by default: after a restore they read as unknown. `export
  --include-vault-values [--actor <principal>]` also writes `vault.jsonl` with every value the
  exported Records reference, in plain text, or `erased`, plus the Domain's key changes
  (`vault_lineage.jsonl`) and erasure tombstones (`vault_tombstones.jsonl`). It needs the vault
  keys, takes its snapshot under the Domain's vault lock (an erasure in flight finishes first),
  creates the files and tarball readable by their owner only, and appends
  `bundle.export_plain_started` before reading any value and `bundle.export_plain` (Domain,
  heads and counts, never values) when done; if an entry cannot be written the output is
  removed. `--actor` is recorded as given: the command runs with the database credentials and
  vault keys of the installation, so restrict who can run it.
- The export holds the vault lock shared only until its snapshot exists, not while it reads
  and writes the files. An erasure asked for in that moment waits up to
  `vault.erase_lock_timeout_ms`, then answers 503 with `Retry-After` and can be repeated; one
  asked for later goes ahead, and the bundle shows the Domain as it was at the snapshot.
- Tombstones recorded before they kept their token key (`fingerprint_key` empty) are exported as
  made under the exporter's current `vault.token_key_id` with the first fingerprint scheme
  (`<id>/1`); `manifest.json` counts them under `vault_notes`. Vault entries carry `moved_to`,
  the key a key change took the token over to.
- Import needs the target's vault keys and the token key id every tombstone was made under
  (`vault.token_key_id` or `previous_token_keys`); a tombstone without one is refused, never
  dropped. Import seals the values under the target's keys and checks that:
  - every key change links two Records of its Kind that the bundle holds;
  - every Record holding a token holds it in the bound Attribute, under the bound key or one
    linked by a key change;
  - every `moved_to` follows a key change of the bundle;
  - every value is valid for the bundle's schema.

  It stores a value a tombstone holds as `vault.erased_values` says, and records a
  `bundle.import_vault` audit entry (`import --actor`) with the counts: values, erased values,
  tombstones, key changes, and groups of more than 1000 linked keys, which an administrator
  should look at. A target Domain with vault rows, tombstones or key changes is not empty.
  Treat such a bundle as personal data and delete it once restored.

Not in a bundle: the audit log, principals, groups, grants and API keys (installation-wide, not
Domain content), and per-Domain operational tables: Proposals and reviews, merge attempts,
Source runs, documents and chunks, extraction jobs, vault entries (unless exported with their
values), embeddings, Operation invocations, outbox and jobs. Back these up with `pg_dump`, which also covers a whole
installation in one step:

```
# installation-wide tables: audit chain, principals and access
pg_dump --format=custom --file=access.dump \
  --table=audit_log --table=principals --table=groups --table=group_members \
  --table=grants --table=api_keys  "$DSN"

# everything (the usual disaster-recovery backup; restore with pg_restore)
pg_dump --format=custom --file=lorebase.dump "$DSN"
```

Restore the audit log with its original `seq` values and into an installation whose chain is
otherwise empty: the hash chain is verified in `seq` order and the first entry of a chain is
its anchor. `pg_dump` of the whole database is the only way to keep the chain, vault and
embeddings intact; bundles are for moving or archiving Domain content and for restoring it
into a fresh installation, after which embeddings are recomputed.

## Audit chain verification

The audit log is hash-chained: one chain per Domain plus one for entries without a Domain
(design 10.4). Each entry stores `sha256(prev_hash || canonical entry)`, so editing, deleting,
inserting or reordering a row breaks the chain from that row on. Three ways to recompute it,
all reading entries in batches of 1000 in `seq` order (memory holds one batch and one hash per
chain, so the length of the log costs time only):

- `python -m lorebase_keeper audit verify DOMAIN`, `--installation` or `--all`, connected like
  `bootstrap` (`database.maintenance_dsn` when set). It prints the result, or JSON with
  `--json`, and exits 1 when an entry does not verify. `--after-seq N` starts after entry N
  and trusts it: pass the `last_seq` of an earlier run to verify only what was appended since.
- `GET /api/v1/domains/{domain_id}/audit/verify` (permission `audit.read`, the `admin` role) and
  `lorebase audit verify -d DOMAIN`, which exits 2 on a broken chain. They cover one Domain's
  chain; the installation-wide chain is verified from the shell or by Scribe.
- Scribe queues an `audit_verify` job of every chain each `scribe.scheduler.audit_verify_interval_s`
  (default one day, 0 disables it) and when a process becomes scheduler leader. The job counts in
  `lorebase_audit_verifications_total` with `outcome` `intact` or `broken`, and the
  `LorebaseAuditChainBroken` alert fires on a broken one.

The answer names the first entry that does not verify and why: `chain link broken` (its
`prev_hash` is not the hash of the entry before it: a row was removed, inserted or reordered)
or `entry content changed` (the stored hash does not match the content: a row was edited).
Entries before it verified. The chain shows that the log was not changed by someone who cannot
also rewrite every later hash. On its own it cannot show that the newest entries of a chain were
deleted, and an owner of the database can recompute a whole chain: checkpoints cover both.

### Checkpoints outside the database

With `scribe.audit_checkpoints.enabled`, the scheduler leader queues an `audit_checkpoint` job
every `interval_s` (one hour). It signs the head (`seq`, `entry_hash`) of every chain of each
stream in `streams` with an Ed25519 key and stores one JSON document per stream as
`{prefix}{stream}/{UTC time}.json` in the sink: `file` (a directory, ideally a volume the
database host cannot write; files are never replaced), `http` (PUT or POST to `http.url`, with
headers from secret references) or `s3` (a PUT signed with Signature Version 4 to an
S3-compatible bucket; turn on versioning or object lock and give the key `PutObject` only). HTTP
targets must use `https` and resolve to global unicast addresses unless `allow_private_targets`
(an in-cluster store); redirects are not followed. `lorebase.audit.checkpoints` counts them by
outcome; a failure fails the job attempt, which is retried.

```bash
openssl genpkey -algorithm ed25519 -out checkpoint.pem          # scribe.audit_checkpoints.signing_key
openssl pkey -in checkpoint.pem -pubout -out checkpoint.pub     # for verifiers
# copy the checkpoints locally (aws s3 sync s3://audit-anchors/lorebase-audit ./checkpoints, ...)
python -m lorebase_keeper audit verify --all --checkpoints ./checkpoints --checkpoint-key checkpoint.pub
```

`--checkpoints` takes files or directories (searched for `*.json`) and is repeatable; only the
checkpoints of the stream verified are compared (`--reads` for the read stream). The run exits 1
when a checkpointed entry is `missing` (the tail of a chain was cut) or `changed` (a chain was
recomputed), when a file is not a checkpoint signed by that key, when no checkpoint of the
stream is found, or, with `--max-age SECONDS`, when the newest one is older (publication
stopped: someone holding the database could have removed the jobs). `--json` lists them under
`checkpoints`. Entries newer than the latest checkpoint are not covered: run it with
`--max-age` a little above `interval_s`.

### Read auditing

`keeper.auth.audit.reads.enabled` (off by default, needs the audit log and the database) records
successful reads of authenticated callers in `audit_read_log`: GET and HEAD, and the POST routes
that only read (query, traverse, neighborhood, search, context). Each record has the actor, the
Domain, action `read`, the route template, status, method, request id, principal type, the Kind
when the route names one (also the target) and `sensitive` (`shown`, `masked`) when the read
showed or masked a value of a `pii`, `confidential` or `restricted` Attribute, vault values
included. Query strings, bodies and values are never recorded.

- `principal_types` and `kinds` keep only some reads, then `sample_rate` keeps a share of them.
  Sensitive reads are recorded whatever the filters and the sampling.
- Records are queued (`queue_size`) and written in batches (`batch_size`, at least every
  `flush_interval_s`), one transaction each, never on the request path. A full queue or a batch
  that fails within `write_timeout_s` drops records: `lorebase.audit.reads.dropped{reason}`
  (`queue_full`, `write_failed`, `shutdown`) and `sensitive`; written ones count in
  `lorebase.audit.reads.recorded`. The last tenth of the queue takes only sensitive reads, and a
  batch holding one is tried twice; alert on drops with `sensitive="true"`.
- With the default `on_overflow: drop` the read stream is evidence, not proof. Where it must be
  proof, set `on_overflow: reject`: a read is answered only once its record holds a place in
  the queue, taken before the status line and any of the body leave (streamed bodies too);
  without one the read gets 503 `read-audit-unavailable` with `Retry-After` (`retry_after_s`),
  counted in `lorebase.audit.reads.refused`. A batch that fails is kept and retried every
  `flush_interval_s`, never dropped (`lorebase.audit.reads.write_failures`); after
  `unready_after_failures` failures in a row `/readyz` reports `read_audit` failed until a
  batch is written. Records still queued at shutdown are lost and counted (`shutdown`).
  `sample_rate` and the filters still apply; with `1.0` every read is recorded.
- The read stream has hash chains of its own, so it never slows down or breaks those of
  actions. Verify it with `audit verify --reads`; the daily `audit_verify` job checks both
  streams (`lorebase_audit_verifications_total{stream}`), and checkpoints cover both.
- Volume: one row per recorded read. Lower `sample_rate` or filter by Kind on busy
  installations, and size the table's retention like the audit log's (as the owner).

## Rate limits and quotas

Keeper limits how fast a principal may call the API and, for API keys, how much it may use per
day (design 13.6). Both are on by default with generous limits; set them under
`keeper.limits.rate` and `keeper.limits.quota` (`config/lorebase.example.yaml`).

**Rate limits** are token buckets. Each principal (the API key id, or the principal id of a
user) has one bucket per request class: `reads` (GETs, query, traverse, neighborhood),
`writes` (other POST, PUT, PATCH, DELETE), `searches`, `context` (context packs) and `admin`
(routes tagged `admin`). A bucket refills at `requests_per_minute` and holds at most `burst`
tokens. A Domain can override any class (`domains.<id>.<class>`); requests in that Domain then
use a bucket of their own.

- Every response of a limited route has `RateLimit-Limit` (the burst), `RateLimit-Remaining`
  and `RateLimit-Reset` (seconds until the bucket is full again).
- A refused request is `429` with the problem type `rate-limited`, `Retry-After` (seconds) and
  the members `class` and `retry_after_s`. A refused request takes no token.
- `/healthz` and `/readyz` are exempt (`exempt_paths`). Requests without a valid credential
  take no token from these buckets: they are answered `401`.
- **Failed authentications** have buckets of their own (`auth_failures`, default 30 per minute,
  burst 30): one per client address (IPv6 by its `/64`, `ipv6_prefix_length`) and one per
  presented API key prefix. Each `401` to a request that carried a credential takes a token from
  both; a success takes nothing. While either bucket is empty, requests from that address or
  with that prefix, valid ones included, are answered `429` (class `auth_failures`,
  `Retry-After`) before any key lookup or token validation, and are logged at DEBUG only. An
  attacker who knows a key's prefix can therefore hold that key off for as long as the attack
  lasts; raise `burst` if that matters more than the database load, or set
  `per_key_prefix_enabled: false` to keep only the per-address bucket. Client addresses come from
  `keeper.trusted_proxies` (see below).
- A request takes one token whatever it carries: a bulk import (`import_records`,
  `import_links`, `import_batch`) or a bulk delete (`delete_records`, up to
  `keeper.operations.max_bulk_delete_keys` keys in one Revision) is one `writes` token. Reset a
  Domain's Records with `delete_records` rather than one `delete_{kind}` call per Record.
- The limit counts `lorebase.http.rate_limited{class}` (OpenTelemetry) per refusal.
- A principal refused `throttle_audit.threshold` times within `window_s` produces one audit
  entry `rate_limit.throttled` (actor, Domain, API key as target, `refusals`, `reason`); the
  refused requests themselves are not audited one by one.
- `backend: local` (the default; `memory` is an alias) keeps the buckets in the process, so N
  Keeper processes allow N times the limit. `backend: postgres` shares them through the
  application database (the UNLOGGED table `rate_limit_buckets`, migration `0022`): a Keeper
  leases up to `postgres.lease_tokens` tokens (at most a quarter of a bucket) in one query and
  serves requests from them until they run out or `lease_ttl_s` passes, so most requests cost no
  query and the Keepers together never exceed a limit (unused leased tokens are lost: limits are
  slightly stricter, never looser). Failed-authentication buckets take one token per `401` and are
  checked from a reading at most `check_cache_s` old. When their query does not answer within
  `auth_failures_timeout_s`, failed-authentication checks answer `429` (fail closed) unless a
  reading of the same bucket at most `stale_reading_s` old allows the request, so an outage
  refuses unknown clients rather than every client. When a query of another bucket does not
  answer within `timeout_s`, it follows `on_error`: `local` (default, the in-process buckets meanwhile), `allow` or `deny`;
  `lorebase.ratelimit.shared.queries{store,outcome}` counts the queries. Rows of full buckets are
  pruned every `prune_interval_s`. A crash of the database server empties the table (buckets
  start full). Compose: set `KEEPER_RATE_LIMIT_BACKEND=postgres` with the `scale-out` profile;
  the Helm chart sets it when more than one Keeper runs.

**Daily quotas** apply to API keys, counted in UTC days: `requests_per_day` and
`context_tokens_per_day` (the estimated tokens of context packs delivered, using
`agents.context.chars_per_token`). A quota that is used up answers `429` with the problem type
`quota-exceeded`, `Retry-After` to the next UTC midnight and the members `quota`, `limit` and
`resets_at`. `keys.<api key id>` overrides the limits of one key (`null` removes a limit).
Use is kept in `api_key_usage` (migration `0006`): each process counts in memory and writes at
most every `flush_interval_s`, adding to the stored row, so quotas hold across restarts and
across processes up to one interval of traffic. If the database fails, requests are still
served and the quota is enforced on what is known. `GET /auth/whoami` shows a key's `quota`
with its use, remaining amounts and reset time.

## Client addresses and API key networks

An API key may carry `allowed_networks` (CIDR, IPv4 or IPv6; `apikey create --allow-network
203.0.113.0/24`, repeatable). From any other client address it is refused with the same `401`
as an invalid key, and Keeper logs `api key rejected` at INFO with the key prefix and the client
address. Keys without networks, including every key created before migration `0009`, work from
anywhere. Networks are set when the key is created (at most
`keeper.auth.api_keys.max_allowed_networks`); to change them create a new key and revoke the old.

The client address is the socket peer. When the peer is in `keeper.trusted_proxies`,
`X-Forwarded-For` is read from right to left, trusted hops are skipped and the first other
address is the client; entries further left are written by the client and never believed. A hop
that is not an address ends the walk with no client address, which no allowlist matches.

## Dashboards and alerts

`deploy/observability` holds the whole bundle: the collector configuration, Prometheus, three Grafana
dashboards and the alert rules. The series they use are listed in [metrics](metrics.md), which also says
which signals the code does not record yet (database pool usage, extraction tokens and cost).

Switch telemetry on with `telemetry.otlp_endpoint` (`LOREBASE__TELEMETRY__OTLP_ENDPOINT`); without it
nothing is exported. The services send OTLP/gRPC to the collector, which exposes metrics on `:8889` for
Prometheus and sends traces to an OTLP backend (Jaeger in the compose profile; set
`LOREBASE_TRACES_OTLP_ENDPOINT` on the collector for Tempo or another one).

- **docker compose.** `docker compose --profile observability up` starts the collector, Prometheus, Jaeger and
  Grafana. Prometheus loads `deploy/observability/alerts/*.yml`; Grafana provisions the dashboards from
  `deploy/observability/grafana/dashboards` and the Prometheus and Jaeger data sources (`GRAFANA_ADMIN_PASSWORD` is
  required). There is no Alertmanager: firing alerts show in the Prometheus and Grafana UIs.
- **Kubernetes.** `observability.enabled=true` renders ConfigMaps from the same files: `-dashboards` (labelled for
  the Grafana sidecar, `observability.dashboards.labels`), `-alert-rules` and `-otel-collector`. Mount them into
  your Prometheus (`rule_files`), Grafana and collector. Scraping the collector is `serviceMonitor`
  ([kubernetes](kubernetes.md#observability)). The chart folder links to `deploy/observability`, so the files
  have one source.
- **Dashboards** use a `datasource` variable, so they work with any Prometheus data source.

| Dashboard | Shows |
|-----------|-------|
| Lorebase Keeper | Requests per second, mean, p50 and p95 latency per route class, 4xx and 5xx, rate limited, quota exceeded, authentication failures by reason, read response cache hit ratio and lookups |
| Lorebase Store | Replica lag and reachability, reads by serving database and reason, commit duration (overall and by origin), writes by status, merge attempts by result, `SECURITY DEFINER` function calls, garbage collections and bytes freed, index jobs |
| Lorebase Scribe | Jobs by type and status, failure ratio, retries, queue depth, jobs claimed, durations, scheduler against finished jobs, webhook deliveries by outcome, Source sync rows and changes, extraction runs and items, garbage collections and bytes freed |

The latency histograms declare bucket boundaries from 5 ms to 10 s (see
[metrics](metrics.md#histogram-buckets)), so the p50 and p95 panels and the latency alert read real
quantiles. Thresholds are written into the rule expressions; edit
`deploy/observability/alerts/lorebase.rules.yml`.

### Alert runbook

Every alert's `runbook_url` points to its entry here.

#### LorebaseKeeperLatencyOverBudget

The p95 of a route class (label `route_class`) has been over its budget for 10 minutes: reads 100 ms, queries
and searches 250 ms, context packs 1 s, writes 2.5 s. Compare with the baseline in
[performance](performance.md). Check the Keeper dashboard for the class and routes involved, then the database:
slow statements (`pg_stat_statements`), locks, a missing per-Attribute index (`python -m lorebase_store.indexes
plan --domain <id>`), and whether reads are going to a lagging replica (Store dashboard). A single slow route in
Jaeger (spans `GET /route`) shows where the time goes.

#### LorebaseKeeperHighErrorRatio

More than 5% of responses are 5xx. The 5xx per route panel names the routes; the Keeper logs hold the stack trace
of each, keyed by the `request_id` returned to the client. Typical causes: the database is unreachable or out of
connections (`/readyz`), a migration is missing (`migrate` job), a failing dependency of a route.

#### LorebaseKeeperMetricsMissing

Prometheus has no request metric from Keeper for 15 minutes. Keeper may be down, idle, or not exporting: check
`telemetry.otlp_endpoint` on Keeper, the collector logs and `:8889/metrics` on the collector, and that Prometheus
scrapes it.

#### LorebaseAuthFailuresThrottled

Clients keep failing to authenticate and Keeper answers their requests 429 (class `auth_failures`, see [rate
limits](#rate-limits-and-quotas)). The Keeper dashboard splits the failures by `reason`
(`lorebase_auth_failures_total`): `unknown_key` and `wrong_secret` point at guessing or a stale secret, `expired`
and `revoked` at an integration that was not updated, `network` at a client outside the key's allowed networks.
Look at the `api key rejected` INFO lines for the key prefix and client address.
A valid key whose prefix is being attacked is held off too: raise `keeper.limits.rate.auth_failures.burst`, or
block the address in front of Keeper. A misconfigured integration shows as one prefix with a steady rate.

#### LorebaseReplicaLagHigh

A replica replays more than 5 s behind the primary. Keeper already sends reads to the primary above
`database.read_max_lag_ms`, so the effect is load on the primary rather than stale reads. On the replica:
`SELECT now() - pg_last_xact_replay_timestamp()`; on the primary: `pg_stat_replication`. Look for long
transactions on the replica, I/O saturation or a network problem. Without `database.read_fallback_to_primary`
reads fail instead (`ReplicaUnavailableError`).

#### LorebaseReplicaDown

A replica did not answer its probe for 5 minutes. Check the replica's Postgres and the network path from Keeper;
`/readyz` reports `database_replicas`. Reads fall back to the primary meanwhile.

#### LorebaseJobFailureRatio

More than 20% of the runs of a job type (label `type`) ended `failed`, that is, used all `store.jobs.max_attempts`
or hit a permanent error. Rows: `SELECT id, domain_id, attempts, result FROM jobs WHERE type = '<type>' AND status =
'failed' ORDER BY id DESC LIMIT 20` (`result.error` holds the last error); Scribe logs `job failed` with the
exception. Fix the cause, then requeue the rows with `status = 'queued', attempts = 0`.

#### LorebaseJobQueueStalled

Claimable jobs have been queued (`lorebase_jobs_queue_depth{status="queued"}`, reported by the scheduler leader;
jobs waiting for a retry delay are counted as `retrying` and do not count) while no worker claimed one (`lorebase_jobs_claimed_total`) in the last 15 minutes. Scribe workers are not running, cannot
claim (database down), or every job hangs. The Scribe dashboard shows the queue depth by type. Check the Scribe pods
and logs, `SELECT type, status, count(*) FROM jobs GROUP BY 1, 2`, and rows whose lock is older than `store.jobs.stale_after_seconds` (the scheduler leader reclaims those; with no leader nothing does).
`python -m lorebase_store.jobs work` drains the queue from a shell.

#### LorebaseJobsReclaimed

Running jobs of a type (label `type`) went without heartbeat for `store.jobs.stale_after_seconds` and the scheduler
leader took them back (`lorebase_jobs_reclaimed_total`; `outcome` is `requeued`, or `failed` once
`store.jobs.max_attempts` were used). Their worker was killed (out of memory, `SIGKILL` after the stop grace period,
node lost), froze, or lost the database. A clean stop releases its jobs (`lorebase_jobs_released_total`) and does not
fire this. Check the Scribe containers or pods for restarts and `OOMKilled`, the `stale job reclaimed` log lines of the
leader, and that `SCRIBE_STOP_GRACE` (compose) or `scribe.terminationGracePeriodSeconds` (chart) exceeds
`scribe.worker.shutdown_grace_s` plus `scribe.worker.shutdown_cancel_s`. Requeued jobs run again on another worker.

#### LorebaseOutboxPruneBacklog

Outbox events eligible for pruning stayed above 50000 for 30 minutes (`lorebase_outbox_prune_backlog`, capped at
100000 by the count): pruning cannot keep up with the event rate, or Scribe stopped passing. Check that the
webhook delivery job runs (job queue and Scribe dashboards), then raise `store.outbox.prune_budget_s` or
`store.outbox.prune_batch`, or shorten `store.outbox.retention_s`. Each batch is a short transaction; a pass
spends at most the budget on it.

#### LorebaseWebhookDeliveryFailing

Deliveries became dead in the last hour (`lorebase_webhooks_deliveries_total{outcome="dead"}`); the Scribe
dashboard shows `delivered`, `retrying` and `dead` side by side. Dead letters are rows of `webhook_deliveries`:
`SELECT webhook_id, count(*), max(last_error) FROM webhook_deliveries WHERE status = 'dead' GROUP BY 1`. A target
refused (scheme, private address) is dead at once; others after `webhooks.max_attempts`. Fix or disable the
webhook; the pass retries what is still pending. An error naming `webhooks.encryption_key` means the key
that sealed the webhook's signing key is no longer configured: put it back, as the current key or in
`webhooks.previous_encryption_keys`.

The outbox keeps a backlog per consumer (`outbox_pending`): `SELECT consumer, count(*) FROM
outbox_pending GROUP BY 1`. Events leave the outbox once every consumer handed them off and they
are older than `store.outbox.retention_s` (7 days), or after `store.outbox.max_age_s` (30 days)
in any case; the latter are counted in `lorebase_outbox_pruned_total{reason="expired"}` and
logged. A pruning pass (one per webhook delivery pass) deletes batches of `store.outbox.prune_batch`,
each in its own transaction, until nothing is left or `store.outbox.prune_budget_s` (5 s) is spent;
`lorebase_outbox_prune_backlog` is what stayed prunable after the latest pass, counted up to 100000.
To stop keeping events for a consumer that no longer runs, delete its row from
`outbox_consumers` (its pending rows go with it).

A receiver that stopped answering does not hold Scribe: each attempt is cut off after `webhooks.timeout_s`
(`last_error` reads `timed out after 10s`, counted in `lorebase_webhooks_timeouts_total`), a pass starts no attempt
after `webhooks.pass_timeout_s` and releases the rest to the next pass (`released` in the job result), and a pass
still running after `webhooks.lease_s` is abandoned as a failed job attempt, since its deliveries may then be sent
by another pass. Many timeouts with a steady `released` count mean slow receivers: raise
`webhooks.concurrency` or fix the receiver.

#### LorebaseBranchCopyFallback

The background copy of an overlay failed `store.materialize.copy_max_failures` times
(`lorebase_store_branch_jobs_runs_total{job="materialize",outcome="fallback"}`): the overlay was
detached from its base and is being rebuilt from its tree (`materialization: building`; commits
into it wait). Read the errors in `branch_materializations.error` and the worker logs: usually
`database.statement_timeout_ms` against the batch size, or a constraint the base rows break.
The branch needs nothing further unless
[LorebaseBranchBuildFailed](#lorebasebranchbuildfailed) follows.

#### LorebaseBranchBuildFailed

A branch build from its tree failed `copy_max_failures` times
(`lorebase_store_branch_jobs_runs_total{job="materialize",outcome="given_up"}`): the branch
stays `building` (`branch_materializations.status = 'failed'`), its head is still readable and
commits into it are refused. Fix the cause from `branch_materializations.error`, then queue a
`store.materialize` job for the branch again ([large branches](#large-branches)); it resumes
from its cursor. A branch that is not needed can be deleted instead.

#### LorebaseGcNotRun

No garbage collection completed in 3 days (`lorebase_gc_runs_total{dry_run="false"}`) although the scheduler
queued real collections (`lorebase_gc_scheduled_total{dry_run="false"}`). A schedule set to dry runs
(`scribe.scheduler.gc_dry_run: true`) queues only dry runs and does not raise the alert; nor does a null schedule. Check
`scribe.scheduler.gc_schedule` (null disables the schedule; change the window in the rule if
you collect less often), the `gc` rows of `jobs` and the Scribe logs; a collection that never ends makes the next ones fail with `GcBusyError`. Run one by hand with
`python -m lorebase_store.gc --all` ([garbage collection](#garbage-collection)).

#### LorebaseAuditChainBroken

A scheduled `audit_verify` job found an audit entry that does not verify
(`lorebase_audit_verifications_total{outcome="broken"}`). Treat it as a security incident until shown otherwise.
The job's `result` holds the first broken entry (`SELECT result FROM jobs WHERE type = 'audit_verify' ORDER BY id
DESC LIMIT 1`), and Scribe logs `audit chain broken` with its `seq`, Domain and reason. Do not repair the rows:
preserve the database (a `pg_dump` of `audit_log`), compare the entry with a backup to see what changed, and check
who holds write access to `audit_log` (the maintenance role and the database owner).
`python -m lorebase_keeper audit verify --all` reruns the check ([audit chain verification](#audit-chain-verification)).
The alert keeps firing while the chain stays broken, because each daily job reports it again.
