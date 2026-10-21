# Operations

Routine maintenance of the store: garbage collection, and backup and restore of a Domain. The
commands read the database and `store.*` settings from the Lorebase configuration
(`LOREBASE_CONFIG`, `LOREBASE__DATABASE__DSN`, see `config/lorebase.example.yaml`).

## Garbage collection

Objects, tree nodes and Revisions are immutable, so deleting a branch only removes its ref.
Collection reclaims what no ref reaches any more (design 6.13). Per Domain, it marks every
Revision reachable from a branch or tag, from an open Proposal's merge attempts, or created less
than `store.gc.grace_seconds` ago (default one hour), then the tree nodes and objects those
Revisions reach. It sweeps the rest in batches: unreachable Revisions with their parents,
changesets and history rows, provenance entries nothing refers to any more, tree nodes and
objects. Branch deletion is the usual source of garbage; ref log entries stay and may name
Revisions that were collected.

```
python -m lorebase_store.gc --domain supply --dry-run     # counts and bytes, deletes nothing
python -m lorebase_store.gc --domain supply               # collect
python -m lorebase_store.gc --all --grace-seconds 86400   # every Domain, longer grace
python -m lorebase_store.gc --domain supply --enqueue     # queue a `gc` job instead
python -m lorebase_store.jobs work                        # run queued jobs (SKIP LOCKED)
```

Scribe runs queued `gc` jobs and queues one per Domain at `scribe.scheduler.gc_schedule`
([scribe](scribe.md)).

The report is one JSON object per Domain (`revisions`, `changesets`, `history_rows`,
`provenance`, `nodes`, `node_bytes`, `objects`, `object_bytes`, mark and sweep seconds). In
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
- The target Domain must not exist, or must have no Revisions and no refs. Revision ids include
  the Domain id, so a bundle can only be restored under the id it was exported with.
- Unreachable data (collected or not yet collected garbage) is not exported, so restoring a
  bundle also compacts the Domain.

Not in a bundle: the audit log, principals, groups, grants and API keys (installation-wide, not
Domain content), and per-Domain operational tables: Proposals and reviews, merge attempts,
Source runs, documents and chunks, extraction jobs, vault entries, embeddings, Operation
invocations, outbox and jobs. Back these up with `pg_dump`, which also covers a whole
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
