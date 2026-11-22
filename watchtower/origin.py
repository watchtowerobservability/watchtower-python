"""Code origin (L2): stamp the file, line and function that started a span as the OTel semconv
attributes code.filepath, code.lineno and code.function. Opt-in because it walks the stack per span.
Frames inside site-packages, opentelemetry and this package are skipped, so the first application
frame is reported. A span that already carries code.filepath is left alone."""
import os
import sys

from opentelemetry.sdk.trace import SpanProcessor

_SKIP = (f"{os.sep}site-packages{os.sep}", f"{os.sep}dist-packages{os.sep}",
         f"{os.sep}opentelemetry{os.sep}", os.path.dirname(os.path.abspath(__file__)) + os.sep, "<frozen ")
_MAX_FRAMES = 40


def first_app_frame(frame):
    """(file, line, function) of the first frame at or above `frame` outside the skipped paths."""
    for _ in range(_MAX_FRAMES):
        if frame is None:
            return None
        f = frame.f_code.co_filename
        if not any(s in f for s in _SKIP):
            return f, frame.f_lineno, frame.f_code.co_name
        frame = frame.f_back
    return None


class CodeOrigin(SpanProcessor):
    def on_start(self, span, parent_context=None):
        if (span.attributes or {}).get("code.filepath") is not None:
            return
        hit = first_app_frame(sys._getframe(1))  # noqa: SLF001 - the stack is the data
        if hit:
            span.set_attribute("code.filepath", hit[0])
            span.set_attribute("code.lineno", hit[1])
            span.set_attribute("code.function", hit[2])
