"""OpenTelemetry instruments of Scribe. They are no-ops until `setup_telemetry` installs
providers (`telemetry.otlp_endpoint` set)."""

from __future__ import annotations

from opentelemetry import metrics, trace

INSTRUMENTATION = "lorebase.scribe"

tracer = trace.get_tracer(INSTRUMENTATION)
meter = metrics.get_meter(INSTRUMENTATION)

JOBS = meter.create_counter(
    "lorebase.scribe.jobs", unit="{job}", description="Jobs run, by type and outcome."
)
JOB_DURATION = meter.create_histogram(
    "lorebase.scribe.job.duration", unit="s", description="Duration of one job run."
)
SCHEDULED = meter.create_counter(
    "lorebase.scribe.scheduled", unit="{job}", description="Jobs queued by the scheduler."
)
