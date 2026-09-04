class ProductObservabilityAdapter:
    """Product-neutral facade; products can override extraction without changing export."""

    product = "generic"
    capabilities = {"model_call": True, "run_correlation": False, "trace_context": False}

    def __init__(self, observer):
        self.observer = observer

    def llm_call(self, **fields):
        fields.setdefault("product", self.product)
        return self.observer.observe(**fields)

    observe = llm_call


class OpenClawObservabilityAdapter(ProductObservabilityAdapter):
    product = "openclaw"


class HermesObservabilityAdapter(ProductObservabilityAdapter):
    product = "hermes"


class EvoScientistObservabilityAdapter(ProductObservabilityAdapter):
    product = "evoscientist"


class ModelProxyObservabilityAdapter(ProductObservabilityAdapter):
    product = "model-proxy"
