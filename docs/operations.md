# Operations

Routine maintenance of the store: garbage collection, and backup and restore of a Domain. The
commands read the database and `store.*` settings from the Lorebase configuration
(`LOREBASE_CONFIG`, `LOREBASE__DATABASE__DSN`, see `config/lorebase.example.yaml`).

## Garbage collection

Objects, tree nodes and Revisions are immutable, so deleting a branch only removes its ref (and
its materialized rows, in the background for a large branch: see [Large branches](#large-branches)).
Collection reclaims what no ref reaches any more (design 6.13). Per Domain, it marks every
Revision reachable from a branch or tag, from an open Proposal's merge attempts, or created less
than `store.gc.grace_seconds` ago (default one hour), then the tree nodes and objects those
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
branch, or of an overlay branch whose base moved too far) and `store.branch_delete` (the rows of
a large deleted branch) jobs by default (`--types` narrows it; see [Large
branches](#large-branches)). Index jobs
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
  into the branch, waits for at most one batch. Raise `job_batch_rows` to copy faster, lower it
  when batches come close to the timeout or commits wait too long
  (`lorebase.store.branch_jobs.batch.duration`, [metrics](metrics.md#store)).
- **Concurrency.** `job_concurrency` bounds the write load of copies and deletions on the
  database; a job that finds no free slot is queued again after `job_retry_seconds` and counted
  as `deferred` in `lorebase.store.branch_jobs.runs`.
- **While a copy runs.** The branch answers with `materialization: overlay`. Schema changes on
  it are refused with 409 `branch-materializing` and `Retry-After` until the copy is done; data
  commits, merges and reads work, with overlay performance. It counts against
  `keeper.branches.max_materialized_per_domain`.
- **A failed copy** (`failed` after `store.jobs.max_attempts`, with the error) leaves a correct
  overlay. Fix the cause (usually the statement timeout: lower `job_batch_rows`), then queue the
  copy again; it resumes from its cursor:

  ```sql
  INSERT INTO jobs (type, domain_id, payload) VALUES ('store.materialize', 'supply', '{"branch": "feature"}');
  ```

- **While a deletion runs.** The name stays taken: creating a branch with it answers 409
  `already-exists` with `Retry-After`. A deleted branch that other overlays are based on waits
  for their background copies first (a copy that keeps failing keeps it waiting) (`waiting` in `lorebase.store.branch_jobs.runs`). Hidden
  branches still being deleted: `SELECT name, deleted_at FROM refs WHERE deleted_at IS NOT
  NULL`. Their heads stay garbage collection roots until the job ends.

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
recomputed), or when a file is not a checkpoint signed by that key; `--json` lists them under
`checkpoints`. Entries newer than the latest checkpoint are not covered.

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
  (`queue_full`, `write_failed`, `shutdown`); written ones count in `lorebase.audit.reads.recorded`.
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
  checked from a reading at most `check_cache_s` old. When a query does not answer within
  `timeout_s`, failed-authentication checks answer `429` (fail closed) and the other buckets
  follow `on_error`: `local` (default, the in-process buckets meanwhile), `allow` or `deny`;
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

Jobs have been queued (`lorebase_jobs_queue_depth{status="queued"}`, reported by the scheduler leader) while no
worker claimed one (`lorebase_jobs_claimed_total`) in the last 15 minutes. Scribe workers are not running, cannot
claim (database down), or every job hangs. The Scribe dashboard shows the queue depth by type. Check the Scribe pods
and logs, `SELECT type, status, count(*) FROM jobs GROUP BY 1, 2`, and rows locked for longer than `store.jobs.stale_after_seconds` (the scheduler requeues those).
`python -m lorebase_store.jobs work` drains the queue from a shell.

#### LorebaseWebhookDeliveryFailing

Deliveries became dead in the last hour (`lorebase_webhooks_deliveries_total{outcome="dead"}`); the Scribe
dashboard shows `delivered`, `retrying` and `dead` side by side. Dead letters are rows of `webhook_deliveries`:
`SELECT webhook_id, count(*), max(last_error) FROM webhook_deliveries WHERE status = 'dead' GROUP BY 1`. A target
refused (scheme, private address) is dead at once; others after `webhooks.max_attempts`. Fix or disable the
webhook; the pass retries what is still pending.

The outbox keeps a backlog per consumer (`outbox_pending`): `SELECT consumer, count(*) FROM
outbox_pending GROUP BY 1`. Events leave the outbox once every consumer handed them off and they
are older than `store.outbox.retention_s` (7 days), or after `store.outbox.max_age_s` (30 days)
in any case; the latter are counted in `lorebase_outbox_pruned_total{reason="expired"}` and
logged. To stop keeping events for a consumer that no longer runs, delete its row from
`outbox_consumers` (its pending rows go with it).

A receiver that stopped answering does not hold Scribe: each attempt is cut off after `webhooks.timeout_s`
(`last_error` reads `timed out after 10s`, counted in `lorebase_webhooks_timeouts_total`), a pass starts no attempt
after `webhooks.pass_timeout_s` and releases the rest to the next pass (`released` in the job result), and a pass
still running after `webhooks.lease_s` is abandoned as a failed job attempt, since its deliveries may then be sent
by another pass. Many timeouts with a steady `released` count mean slow receivers: raise
`webhooks.concurrency` or fix the receiver.

#### LorebaseGcNotRun

No garbage collection completed in 3 days (`lorebase_gc_runs_total`, dry runs excluded) although Scribe is
scheduling work. Check `scribe.scheduler.gc_schedule` (null disables the schedule; change the window in the rule if
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
