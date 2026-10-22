# Metrics catalogue

Every metric Lorebase emits, with the Prometheus series it becomes, its labels and where in the
code it is recorded. The dashboards and alert rules in `deploy/observability` use only the series
listed here; `tests/tooling/test_observability_bundle.py` fails when they name another one, and
when an instrument in `packages/*/src` is missing from the tables.

Services export OpenTelemetry metrics over OTLP/gRPC to the collector when `telemetry.otlp_endpoint`
is set (design 14; with it unset nothing is created). The collector's Prometheus exporter
(`deploy/observability/otel-collector.yaml`, port 8889) is what Prometheus scrapes. Traces go to
Jaeger or Tempo through the same collector.

**Name conversion.** Dots become underscores; the unit is appended (`s` becomes `_seconds`, `ms`
becomes `_milliseconds`; counted units such as `{job}` add nothing); counters end in `_total`;
histograms add `_bucket`, `_sum` and `_count`. Attribute names follow the same dot rule
(`http.route` is `http_route`). With `resource_to_telemetry_conversion` every series also carries
`service_name` (`telemetry.service_name` of the process) and `service_namespace`.

## Keeper

Meter `lorebase.keeper`.

| OpenTelemetry name | Prometheus series | Type | Unit | Labels | Meaning | Emitted by |
|--------------------|-------------------|------|------|--------|---------|------------|
| `http.server.request.duration` | `http_server_request_duration_seconds` (`_bucket`, `_sum`, `_count`) | histogram | s | `http_request_method`, `http_route`, `http_response_status_code` | Duration of every HTTP request until its response is complete (health checks included). `http_route` is the route template (`/domains/{domain_id}/...`), `unmatched` when no route matched. Its `_count` is the request count. | `lorebase_keeper.middleware.RequestContextMiddleware` |
| `lorebase.http.rate_limited` | `lorebase_http_rate_limited_total` | counter | `{request}` | `class` | Requests refused with 429 because a token bucket was empty. `class` is `reads`, `writes`, `searches`, `context`, `admin`, or `auth_failures` (the bucket of failed authentications, see [operations](operations.md#rate-limits-and-quotas)). | `lorebase_keeper.ratelimit.limiter.RateLimiter` |
| `lorebase.http.quota_exceeded` | `lorebase_http_quota_exceeded_total` | counter | `{request}` | `quota` | Requests refused with 429 because an API key used up a daily quota. `quota` is `requests` or `context_tokens`. | `lorebase_keeper.ratelimit.quotas.QuotaTracker` |
| `lorebase.audit.write_failures` | `lorebase_audit_write_failures_total` | counter | `{entry}` | `reason` | Audit entries that could not be written (timeout or database error). `reason` is the exception class. | `lorebase_keeper.auth.audit.AuditWriter` |
| `lorebase.audit.reads.recorded` | `lorebase_audit_reads_recorded_total` | counter | `{entry}` | none | Read records written to `audit_read_log` (`keeper.auth.audit.reads`). | `lorebase_keeper.auth.read_audit.ReadAuditor` |
| `lorebase.audit.reads.dropped` | `lorebase_audit_reads_dropped_total` | counter | `{entry}` | `reason`, `sensitive` | Read records dropped: `queue_full`, `write_failed` (a batch failed or timed out, after a retry when it held a sensitive read) or `shutdown`; `sensitive` is `true` for reads that showed or masked a sensitive value. | `lorebase_keeper.auth.read_audit.ReadAuditor` |
| `lorebase.trace.steps.recorded` | `lorebase_trace_steps_recorded_total` | counter | `{step}` | none | Agent trace steps written to `agent_trace_steps` (`keeper.trace`). | `lorebase_keeper.tracing.recorder.TraceRecorder` |
| `lorebase.trace.steps.dropped` | `lorebase_trace_steps_dropped_total` | counter | `{step}` | `reason` | Agent trace steps dropped: `queue_full`, `write_failed`, `session_full` (over `max_steps_per_session`) or `shutdown`. | `lorebase_keeper.tracing.recorder.TraceRecorder` |
| `lorebase.ratelimit.shared.queries` | `lorebase_ratelimit_shared_queries_total` | counter | `{query}` | `store`, `outcome` | Queries to the shared rate limit buckets (`backend: postgres`). `store` is `principals` or `auth_failures`; `outcome` is `ok`, `stale` (a failed-authentication check decided from a recent reading during an outage), `timeout` or the exception class of a failure. | `lorebase_keeper.ratelimit.shared.PostgresRateStore` |
| `lorebase.db.reads` | `lorebase_db_reads_total` | counter | none | `source`, `reason` | Reads by the database that served them. `source` is `primary` or `replica`. `reason` is why: `replica_fresh` (a replica served it), `no_replicas`, `not_preferred`, `stale`, `lagging`, `unreachable`. | `lorebase_keeper.db.create_database` (route callback of `lorebase_store.replicas.ReadSessionFactory`) |
| `lorebase.db.replica.lag` | `lorebase_db_replica_lag_milliseconds` | gauge | ms | `replica` | Replay lag of each read replica at its latest probe. Absent while a replica has not answered a probe, and when no replica is configured (`database.read_dsn`). | `lorebase_keeper.db._observe_lag` |
| `lorebase.db.replica.up` | `lorebase_db_replica_up` | gauge | none | `replica` | 1 when the replica answered its latest probe, 0 otherwise. Absent when no replica is configured. | `lorebase_keeper.db._observe_lag` |
| `lorebase.keeper.cache.requests` | `lorebase_keeper_cache_requests_total` | counter | `{request}` | `result` | Lookups of the read response cache by GET reads (a 304 answered from `If-None-Match` is not counted). `result` is `hit`, `miss` or `bypass` (read response cache disabled). | `lorebase_keeper.reading.caching.respond_with` |
| `lorebase.auth.failures` | `lorebase_auth_failures_total` | counter | `{request}` | `reason` | Requests whose authentication failed. `reason` is `missing` (no credential), `malformed` (unsupported `Authorization` header, or a token not shaped like a key), `method_not_accepted` (API keys or bearer tokens not enabled), `unknown_key`, `wrong_secret`, `revoked`, `expired`, `network` (client address outside the key's allowed networks), `principal_missing`, `invalid_token` (OIDC token rejected) or `no_account` (valid token, no account and no auto provisioning; answered 403). | `lorebase_keeper.auth.principals.Authenticator` |

The refusal of clients that keep failing to authenticate is `lorebase_http_rate_limited_total{class="auth_failures"}`;
it counts the requests answered 429, while `lorebase_auth_failures_total` counts the failed authentications.

### Route classes

The latency histogram carries the route, not the rate limit class. The dashboards and alerts
rebuild the classes from the method and the route suffix, with the same defaults as
`keeper.limits.rate.routes`:

| Class | Selector |
|-------|----------|
| `reads` | method `GET`, `HEAD` or `OPTIONS` (health checks excluded) |
| `queries` | `POST` to a route ending in `/query`, `/traverse` or `/neighborhood` (rate limit class `reads`) |
| `searches` | `POST` to a route ending in `/search` |
| `context` | `POST` to a route ending in `/context` |
| `writes` | any other `POST`, `PUT`, `PATCH` or `DELETE` |

`admin` is chosen by OpenAPI tag and cannot be told apart in this metric. If you change the
suffixes in `keeper.limits.rate.routes`, change the selectors in the dashboards and rules too.

## Store

Meter `lorebase.store` (`lorebase_store.telemetry`). The store code runs in whichever process
calls it: Keeper records commits, merges and reads, Scribe the commits of its jobs (Source
syncs, extraction), job claims, garbage collection and webhook deliveries. Both export the series, so sum over `service_name` for the installation total.

| OpenTelemetry name | Prometheus series | Type | Unit | Labels | Meaning | Emitted by |
|--------------------|-------------------|------|------|--------|---------|------------|
| `lorebase.store.commit.duration` | `lorebase_store_commit_duration_seconds` (`_bucket`, `_sum`, `_count`) | histogram | s | `origin` | Time of a successful commit through the commit pipeline (lock, validation, tree, Revision, materialization, ref swap, events). It does not include the caller's final transaction commit when the pipeline runs inside the caller's transaction. `origin` is the origin type of the Revision: `operation`, `source_sync`, `extraction`, `schema_change`, `import`, and so on. | `lorebase_store.commit.pipeline.commit` |
| `lorebase.store.merge.attempts` | `lorebase_store_merge_attempts_total` | counter | `{attempt}` | `result` | Merge runs of a Proposal (`POST` merge) by outcome. `result` is `merged`, `fast_forward`, `up_to_date` or `conflicts` (open Conflicts left, nothing written). | `lorebase_store.merging.service.MergeService.merge` |
| `lorebase.store.reads.definer` | `lorebase_store_reads_definer_total` | counter | `{call}` | `function` | Calls of the `SECURITY DEFINER` read functions, only when `store.reads.use_definer_functions` is on. `function` is `lb_search`, `lb_links_in`, `lb_neighborhood`, `lb_query_by_attribute` or one of their `_overlay` variants. | `lorebase_store.reads`, `lorebase_store.traverse` |
| `lorebase.jobs.claimed` | `lorebase_jobs_claimed_total` | counter | `{job}` | `type` | Jobs claimed by workers from the queue. | `lorebase_store.jobs.claim_jobs` |
| `lorebase.webhooks.deliveries` | `lorebase_webhooks_deliveries_total` | counter | `{delivery}` | `outcome` | Webhook delivery attempts by outcome: `delivered`, `retrying` (failed, another attempt is scheduled) or `dead` (attempts used up, or the target was refused). | `lorebase_store.webhooks.delivery.deliver_pending` |
| `lorebase.outbox.handoffs` | `lorebase_outbox_handoffs_total` | counter | `{event}` | `consumer` | Outbox events a consumer handed off (for webhooks: fanned out into deliveries) and no longer has pending. | `lorebase_store.webhooks.delivery.deliver_pending` |
| `lorebase.outbox.pruned` | `lorebase_outbox_pruned_total` | counter | `{event}` | `reason` | Outbox events deleted. `reason` is `retention` (handed off by every consumer and older than `store.outbox.retention_s`) or `expired` (older than `store.outbox.max_age_s` while a consumer still had them pending: that consumer lost them). | `lorebase_store.webhooks.delivery.deliver_pending` |
| `lorebase.outbox.prune.backlog` | `lorebase_outbox_prune_backlog` | gauge | `{event}` | | Outbox events still eligible for pruning (see `lorebase.outbox.pruned`) after the latest pruning pass of the process, counted up to 100000 (`PRUNE_BACKLOG_CAP`), so a larger backlog reads 100000. Absent until the first pass. | `lorebase_store.webhooks.delivery.deliver_pending` |
| `lorebase.operations.events` | `lorebase_operations_events_total` | counter | `{event}` | | Operation side effect events written to the outbox by committed executions. | `lorebase_keeper.services.operations.execute` |
| `lorebase.webhooks.timeouts` | `lorebase_webhooks_timeouts_total` | counter | `{attempt}` | | Webhook delivery attempts cut off by `webhooks.timeout_s` (receiver gone, silent, dropping packets or slow); each is also counted in `lorebase.webhooks.deliveries` as `retrying` or `dead`. | `lorebase_store.webhooks.delivery.deliver_pending` |
| `lorebase.store.branch_jobs.rows` | `lorebase_store_branch_jobs_rows_total` | counter | `{row}` | `job` | Rows read by the background branch jobs ([operations](operations.md#large-branches)). `job` is `materialize` (base rows a copy went through), `delete` (rows of a deleted branch removed) or `sensitivity_refresh` (Records whose title and search vector were checked, `lorebase_store.materialize.stale`). | `lorebase_store.materialize.background` |
| `lorebase.store.branch_jobs.runs` | `lorebase_store_branch_jobs_runs_total` | counter | `{run}` | `job`, `outcome` | Runs of the background branch jobs. `outcome` is `done`, `deferred` (no free slot of `store.materialize.job_concurrency`, or the branch stayed locked; queued again), `waiting` (a deletion waits for the copies of overlays based on the branch; queued again), `skipped` (another job works on the branch), `cancelled` (the branch is gone, or is not being deleted) or `failed` (a run raised); for copies also `retried` (its job used every attempt; queued again with backoff), `fallback` (failed `copy_max_failures` times: the overlay was detached and is built from its tree) and `given_up` (a build failed as often; the branch stays `building`). | `lorebase_store.materialize.background` |
| `lorebase.store.branch_jobs.batch.rows` | `lorebase_store_branch_jobs_batch_rows` (`_bucket`, `_sum`, `_count`) | histogram | `{row}` | `job` | Rows a batch of a background branch job was sized for: between `job_batch_min_rows` and `job_batch_rows`, smaller while commits wait for the branch lock. | `lorebase_store.materialize.background` |
| `lorebase.store.branch_jobs.batch.duration` | `lorebase_store_branch_jobs_batch_duration_seconds` (`_bucket`, `_sum`, `_count`) | histogram | s | `job` | One batch transaction of a background branch job. Batches close to `database.statement_timeout_ms` call for a lower `store.materialize.job_batch_rows`. | `lorebase_store.materialize.background` |
| `lorebase.gc.runs` | `lorebase_gc_runs_total` | counter | `{run}` | `dry_run` | Completed garbage collections of a Domain. `dry_run` is `true` or `false`. | `lorebase_store.gc.collect_garbage` |
| `lorebase.gc.freed_bytes` | `lorebase_gc_freed_bytes_total` | counter | `By` | `content` | Bytes of tree nodes and objects deleted by garbage collection; dry runs add nothing. `content` is `nodes` or `objects`. The name already ends in `bytes`, so the conversion adds nothing to it. | `lorebase_store.gc.collect_garbage` |

## Scribe

Meter `lorebase.scribe` (`lorebase_scribe.telemetry`).

| OpenTelemetry name | Prometheus series | Type | Unit | Labels | Meaning | Emitted by |
|--------------------|-------------------|------|------|--------|---------|------------|
| `lorebase.scribe.jobs` | `lorebase_scribe_jobs_total` | counter | `{job}` | `type`, `status` | Job runs. `type` is `source_sync`, `webhook_delivery`, `store.index`, `store.materialize`, `store.branch_delete`, `gc`, `extraction`, `embeddings`, `audit_verify` or `audit_checkpoint`. `status` is the state after the run: `succeeded`, `queued` (failed and requeued for another attempt: a retry) or `failed` (attempts used up, or a permanent error). | `lorebase_scribe.worker.Worker` |
| `lorebase.scribe.job.duration` | `lorebase_scribe_job_duration_seconds` (`_bucket`, `_sum`, `_count`) | histogram | s | `type` | Wall time of one run of a job, including the write of its outcome. | `lorebase_scribe.worker.Worker` |
| `lorebase.scribe.scheduled` | `lorebase_scribe_scheduled_total` | counter | `{job}` | none | Jobs the scheduler queued in one tick (the webhook delivery pass, scheduled Source syncs, garbage collection, embedding passes). Jobs queued by the API are not counted. | `lorebase_scribe.scheduler.Scheduler` |
| `lorebase.gc.scheduled` | `lorebase_gc_scheduled_total` | counter | `{job}` | `dry_run` | `gc` jobs the scheduler queued at `scribe.scheduler.gc_schedule`, one per Domain and firing. `dry_run` follows `scribe.scheduler.gc_dry_run` (`true` or `false`). The series starts at 0 on the leader while a schedule is set, and is absent when `gc_schedule` is null. Collections queued by hand are not counted. | `lorebase_scribe.scheduler.Scheduler` |
| `lorebase.scribe.source.rows` | `lorebase_scribe_source_rows_total` | counter | `{row}` | `lorebase_source` | Rows read from a Source by sync runs. | `lorebase_scribe.sources.sync.SourceSync` |
| `lorebase.scribe.source.changes` | `lorebase_scribe_source_changes_total` | counter | `{change}` | `lorebase_source`, `dry_run` | Data changes a Source run computed against the Domain (committed unless `dry_run` is `true`). | `lorebase_scribe.sources.sync.SourceSync` |
| `lorebase.jobs.queue_depth` | `lorebase_jobs_queue_depth` | gauge | `{job}` | `type`, `status` | Rows of the `jobs` table waiting or running, counted by the scheduler leader at each tick (`scribe.scheduler.tick_s`). `status` is `queued` (claimable now), `retrying` (queued with a `run_after` still ahead: a failed job waiting for `store.jobs.retry_delay_seconds`, or a job deferred to later) or `running`. Only the leader reports it, so it is absent when no Scribe holds leadership; a type and status pair that empties reports 0. | `lorebase_scribe.scheduler.Scheduler` |
| `lorebase.extraction.items` | `lorebase_extraction_items_total` | counter | `{item}` | `tier`, `decision` | Items an extraction job proposes. `tier` is `0`, `1`, `2` or `none` (items without a tier). `decision` is `accepted` (accepted at once by the confidence rules) or `pending` (left for review). | `lorebase_scribe.extraction.pipeline` |
| `lorebase.scribe.embeddings.records` | `lorebase_scribe_embeddings_records_total` | counter | `{record}` | `outcome` | Records an embedding pass rendered. `outcome` is `embedded` (sent to the model), `reused` (the vector of an identical text was copied), `unchanged` (the stored vector has the same text) or `empty` (the template rendered nothing). | `lorebase_scribe.embeddings.EmbeddingPass` |
| `lorebase.scribe.embeddings.call.duration` | `lorebase_scribe_embeddings_call_duration_seconds` (`_bucket`, `_sum`, `_count`) | histogram | s | none | Duration of one call of the embedding model by an embedding job, failed attempts included. | `lorebase_scribe.embeddings.EmbeddingPass` |
| `lorebase.audit.verifications` | `lorebase_audit_verifications_total` | counter | `{verification}` | `outcome`, `stream` | Verifications of every audit hash chain by `audit_verify` jobs. `outcome` is `intact` or `broken` (an entry does not verify; the job result names the first one); `stream` is `actions` or `reads`. | `lorebase_scribe.jobs` |
| `lorebase.audit.checkpoints` | `lorebase_audit_checkpoints_total` | counter | `{checkpoint}` | `outcome` | Audit checkpoints of `audit_checkpoint` jobs: `published` (one per stream) or `failed` (a job attempt that stored none). | `lorebase_scribe.jobs` |

Job counts per type answer most questions about the work: runs of `source_sync` are Source sync
runs, `extraction` is extraction throughput, `webhook_delivery` is the delivery pass, `gc` is
garbage collection, `embeddings` the embedding passes (a pass larger than
`scribe.embeddings.max_records_per_job` runs as several jobs). A retry is a `queued` run.

## Guide

Meter `lorebase.guide` (`lorebase_guide.telemetry`), exported by the optional Guide service.

| OpenTelemetry name | Prometheus series | Type | Unit | Labels | Meaning | Emitted by |
|--------------------|-------------------|------|------|--------|---------|------------|
| `lorebase.guide.requests` | `lorebase_guide_requests_total` | counter | `{request}` | `route`, `status` | HTTP requests answered by Guide. | `lorebase_guide.app` |
| `lorebase.guide.request.duration` | `lorebase_guide_request_duration_seconds` (`_bucket`, `_sum`, `_count`) | histogram | s | `route` | Duration of one HTTP request; for `/explore`, until the stream ends. | `lorebase_guide.app` |
| `lorebase.guide.explorations` | `lorebase_guide_explorations_total` | counter | `{exploration}` | `outcome` | Explorations by outcome (`answered`, `not_found`, `budget`, `timeout`, `model`, ...). | `lorebase_guide.app` |
| `lorebase.guide.steps` | `lorebase_guide_steps_total` | counter | `{call}` | `tool`, `ok` | Keeper reads the model asked for. | `lorebase_guide.agent.Explorer` |
| `lorebase.guide.tokens` | `lorebase_guide_tokens_total` | counter | `{token}` | `direction` | Model tokens, `input` or `output`. | `lorebase_guide.agent.Explorer` |
| `lorebase.guide.embeddings` | `lorebase_guide_embeddings_total` | counter | `{text}` | `mode` | Texts embedded for the web app's semantic search. | `lorebase_guide.app` |
| `lorebase.guide.refused` | `lorebase_guide_refused_total` | counter | `{request}` | `reason` | Requests refused before any work (`auth`, `origin`, `rate`, `busy`, ...). | `lorebase_guide.app` |

## Spans

Traces use the same pipeline. Instrumentation names `lorebase.keeper` and `lorebase.scribe`.

| Span | Attributes | Emitted by |
|------|------------|------------|
| `{METHOD} {route}` (server span of each HTTP request; the W3C trace context of the caller is continued) | `http.request.method`, `url.path`, `http.route`, `http.response.status_code`, `lorebase.request_id`; status error on 5xx | `lorebase_keeper.middleware.RequestContextMiddleware` |
| `scribe.job {type}` | `lorebase.job.type`, `lorebase.job.id`, `lorebase.domain`, `lorebase.job.status`; error status and the exception when the attempt fails | `lorebase_scribe.worker.Worker` |
| `scribe.source_sync` | `lorebase.domain`, `lorebase.source`, `lorebase.run`, `lorebase.dry_run`, `lorebase.changes` | `lorebase_scribe.sources.sync.SourceSync` |
| `scribe.scheduler.tick` | none | `lorebase_scribe.scheduler.Scheduler` |
| `scribe.embeddings` | `lorebase.domain`, `lorebase.complete` | `lorebase_scribe.embeddings.EmbeddingPass` |

The spans of design 14 beyond these (authentication, query compile, SQL, commit and merge phases,
extraction steps) are not emitted yet. Herald emits no telemetry of its own.

## Histogram buckets

The latency histograms (`http_server_request_duration_seconds`, `lorebase_scribe_job_duration_seconds`
and `lorebase_store_commit_duration_seconds`) are recorded in seconds with explicit boundaries:
0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5 and 10 (`lorebase_platform.telemetry.LATENCY_BUCKETS_S`,
declared through `explicit_bucket_boundaries_advisory`). Everything slower than 10 seconds lands in the
`+Inf` bucket, where `histogram_quantile` returns the highest finite boundary. The p50 and p95 panels of
the dashboards and the latency alerts are meaningful between 5 ms and 10 s, and every budget they use
(100 ms, 250 ms, 1 s, 2.5 s) is a boundary.

## Not emitted yet

Design 14 lists signals that no code records, and the dashboards and alerts do not cover them:
database pool usage, extraction tokens and cost, changes per commit, Conflicts by code, schema cache
hits, and spans beyond those listed above.
