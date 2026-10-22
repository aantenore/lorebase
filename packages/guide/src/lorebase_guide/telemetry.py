"""OpenTelemetry instruments of Guide. No-ops until `setup_telemetry` installs providers
(`telemetry.otlp_endpoint` set). Attributes are names and outcomes, never values."""

from __future__ import annotations

from opentelemetry import metrics, trace

from lorebase_platform.telemetry import LATENCY_BUCKETS_S

INSTRUMENTATION = "lorebase.guide"

tracer = trace.get_tracer(INSTRUMENTATION)
meter = metrics.get_meter(INSTRUMENTATION)

REQUESTS = meter.create_counter(
    "lorebase.guide.requests", unit="{request}", description="HTTP requests, by route and status."
)
REQUEST_DURATION = meter.create_histogram(
    "lorebase.guide.request.duration",
    unit="s",
    description="Duration of one HTTP request (for /explore: until the stream ends).",
    explicit_bucket_boundaries_advisory=LATENCY_BUCKETS_S,
)
EXPLORATIONS = meter.create_counter(
    "lorebase.guide.explorations",
    unit="{exploration}",
    description="Explorations, by outcome (answered, not_found, budget, timeout, model, ...).",
)
STEPS = meter.create_counter(
    "lorebase.guide.steps", unit="{call}", description="Tool calls, by tool and success."
)
TOKENS = meter.create_counter(
    "lorebase.guide.tokens", unit="{token}", description="Model tokens, by direction."
)
EMBEDDINGS = meter.create_counter(
    "lorebase.guide.embeddings", unit="{text}", description="Texts embedded, by mode."
)
REFUSED = meter.create_counter(
    "lorebase.guide.refused",
    unit="{request}",
    description="Requests refused before any work, by reason (auth, origin, rate, busy).",
)
