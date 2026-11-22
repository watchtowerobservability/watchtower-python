"""`@wrap` an Azure Functions handler: one span per invocation with cold start, invocation id and
trigger, errors captured, HTTP 5xx marked failed, spans flushed before the host may freeze."""
import functools
import inspect

from opentelemetry.trace import SpanKind, Status, StatusCode

from .. import client as _c
from ..errors import capture_exception

_TRIGGERS = {
    "HttpRequest": "http",
    "TimerRequest": "timer",
    "QueueMessage": "pubsub",
    "ServiceBusMessage": "pubsub",
    "EventHubEvent": "pubsub",
    "EventGridEvent": "pubsub",
    "InputStream": "datasource",
    "DocumentList": "datasource",
}
_cold = [True]


def faas_trigger(args):
    """The OTel `faas.trigger` of the first binding whose type is known, else "other"."""
    for a in args:
        t = _TRIGGERS.get(type(a).__name__)
        if t:
            return t
    return "other"


def _start(handler, args, kwargs):
    ctx = kwargs.get("context")
    cold, _cold[0] = _cold[0], False
    trig = faas_trigger(list(args) + [v for k, v in kwargs.items() if k != "context"])
    attrs = {
        "cloud.provider": "azure",
        "cloud.platform": "azure_functions",
        "faas.name": getattr(ctx, "function_name", None) or handler.__name__,
        "faas.invocation_id": getattr(ctx, "invocation_id", "") or "",
        "faas.trigger": trig,
        "faas.coldstart": cold,
    }
    kind = SpanKind.SERVER if trig == "http" else SpanKind.CONSUMER
    return _c._need(), attrs, kind


def _result(span, attrs, r):
    code = getattr(r, "status_code", None)
    if attrs["faas.trigger"] == "http" and isinstance(code, int):
        span.set_attribute("http.response.status_code", code)
        if code >= 500:
            span.set_status(Status(StatusCode.ERROR))
    return r


def wrap(handler):
    """Bindings arrive as keyword arguments; pass `context: func.Context` to get the invocation id."""
    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def ainner(*args, **kwargs):
            c, attrs, kind = _start(handler, args, kwargs)
            try:
                with c.tracer.start_as_current_span(attrs["faas.name"], kind=kind, attributes=attrs) as span:
                    try:
                        return _result(span, attrs, await handler(*args, **kwargs))
                    except Exception as e:
                        capture_exception(e)
                        raise
            finally:
                c.provider.force_flush()

        return ainner

    @functools.wraps(handler)
    def inner(*args, **kwargs):
        c, attrs, kind = _start(handler, args, kwargs)
        try:
            with c.tracer.start_as_current_span(attrs["faas.name"], kind=kind, attributes=attrs) as span:
                try:
                    return _result(span, attrs, handler(*args, **kwargs))
                except Exception as e:
                    capture_exception(e)
                    raise
        finally:
            c.provider.force_flush()

    return inner
