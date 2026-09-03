import logging
import os
import time
from contextlib import nullcontext


LOG = logging.getLogger(__name__)


def _enabled():
    return os.environ.get("OTEL_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _headers():
    result = {}
    for item in os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def initialize(service_name=None):
    """Return an OTel tracer, or a no-op tracer when observability is disabled."""
    if not _enabled():
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        LOG.warning("OTEL_ENABLED is true but OpenTelemetry SDK is unavailable")
        return None
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    if not endpoint:
        base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
        endpoint = f"{base}/v1/traces" if base else ""
    if not endpoint:
        LOG.warning("OTEL_ENABLED is true but no OTLP traces endpoint is configured")
        return None
    resource = Resource.create({
        "service.name": service_name or os.environ.get("OTEL_SERVICE_NAME", "openclaw-manager"),
        **_resource_attributes(),
    })
    try:
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, headers=_headers())
        ))
        trace.set_tracer_provider(provider)
        return trace.get_tracer("openclaw-manager.observability")
    except Exception:
        LOG.warning("OpenTelemetry initialization failed; continuing without export", exc_info=True)
        return None


def _resource_attributes():
    result = {}
    for item in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            result[key.strip()] = value.strip()
    return result


class ModelRequestObserver:
    """Product-neutral model request observer; export failures never affect callers."""

    def __init__(self, tracer=None):
        self.tracer = tracer

    def observe(self, *, agent_id, model, path, request_bytes, message_count=0,
                tool_result_count=0, tool_result_bytes=0, session_id="", run_id="",
                product=""):
        if self.tracer is None:
            return nullcontext()
        attributes = {
            "openclaw.agent.id": agent_id,
            "openclaw.agent.name": agent_id,
            "openclaw.product": product,
            "gen_ai.request.model": model,
            "gen_ai.operation.name": "chat" if path == "chat/completions" else path,
            "openclaw.request.path": path,
            "openclaw.request.bytes": request_bytes,
            "openclaw.request.message_count": message_count,
            "openclaw.request.tool_result_count": tool_result_count,
            "openclaw.request.tool_result_bytes": tool_result_bytes,
        }
        if session_id:
            attributes["openclaw.session.id"] = session_id
        if run_id:
            attributes["openclaw.run.id"] = run_id
        try:
            span = self.tracer.start_as_current_span("llm.call", attributes=attributes)
        except Exception:
            LOG.debug("OTel span creation failed", exc_info=True)
            return nullcontext()
        started = time.monotonic()

        class _SafeSpan:
            def __enter__(self):
                return span.__enter__()

            def __exit__(self, exc_type, exc, tb):
                try:
                    if exc is not None:
                        from opentelemetry.trace import Status, StatusCode
                        span.record_exception(exc)
                        span.set_status(Status(StatusCode.ERROR))
                    span.set_attribute("openclaw.request.duration_ms", int((time.monotonic() - started) * 1000))
                except Exception:
                    LOG.debug("OTel span finalization failed", exc_info=True)
                return span.__exit__(exc_type, exc, tb)

        return _SafeSpan()
