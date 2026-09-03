"""Optional OpenTelemetry integration for OpenClaw Manager services."""

from .otel import ModelRequestObserver, initialize
from .adapters import (
    EvoScientistObservabilityAdapter,
    HermesObservabilityAdapter,
    ModelProxyObservabilityAdapter,
    OpenClawObservabilityAdapter,
)

__all__ = [
    "EvoScientistObservabilityAdapter", "HermesObservabilityAdapter",
    "ModelProxyObservabilityAdapter", "ModelRequestObserver",
    "OpenClawObservabilityAdapter", "initialize",
]
