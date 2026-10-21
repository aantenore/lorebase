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
| `lorebase.db.reads` | `lorebase_db_reads_total` | counter | none | `source`, `reason` | Reads by the database that served them. `source` is `primary` or `replica`. `reason` is why: `replica_fresh` (a replica served it), `no_replicas`, `not_preferred`, `stale`, `lagging`, `unreachable`. | `lorebase_keeper.db.create_database` (route callback of `lorebase_store.replicas.ReadSessionFactory`) |
| `lorebase.db.replica.lag` | `lorebase_db_replica_lag_milliseconds` | gauge | ms | `replica` | Replay lag of each read replica at its latest probe. Absent while a replica has not answered a probe, and when no replica is configured (`database.read_dsn`). | `lorebase_keeper.db._observe_lag` |
| `lorebase.db.replica.up` | `lorebase_db_replica_up` | gauge | none | `replica` | 1 when the replica answered its latest probe, 0 otherwise. Absent when no replica is configured. | `lorebase_keeper.db._observe_lag` |

There is no authentication failure counter of its own. Failed authentications are the 401 and 403
rows of `http_server_request_duration_seconds_count`, and the refusal of clients that keep failing
is `lorebase_http_rate_limited_total{class="auth_failures"}`.

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

## Scribe

Meter `lorebase.scribe` (`lorebase_scribe.telemetry`).

| OpenTelemetry name | Prometheus series | Type | Unit | Labels | Meaning | Emitted by |
|--------------------|-------------------|------|------|--------|---------|------------|
| `lorebase.scribe.jobs` | `lorebase_scribe_jobs_total` | counter | `{job}` | `type`, `status` | Job runs. `type` is `source_sync`, `webhook_delivery`, `store.index`, `gc` or `extraction`. `status` is the state after the run: `succeeded`, `queued` (failed and requeued for another attempt: a retry) or `failed` (attempts used up, or a permanent error). | `lorebase_scribe.worker.Worker` |
| `lorebase.scribe.job.duration` | `lorebase_scribe_job_duration_seconds` (`_bucket`, `_sum`, `_count`) | histogram | s | `type` | Wall time of one run of a job, including the write of its outcome. | `lorebase_scribe.worker.Worker` |
| `lorebase.scribe.scheduled` | `lorebase_scribe_scheduled_total` | counter | `{job}` | none | Jobs the scheduler queued in one tick (the webhook delivery pass, scheduled Source syncs, garbage collection). Jobs queued by the API are not counted. | `lorebase_scribe.scheduler.Scheduler` |
| `lorebase.scribe.source.rows` | `lorebase_scribe_source_rows_total` | counter | `{row}` | `lorebase_source` | Rows read from a Source by sync runs. | `lorebase_scribe.sources.sync.SourceSync` |
| `lorebase.scribe.source.changes` | `lorebase_scribe_source_changes_total` | counter | `{change}` | `lorebase_source`, `dry_run` | Data changes a Source run computed against the Domain (committed unless `dry_run` is `true`). | `lorebase_scribe.sources.sync.SourceSync` |

Job counts per type answer most questions about the work: runs of `source_sync` are Source sync
runs, `extraction` is extraction throughput, `webhook_delivery` is the delivery pass, `gc` is
garbage collection. A retry is a `queued` run.

## Spans

Traces use the same pipeline. Instrumentation names `lorebase.keeper` and `lorebase.scribe`.

| Span | Attributes | Emitted by |
|------|------------|------------|
| `{METHOD} {route}` (server span of each HTTP request; the W3C trace context of the caller is continued) | `http.request.method`, `url.path`, `http.route`, `http.response.status_code`, `lorebase.request_id`; status error on 5xx | `lorebase_keeper.middleware.RequestContextMiddleware` |
| `scribe.job {type}` | `lorebase.job.type`, `lorebase.job.id`, `lorebase.domain`, `lorebase.job.status`; error status and the exception when the attempt fails | `lorebase_scribe.worker.Worker` |
| `scribe.source_sync` | `lorebase.domain`, `lorebase.source`, `lorebase.run`, `lorebase.dry_run`, `lorebase.changes` | `lorebase_scribe.sources.sync.SourceSync` |
| `scribe.scheduler.tick` | none | `lorebase_scribe.scheduler.Scheduler` |

The spans of design 14 beyond these (authentication, query compile, SQL, commit and merge phases,
extraction steps) are not emitted yet. Herald emits no telemetry of its own.

## Histogram buckets

Both histograms are recorded in seconds, but the instruments do not set bucket boundaries, so the
OpenTelemetry SDK uses its defaults: 0, 5, 10, 25, 50, 75, 100, 250, 500, 750, 1000, 2500, 5000,
7500, 10000. Every request faster than 5 seconds lands in the first bucket, so
`histogram_quantile` over these series reads close to 5 seconds whatever the real latency (the
mean, `_sum / _count`, is exact). The p50 and p95 panels are correct once the instruments declare
boundaries in seconds, for example `explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025,
0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]` on `create_histogram`. The latency alerts check that a
bucket exists at their budget (`le="0.1"`, `0.25`, `1`, `2.5`) and stay silent until it does.

## Not emitted yet

Design 14 lists signals that no code records. The dashboards do not chart them and no alert
depends on them; the nearest series is given.

| Signal | What to use meanwhile |
|--------|-----------------------|
| Commit duration and changes per commit | `writes` class latency (`http_server_request_duration_seconds`) |
| Merge attempts and Conflicts by code | requests to routes ending in `/merge` by `http_response_status_code` |
| Cache hit ratio (read response cache, schema cache) | none |
| `SECURITY DEFINER` function calls | none |
| Job queue depth and age | `lorebase_scribe_scheduled_total` against `lorebase_scribe_jobs_total` (alert `LorebaseJobQueueStalled`); queue rows in the `jobs` table |
| Jobs claimed | `lorebase_scribe_jobs_total` (runs, by outcome) |
| Webhook deliveries (sent, retried, dead) | `webhook_delivery` runs in `lorebase_scribe_jobs_total`; the `webhook_deliveries` table by `status` |
| Extraction items, tokens, cost | `extraction` runs in `lorebase_scribe_jobs_total` |
| Garbage collection reclaimed rows | the JSON report of the `gc` job; runs in `lorebase_scribe_jobs_total{type="gc"}` |
| Database pool usage | none |
