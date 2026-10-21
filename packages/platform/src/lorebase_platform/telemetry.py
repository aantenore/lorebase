"""OpenTelemetry setup for traces and metrics.

Telemetry is off unless `telemetry.otlp_endpoint` is configured: no provider, no exporter, no
background thread and no network access are created in that case.
"""

from __future__ import annotations

from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from lorebase_platform.settings import TelemetrySettings

LATENCY_BUCKETS_S: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
"""Bucket boundaries, in seconds, of the duration histograms (request, job and commit
latency). Without them the SDK defaults (0, 5, 10, 25, ...) put every sub-5-second value in
one bucket and quantiles cannot be read."""


@dataclass
class Telemetry:
    """Handle on the configured providers; `enabled` is False when nothing was set up."""

    enabled: bool
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None

    def shutdown(self) -> None:
        """Flush pending data and stop exporters."""
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()
        if self.meter_provider is not None:
            self.meter_provider.shutdown()


def setup_telemetry(
    settings: TelemetrySettings,
    *,
    install_global: bool = True,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
) -> Telemetry:
    """Create providers for the configured endpoint, or return a disabled handle.

    `span_exporter` and `metric_reader` replace the OTLP exporters (used by tests and by
    deployments that export through another channel). `install_global` registers the
    providers with the OpenTelemetry API.
    """
    if settings.otlp_endpoint is None:
        return Telemetry(enabled=False)

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.namespace": settings.service_namespace,
        }
    )
    tracer_provider = TracerProvider(
        resource=resource, sampler=ParentBased(TraceIdRatioBased(settings.sample_ratio))
    )
    tracer_provider.add_span_processor(_span_processor(settings, span_exporter))
    meter_provider = MeterProvider(
        resource=resource, metric_readers=[metric_reader or _metric_reader(settings)]
    )
    if install_global:
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
    return Telemetry(enabled=True, tracer_provider=tracer_provider, meter_provider=meter_provider)


def _span_processor(settings: TelemetrySettings, exporter: SpanExporter | None) -> SpanProcessor:
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
    return BatchSpanProcessor(exporter)


def _metric_reader(settings: TelemetrySettings) -> MetricReader:
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    return PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=settings.otlp_endpoint),
        export_interval_millis=settings.metric_export_interval_s * 1000,
    )
