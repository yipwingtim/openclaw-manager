import os
import sys
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services"))

from observability.otel import ModelRequestObserver, _headers, _resource_attributes, initialize
from observability.adapters import (
    EvoScientistObservabilityAdapter,
    HermesObservabilityAdapter,
    OpenClawObservabilityAdapter,
)


class ObservabilityTests(unittest.TestCase):
    def test_disabled_initialization_is_noop(self):
        with patch.dict(os.environ, {"OTEL_ENABLED": "false"}, clear=False):
            self.assertIsNone(initialize())

    def test_configuration_parsers_ignore_malformed_items(self):
        with patch.dict(os.environ, {
            "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Basic abc, malformed, X-Test=value=with-equals",
            "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=production,broken",
        }, clear=False):
            self.assertEqual(_headers(), {
                "Authorization": "Basic abc",
                "X-Test": "value=with-equals",
            })
            self.assertEqual(_resource_attributes(), {"deployment.environment": "production"})

    def test_noop_observer_context_is_usable(self):
        with ModelRequestObserver(None).observe(
            agent_id="alice", model="qwen", path="chat/completions", request_bytes=12
        ) as span:
            self.assertIsNone(span)

    def test_product_adapters_share_observer_contract(self):
        class Observer:
            def observe(self, **fields):
                self.fields = fields
                from contextlib import nullcontext
                return nullcontext()

        for adapter_type, product in (
            (OpenClawObservabilityAdapter, "openclaw"),
            (HermesObservabilityAdapter, "hermes"),
            (EvoScientistObservabilityAdapter, "evoscientist"),
        ):
            observer = Observer()
            with adapter_type(observer).llm_call(
                agent_id="a", model="m", path="chat/completions", request_bytes=1
            ):
                pass
            self.assertEqual(observer.fields["product"], product)

    def test_span_creation_failure_degrades_to_noop(self):
        class BrokenTracer:
            def start_as_current_span(self, *args, **kwargs):
                raise RuntimeError("collector unavailable")

        with ModelRequestObserver(BrokenTracer()).observe(
            agent_id="alice", model="qwen", path="chat/completions", request_bytes=12
        ) as span:
            self.assertIsNone(span)


if __name__ == "__main__":
    unittest.main()
