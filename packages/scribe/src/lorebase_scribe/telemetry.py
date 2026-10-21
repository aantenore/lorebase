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
    description="Jobs waiting or running, by type and status (scheduler leader only).",
)
