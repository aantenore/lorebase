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
python -m lorebase_store.indexes plan --domain supply     # pending per-Attribute index DDL
```

`jobs work` runs `gc` and `store.index` jobs by default (`--types` narrows it). Index jobs
build and drop per-Attribute indexes; they run as `database.maintenance_dsn` when it is set,
because their DDL needs a role that owns `mat_records` ([store schema
notes](store-schema-notes.md#physical-layout)). `indexes plan` prints that DDL for an
administrator where no such role is configured.

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
- `/healthz` and `/readyz` are exempt (`exempt_paths`), and so are requests without a valid
  credential: they are answered `401` before any bucket is touched.
- The limit counts `lorebase.http.rate_limited{class}` (OpenTelemetry) per refusal.
- A principal refused `throttle_audit.threshold` times within `window_s` produces one audit
  entry `rate_limit.throttled` (actor, Domain, API key as target, `refusals`, `reason`); the
  refused requests themselves are not audited one by one.
- The only backend is `memory`: buckets live in the process, so N Keeper processes allow N
  times the limit. A shared store (Valkey, Redis) is a class implementing
  `lorebase_keeper.ratelimit.RateStore.take`; nothing else changes.

**Daily quotas** apply to API keys, counted in UTC days: `requests_per_day` and
`context_tokens_per_day` (the estimated tokens of context packs delivered, using
`agents.context.chars_per_token`). A quota that is used up answers `429` with the problem type
`quota-exceeded`, `Retry-After` to the next UTC midnight and the members `quota`, `limit` and
`resets_at`. `keys.<api key id>` overrides the limits of one key (`null` removes a limit).
Use is kept in `api_key_usage` (migration `0005`): each process counts in memory and writes at
most every `flush_interval_s`, adding to the stored row, so quotas hold across restarts and
across processes up to one interval of traffic. If the database fails, requests are still
served and the quota is enforced on what is known. `GET /auth/whoami` shows a key's `quota`
with its use, remaining amounts and reset time.

## Client addresses and API key networks

An API key may carry `allowed_networks` (CIDR, IPv4 or IPv6; `apikey create --allow-network
203.0.113.0/24`, repeatable). From any other client address it is refused with the same `401`
as an invalid key, and Keeper logs `api key rejected` at INFO with the key prefix and the client
address. Keys without networks, including every key created before migration `0008`, work from
anywhere. Networks are set when the key is created (at most
`keeper.auth.api_keys.max_allowed_networks`); to change them create a new key and revoke the old.

The client address is the socket peer. When the peer is in `keeper.trusted_proxies`,
`X-Forwarded-For` is read from right to left, trusted hops are skipped and the first other
address is the client; entries further left are written by the client and never believed. A hop
that is not an address ends the walk with no client address, which no allowlist matches.
