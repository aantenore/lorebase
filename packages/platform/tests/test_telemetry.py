from __future__ import annotations

import socket

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lorebase_platform.settings import TelemetrySettings
from lorebase_platform.telemetry import setup_telemetry


def test_no_endpoint_creates_no_exporter_and_no_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("telemetry setup must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    previous = trace.get_tracer_provider()

    handle = setup_telemetry(TelemetrySettings())
    handle.shutdown()

    assert handle.enabled is False
    assert handle.tracer_provider is None
    assert handle.meter_provider is None
    assert trace.get_tracer_provider() is previous


def test_empty_endpoint_is_treated_as_disabled() -> None:
    assert setup_telemetry(TelemetrySettings(otlp_endpoint="")).enabled is False


def test_endpoint_enables_providers_with_resource_and_sampling() -> None:
    spans = InMemorySpanExporter()
    reader = InMemoryMetricReader()
    settings = TelemetrySettings(
        otlp_endpoint="http://collector:4317",
        service_name="svc",
        service_namespace="ns",
        sample_ratio=1.0,
    )

    handle = setup_telemetry(
        settings, install_global=False, span_exporter=spans, metric_reader=reader
    )
    assert handle.enabled
    assert handle.tracer_provider is not None
    assert handle.meter_provider is not None

    with handle.tracer_provider.get_tracer("t").start_as_current_span("op"):
        pass
    handle.meter_provider.get_meter("m").create_counter("hits").add(2)
    handle.tracer_provider.force_flush()

    (span,) = spans.get_finished_spans()
    assert span.name == "op"
    assert span.resource.attributes["service.name"] == "svc"
    assert span.resource.attributes["service.namespace"] == "ns"
    assert reader.get_metrics_data() is not None
    handle.shutdown()


def test_zero_sample_ratio_drops_root_spans() -> None:
    spans = InMemorySpanExporter()
    handle = setup_telemetry(
        TelemetrySettings(otlp_endpoint="http://collector:4317", sample_ratio=0),
        install_global=False,
        span_exporter=spans,
        metric_reader=InMemoryMetricReader(),
    )
    assert handle.tracer_provider is not None
    with handle.tracer_provider.get_tracer("t").start_as_current_span("op"):
        pass
    handle.tracer_provider.force_flush()
    assert spans.get_finished_spans() == ()
    handle.shutdown()
