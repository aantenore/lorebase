"""OpenTelemetry instruments of the store. They are no-ops until the process installs
providers (`lorebase_platform.telemetry.setup_telemetry`, `telemetry.otlp_endpoint` set); the
process running the store code (Keeper or Scribe) exports them under its own service name."""

from __future__ import annotations

from collections.abc import Iterable
from threading import Lock

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation

from lorebase_platform.telemetry import LATENCY_BUCKETS_S

INSTRUMENTATION = "lorebase.store"

meter = metrics.get_meter(INSTRUMENTATION)

COMMIT_DURATION = meter.create_histogram(
    "lorebase.store.commit.duration",
    unit="s",
    description="Duration of a successful commit through the commit pipeline, by origin.",
    explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
)
MERGE_ATTEMPTS = meter.create_counter(
    "lorebase.store.merge.attempts",
    unit="{attempt}",
    description="Merges of a Proposal, by result (merged, fast_forward, up_to_date, conflicts).",
)
DEFINER_READS = meter.create_counter(
    "lorebase.store.reads.definer",
    unit="{call}",
    description="Calls of the security definer read functions, by function.",
)
JOBS_CLAIMED = meter.create_counter(
    "lorebase.jobs.claimed", unit="{job}", description="Jobs claimed from the queue, by type."
)
WEBHOOK_OUTCOMES = meter.create_counter(
    "lorebase.webhooks.deliveries",
    unit="{delivery}",
    description="Webhook delivery attempts, by outcome (delivered, retrying, dead).",
)
WEBHOOK_TIMEOUTS = meter.create_counter(
    "lorebase.webhooks.timeouts",
    unit="{attempt}",
    description="Webhook delivery attempts cut off by webhooks.timeout_s.",
)
OUTBOX_HANDOFFS = meter.create_counter(
    "lorebase.outbox.handoffs",
    unit="{event}",
    description="Outbox events handed off by a consumer, by consumer.",
)
OUTBOX_PRUNED = meter.create_counter(
    "lorebase.outbox.pruned",
    unit="{event}",
    description="Outbox events deleted, by reason (retention: handed off by every consumer; "
    "expired: older than store.outbox.max_age_s while still pending).",
)


class PruneBacklog:
    """Outbox events still eligible for pruning after the latest pruning pass of this process
    (capped, see `OutboxRepository.prunable`); nothing is reported before the first pass."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._value: int | None = None

    def set(self, value: int) -> None:
        with self._lock:
            self._value = value

    def observe(self, _options: CallbackOptions | None = None) -> Iterable[Observation]:
        with self._lock:
            value = self._value
        return [] if value is None else [Observation(value)]


OUTBOX_PRUNE_BACKLOG = PruneBacklog()
meter.create_observable_gauge(
    "lorebase.outbox.prune.backlog",
    callbacks=[OUTBOX_PRUNE_BACKLOG.observe],
    unit="{event}",
    description="Outbox events eligible for pruning left after the latest pruning pass "
    "(counted up to a cap).",
)
OPERATION_EVENTS = meter.create_counter(
    "lorebase.operations.events",
    unit="{event}",
    description="Operation side effect events written to the outbox with a committed execution.",
)
GC_RUNS = meter.create_counter(
    "lorebase.gc.runs", unit="{run}", description="Completed garbage collections of a Domain."
)
GC_FREED_BYTES = meter.create_counter(
    "lorebase.gc.freed_bytes",
    unit="By",
    description="Bytes of tree nodes and objects deleted by garbage collection.",
)

BRANCH_JOB_ROWS = meter.create_counter(
    "lorebase.store.branch_jobs.rows",
    unit="{row}",
    description="Rows read by background branch jobs, by job (materialize: base rows copied "
    "to a branch, delete: rows of a deleted branch removed, sensitivity_refresh: Records whose "
    "title and search vector were checked).",
)
BRANCH_JOB_RUNS = meter.create_counter(
    "lorebase.store.branch_jobs.runs",
    unit="{run}",
    description="Runs of background branch jobs, by job and outcome (done, deferred, waiting, "
    "skipped, cancelled, failed; for copies also retried: failed for good, queued again "
    "with backoff; fallback: the overlay was detached and is rebuilt from its tree; "
    "given_up: no attempt left).",
)
BRANCH_JOB_BATCH_ROWS = meter.create_histogram(
    "lorebase.store.branch_jobs.batch.rows",
    unit="{row}",
    description="Rows a batch of a background branch job was sized for, by job: batches "
    "shrink while commits wait for the branch lock and grow back when none does.",
)
BRANCH_JOB_BATCH_DURATION = meter.create_histogram(
    "lorebase.store.branch_jobs.batch.duration",
    unit="s",
    description="Duration of one batch transaction of a background branch job, by job.",
    explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
)


def record_definer_call(function: str) -> None:
    DEFINER_READS.add(1, {"function": function})
