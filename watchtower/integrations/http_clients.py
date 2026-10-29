"""Outgoing HTTP spans for requests and httpx (whichever is installed)."""


def instrument():
    try:
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument()
    except ImportError:
        pass
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
    except ImportError:
        pass
