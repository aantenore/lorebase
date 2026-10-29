"""OpenTelemetry instruments of Scribe. They are no-ops until `setup_telemetry` installs
providers (`telemetry.otlp_endpoint` set)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from threading import Lock

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation

from lorebase_platform.telemetry import LATENCY_BUCKETS_S

INSTRUMENTATION = "lorebase.scribe"

tracer = trace.get_tracer(INSTRUMENTATION)
meter = metrics.get_meter(INSTRUMENTATION)

JOBS = meter.create_counter(
    "lorebase.scribe.jobs", unit="{job}", description="Jobs run, by type and outcome."
)
JOB_DURATION = meter.create_histogram(
    "lorebase.scribe.job.duration",
    unit="s",
    description="Duration of one job run.",
    explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
)
SCHEDULED = meter.create_counter(
    "lorebase.scribe.scheduled", unit="{job}", description="Jobs queued by the scheduler."
)
GC_SCHEDULED = meter.create_counter(
    "lorebase.gc.scheduled",
    unit="{job}",
    description="gc jobs queued by the scheduler at gc_schedule, by dry_run. Starts at 0 when "
    "the schedule is set, so a schedule that queues nothing still reports its mode.",
)
SOURCE_ROWS = meter.create_counter(
    "lorebase.scribe.source.rows", unit="{row}", description="Source rows read."
)
SOURCE_CHANGES = meter.create_counter(
    "lorebase.scribe.source.changes",
    unit="{change}",
    description="Data changes computed by Source runs (committed unless dry_run).",
)
EXTRACTION_ITEMS = meter.create_counter(
    "lorebase.extraction.items",
    unit="{item}",
    description="Items proposed by extraction jobs, by tier and initial decision.",
)
EVIDENCE_WITHHELD = meter.create_counter(
    "lorebase.extraction.evidence_withheld",
    unit="{observation}",
    description="Schema suggestion observations kept without evidence because it held a "
    "sensitive value. For operators only, without labels.",
)
EMBEDDING_RECORDS = meter.create_counter(
    "lorebase.scribe.embeddings.records",
    unit="{record}",
    description="Records an embedding pass rendered, by outcome.",
)
EMBEDDING_CALL_DURATION = meter.create_histogram(
    "lorebase.scribe.embeddings.call.duration",
    unit="s",
    description="Duration of one call of the embedding model by an embedding job.",
    explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
)
AUDIT_VERIFICATIONS = meter.create_counter(
    "lorebase.audit.verifications",
    unit="{verification}",
    description="Verifications of the audit hash chains by audit_verify jobs, by outcome and "
    "stream.",
)
AUDIT_CHECKPOINTS = meter.create_counter(
    "lorebase.audit.checkpoints",
    unit="{checkpoint}",
    description="Audit checkpoints published by audit_checkpoint jobs, by outcome.",
)

RECONCILIATION_CANDIDATES = meter.create_counter(
    "lorebase.scribe.reconciliation.candidates",
    unit="{candidate}",
    description="Reconciliation candidates the detection stages produced, by type and outcome "
    "(raised, filtered, claim_added, obsolete).",
)
RECONCILIATION_SWEEP_DURATION = meter.create_histogram(
    "lorebase.scribe.reconciliation.sweep.duration",
    unit="s",
    description="Duration of one reconcile_sweep job.",
    explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
)


class QueueDepth:
    """The latest count of waiting and running jobs per (type, status), as the scheduler
    leader saw it. A pair that empties reports 0; after `clear` (leadership lost) nothing is
    reported, so only the leader's view is exported."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: dict[tuple[str, str], int] = {}

    def update(self, counts: Mapping[tuple[str, str], int]) -> None:
        with self._lock:
            self._counts = dict.fromkeys(self._counts, 0) | dict(counts)

    def clear(self) -> None:
        with self._lock:
            self._counts = {}

    def observe(self, _options: CallbackOptions | None = None) -> Iterable[Observation]:
        with self._lock:
            counts = dict(self._counts)
        return [
            Observation(count, {"type": job_type, "status": status})
            for (job_type, status), count in sorted(counts.items())
        ]


QUEUE_DEPTH = QueueDepth()
meter.create_observable_gauge(
    "lorebase.jobs.queue_depth",
    callbacks=[QUEUE_DEPTH.observe],
    unit="{job}",
    description="Jobs waiting or running, by type and status: queued (claimable), retrying "
    "(waiting for run_after) or running (scheduler leader only).",
)


class OpenCandidates:
    """The latest count of open reconciliation candidates per (Domain, type), as the last
    reconcile_sweep of each Domain in this process counted them; a type that empties reports
    0."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: dict[tuple[str, str], int] = {}

    def update(self, domain_id: str, counts: Mapping[str, int]) -> None:
        with self._lock:
            known = {k: 0 for k in self._counts if k[0] == domain_id}
            self._counts.update(known | {(domain_id, t): n for t, n in counts.items()})

    def observe(self, _options: CallbackOptions | None = None) -> Iterable[Observation]:
        with self._lock:
            counts = dict(self._counts)
        return [
            Observation(count, {"domain": domain_id, "type": kind})
            for (domain_id, kind), count in sorted(counts.items())
        ]


OPEN_CANDIDATES = OpenCandidates()
meter.create_observable_gauge(
    "lorebase.scribe.reconciliation.open",
    callbacks=[OPEN_CANDIDATES.observe],
    unit="{candidate}",
    description="Open reconciliation candidates by Domain and type, counted by each "
    "reconcile_sweep job.",
)
