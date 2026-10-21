"""OpenTelemetry instruments of the store. They are no-ops until the process installs
providers (`lorebase_platform.telemetry.setup_telemetry`, `telemetry.otlp_endpoint` set); the
process running the store code (Keeper or Scribe) exports them under its own service name."""

from __future__ import annotations

from opentelemetry import metrics

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
GC_RUNS = meter.create_counter(
    "lorebase.gc.runs", unit="{run}", description="Completed garbage collections of a Domain."
)
GC_FREED_BYTES = meter.create_counter(
    "lorebase.gc.freed_bytes",
    unit="By",
    description="Bytes of tree nodes and objects deleted by garbage collection.",
)


def record_definer_call(function: str) -> None:
    DEFINER_READS.add(1, {"function": function})
