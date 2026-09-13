"""OpenTelemetry instrumentation for Partyline.

Measures method latency, identifies blocking operations, and surfaces performance issues.

Export target is decided by the environment, so it "just works" across deployments:
  - local mode (PARTYLINE_OTEL_LOCAL=true): spans to the console (→ app logs).
  - OTEL_EXPORTER_OTLP_ENDPOINT set: OTLP export (protocol from OTEL_EXPORTER_OTLP_PROTOCOL,
    default grpc — this is what Databricks Apps telemetry injects when enabled).
  - neither: instrument but DO NOT export. Avoids hammering a dead collector (e.g. the old
    localhost:4318 default) when no backend is configured; export switches on automatically
    once App telemetry is enabled and injects the endpoint. No code change needed then.

Every exporter gets a bounded timeout, and export runs in a background BatchSpanProcessor,
so telemetry can never hang request handling.
"""

import os
import sys

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource

_EXPORT_TIMEOUT = float(os.getenv("OTEL_EXPORT_TIMEOUT", "10"))  # seconds; never hang


def _span_exporter(protocol: str, endpoint: str):
    """OTLP span exporter for the injected protocol (grpc default; http/protobuf fallback).

    Imported lazily so a missing exporter dependency degrades gracefully (caught by the
    caller in main.py) instead of breaking module import.
    """
    if protocol.startswith("grpc"):
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=endpoint, timeout=int(_EXPORT_TIMEOUT))
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint, timeout=int(_EXPORT_TIMEOUT))


def _metric_exporter(protocol: str, endpoint: str):
    if protocol.startswith("grpc"):
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

        return OTLPMetricExporter(endpoint=endpoint, timeout=int(_EXPORT_TIMEOUT))
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

    return OTLPMetricExporter(endpoint=endpoint, timeout=int(_EXPORT_TIMEOUT))


def init_telemetry(service_name: str = "covasant-partyline", local: bool = False) -> None:
    """Initialize OpenTelemetry tracing and metrics (see module docstring for export modes)."""
    resource = Resource.create(
        {
            "service.name": service_name,
            "environment": "local" if local else os.getenv("ENVIRONMENT", "production"),
        }
    )
    trace_provider = TracerProvider(resource=resource)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    protocol = (os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL") or "grpc").lower()

    if local:
        trace_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        mode = "console (local)"
    elif endpoint:
        trace_provider.add_span_processor(BatchSpanProcessor(_span_exporter(protocol, endpoint)))
        reader = PeriodicExportingMetricReader(_metric_exporter(protocol, endpoint))
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
        mode = f"otlp/{protocol} -> {endpoint}"
    else:
        # No collector configured — instrument but don't export (avoids a dead-endpoint loop).
        mode = "instrumented; export disabled (set OTEL_EXPORTER_OTLP_ENDPOINT to enable)"

    trace.set_tracer_provider(trace_provider)

    # Auto-instrument libraries (cheap no-op when nothing exports).
    FastAPIInstrumentor().instrument()
    # psycopg (v3) — try the v3 instrumentor first, fall back to psycopg2 instrumentor,
    # skip silently if neither is installed.
    try:
        from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

        PsycopgInstrumentor().instrument()
    except (ImportError, ModuleNotFoundError):
        try:
            from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor

            Psycopg2Instrumentor().instrument()
        except (ImportError, ModuleNotFoundError):
            pass
    HTTPXClientInstrumentor().instrument()

    print(f"[telemetry] {service_name}: {mode}", file=sys.stderr)


def get_tracer(module: str):
    """Get a tracer for a module."""
    return trace.get_tracer(module)
