"""Error capture: exceptions become error spans carrying trace ids; hooks catch what nobody handled."""
import json
import re
import reprlib
import sys
import threading

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from . import client as _c

_prev = {}
_locals = {"on": False}
_SENSITIVE = re.compile(r"pass|secret|token|api[-_]?key|auth|cookie|session|credit|card|ssn|private|bearer", re.I)
_repr = reprlib.Repr()
_repr.maxstring = _repr.maxother = 120
_repr.maxlevel, _repr.maxlist, _repr.maxdict = 2, 6, 6
MAX_LOCAL_FRAMES, MAX_LOCAL_VARS = 8, 20


def enable_locals(on=True):
    """Attach the local variables of the innermost in-app frames to captured exceptions (exception
    replay). Off by default; the project must also turn it on in its issue settings or the server
    drops them. Names that look secret are never read, values are shortened."""
    _locals["on"] = bool(on)


def _in_app(filename):
    return not (filename.startswith("<") or "site-packages" in filename or "dist-packages" in filename
                or filename.startswith(sys.base_prefix) or filename.startswith(sys.prefix + "/lib"))


def exception_locals(exc):
    """`[{function, filename, lineno, vars}]`, innermost frame first, as the `exception.locals` event attribute."""
    chain, tb = [], getattr(exc, "__traceback__", None)
    while tb is not None:
        chain.append((tb.tb_frame, tb.tb_lineno))
        tb = tb.tb_next
    out = []
    for fr, line in reversed(chain):
        name = fr.f_code.co_filename
        if not _in_app(name):
            continue
        vars_ = {}
        for k, v in list(fr.f_locals.items())[:MAX_LOCAL_VARS * 2]:
            if k.startswith("__") or len(vars_) >= MAX_LOCAL_VARS:
                continue
            try:
                vars_[k] = "[Filtered]" if _SENSITIVE.search(k) else _repr.repr(v)
            except Exception:  # noqa: BLE001 - a broken __repr__ must not break capture
                vars_[k] = "<unrepresentable>"
        if vars_:
            out.append({"function": fr.f_code.co_name, "filename": name, "lineno": line, "vars": vars_})
        if len(out) >= MAX_LOCAL_FRAMES:
            break
    return out


def _mark(span, exc, attrs):
    extra = None
    if _locals["on"]:
        try:
            frames = exception_locals(exc)
            if frames:
                extra = {"exception.locals": json.dumps(frames)[:15000]}
        except Exception:  # noqa: BLE001 - never let capture break the crash path
            extra = None
    span.record_exception(exc, attributes=extra)
    span.set_status(Status(StatusCode.ERROR, str(exc)))
    for k, v in (attrs or {}).items():
        if not k.startswith(_c.WT_PREFIX):
            span.set_attribute(k, v)


def capture_exception(exc=None, **attrs):
    """Record `exc` (default: the exception being handled) on the current span, or on a new
    `exception` span. Returns the 32-hex trace id, or None when there is nothing to capture."""
    exc = exc or sys.exc_info()[1]
    if exc is None or getattr(exc, "_wt_captured", False):
        return None
    c = _c._need()
    try:
        exc._wt_captured = True  # an exception reaches several hooks; report it once
    except Exception:  # noqa: BLE001 - slotted/immutable exceptions just may repeat
        pass
    cur = trace.get_current_span()
    if cur.get_span_context().is_valid and cur.is_recording():
        _mark(cur, exc, attrs)
        return format(cur.get_span_context().trace_id, "032x")
    with c.tracer.start_as_current_span("exception") as span:
        _mark(span, exc, attrs)
        return format(span.get_span_context().trace_id, "032x")


def _safe_capture(exc):
    try:
        capture_exception(exc)
    except Exception:  # noqa: BLE001 - never let capture break the crash path
        pass


def _excepthook(tp, val, tb):
    _safe_capture(val)
    _prev["sys"](tp, val, tb)


def _thread_hook(args):
    _safe_capture(args.exc_value)
    _prev["thread"](args)


def install_hooks():
    """Chain `sys.excepthook` and `threading.excepthook` (idempotent)."""
    if "sys" in _prev:
        return
    _prev["sys"], _prev["thread"] = sys.excepthook, threading.excepthook
    sys.excepthook, threading.excepthook = _excepthook, _thread_hook


def uninstall_hooks():
    if "sys" in _prev:
        sys.excepthook, threading.excepthook = _prev.pop("sys"), _prev.pop("thread")


def install_asyncio(loop):
    """Capture exceptions that reach the event loop's exception handler."""
    prev = loop.get_exception_handler()

    def handler(lp, ctx):
        if isinstance(ctx.get("exception"), BaseException):
            _safe_capture(ctx["exception"])
        if prev:
            prev(lp, ctx)
        else:
            lp.default_exception_handler(ctx)

    loop.set_exception_handler(handler)
