"""OpenTelemetry instrumentation for Partyline.

Sets up tracing to measure method latency, identify blocking operations, and surface
performance issues. Exports to OTLP (http) or Jaeger (dev-local).
"""

import os
import sys
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import SimpleSpanProcessor


def init_telemetry(service_name: str = "covasant-partyline", local: bool = False) -> None:
    """Initialize OpenTelemetry tracing and metrics.

    Args:
        service_name: Service name for traces/metrics
        local: If True, use Jaeger dev exporter (http://localhost:16686); else OTLP
    """
    # Resource labels
    resource = Resource.create(
        {
            "service.name": service_name,
            "environment": "local" if local else os.getenv("ENVIRONMENT", "production"),
        }
    )

    # Tracer setup
    if local:
        # Dev mode: Jaeger (http://localhost:16686)
        jaeger_exporter = JaegerExporter(
            agent_host_name="localhost",
            agent_port=6831,
        )
        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(SimpleSpanProcessor(jaeger_exporter))
    else:
        # Prod mode: OTLP (http exporter, endpoint from env or default)
        otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
        otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

    trace.set_tracer_provider(trace_provider)

    # Metrics setup (optional; export if OTLP endpoint is configured)
    if not local and os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        otlp_metrics_exporter = OTLPMetricExporter(endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
        metrics_reader = PeriodicExportingMetricReader(otlp_metrics_exporter)
        metrics_provider = MeterProvider(resource=resource, metric_readers=[metrics_reader])
        metrics.set_meter_provider(metrics_provider)

    # Auto-instrument libraries
    FastAPIInstrumentor().instrument()
    Psycopg2Instrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

    print(
        f"[telemetry] initialized {service_name}; tracer={type(trace.get_tracer_provider()).__name__}", file=sys.stderr
    )


def get_tracer(module: str):
    """Get a tracer for a module."""
    return trace.get_tracer(__name__)
