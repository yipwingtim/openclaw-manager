import logging
import json
import os
import time
from datetime import datetime, timezone


LOG = logging.getLogger(__name__)


class _NoopObservation:
    def __enter__(self):
        return None

    def __exit__(self, *args):
        return False

    def set_attribute(self, *args):
        pass

    set_output = set_attribute
    set_usage = set_attribute
    mark_completion_started = set_attribute


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
                product="", input_value=None, model_parameters=None, context=None,
                instance_id="", environment=""):
        if self.tracer is None:
            return _NoopObservation()
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
            "langfuse.observation.type": "generation",
            "langfuse.observation.model.name": model,
            "langfuse.observation.model.parameters": "{}",
        }
        if input_value is not None:
            attributes["langfuse.observation.input"] = _json_value(input_value)
        if model_parameters is not None:
            attributes["langfuse.observation.model.parameters"] = _json_value(model_parameters)
        if session_id:
            attributes["openclaw.session.id"] = session_id
            attributes["langfuse.session.id"] = session_id
        if run_id:
            attributes["openclaw.run.id"] = run_id
            attributes["langfuse.trace.metadata.run_id"] = run_id
        if instance_id:
            attributes["langfuse.trace.metadata.instance_id"] = instance_id
        if product:
            attributes["langfuse.trace.metadata.product"] = product
        if environment:
            attributes["langfuse.environment"] = environment
        try:
            span = self.tracer.start_as_current_span("llm.call", context=context, attributes=attributes)
        except Exception:
            LOG.debug("OTel span creation failed", exc_info=True)
            return _NoopObservation()
        started = time.monotonic()

        class _SafeSpan:
            def __enter__(self):
                span.__enter__()
                return self

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

            def set_attribute(self, key, value):
                try:
                    span.set_attribute(key, value)
                except Exception:
                    LOG.debug("OTel span attribute failed", exc_info=True)

            def set_output(self, value):
                try:
                    if isinstance(value, (dict, list)):
                        parsed = value
                    elif isinstance(value, bytes):
                        value = value.decode("utf-8", errors="replace")
                        try:
                            parsed = json.loads(value)
                        except (TypeError, ValueError):
                            parsed = {"raw": str(value)}
                    else:
                        try:
                            parsed = json.loads(value)
                        except (TypeError, ValueError):
                            parsed = {"raw": str(value)}
                    span.set_attribute("langfuse.observation.output", _json_value(parsed))
                except Exception:
                    LOG.debug("OTel output attribute failed", exc_info=True)

            def set_usage(self, usage):
                try:
                    span.set_attribute("langfuse.observation.usage_details", _json_value(usage))
                except Exception:
                    LOG.debug("OTel usage attribute failed", exc_info=True)

            def mark_completion_started(self):
                self.set_attribute(
                    "langfuse.observation.completion_start_time",
                    datetime.now(timezone.utc).isoformat(),
                )

        return _SafeSpan()


def _json_value(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps({"unserializable": str(value)}, ensure_ascii=False)
