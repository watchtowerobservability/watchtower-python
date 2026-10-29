"""`@wrap` a Lambda handler: one span per invocation, errors captured, spans flushed before freeze."""
import functools

from .. import client as _c
from ..errors import capture_exception


def wrap(handler):
    @functools.wraps(handler)
    def inner(event, context):
        c = _c._need()
        name = getattr(context, "function_name", None) or handler.__name__
        attrs = {"faas.invocation_id": getattr(context, "aws_request_id", "") or ""}
        try:
            with c.tracer.start_as_current_span(name, attributes=attrs):
                try:
                    return handler(event, context)
                except Exception as e:
                    capture_exception(e)
                    raise
        finally:
            c.provider.force_flush()

    return inner
