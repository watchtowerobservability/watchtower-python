"""IAST (opt-in): notices at runtime when request-derived input reaches a sensitive sink and
reports it to `POST /v1/projects/{prj}/code-security/runtime`.

Off until `start_iast(...)` is called. `IastMiddleware(app)` (WSGI: Flask, Django) and
`IastASGIMiddleware(app)` (FastAPI, Starlette) remember the request's input strings (query string,
path, cookies, header values and, for WSGI, a form or JSON body that was already read by the app
through `environ["wsgi.input"]` is NOT consumed: pass bodies with `add_inputs`) for the life of the
request in a `contextvars.ContextVar`. `guard_sink(fn, "sql_injection")` wraps a sink
(`cursor.execute`, `os.system`, `open`, ...); before the call it checks whether any input string
appears inside the sink's first argument. Parameterised queries keep the input in the parameter
sequence, not the text, so they never match. A hit is queued with the `file:line` of the caller
(first frame outside this module and site-packages) and the active trace id, and flushed in the
background. Each (type, location) is reported once per process.

shortcut: taint is substring matching of raw input values (>= 4 chars) in the sink text, not
byte-level propagation; input that is transformed (encoded, split) before use is missed, and a
request body is only seen if the app passes it to `add_inputs`. Upgrade to AST-level propagation.
"""
import contextvars
import functools
import json
import sys
import threading
import urllib.parse
import urllib.request

from opentelemetry import trace

MIN_LEN = 4
MAX_INPUTS = 256
_inputs = contextvars.ContextVar("wt_iast_inputs", default=None)
_lock = threading.Lock()
_state = {"opts": None, "queue": [], "seen": set(), "timer": None}


def start_iast(api_url, api_key, project, service, flush_s=2.0, post=None):
    """Turn IAST on. `post(url, headers, body_bytes)` replaces the HTTP call (tests)."""
    with _lock:
        _state.update(opts={"api_url": api_url.rstrip("/"), "key": api_key, "project": project,
                            "service": service, "flush_s": flush_s, "post": post or _post},
                      queue=[], seen=set())


def stop_iast():
    with _lock:
        t = _state["timer"]
        _state.update(opts=None, queue=[], seen=set(), timer=None)
    if t:
        t.cancel()


def _post(url, headers, body):
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=10):
        pass


def _collect(v, out, depth=0):
    if len(out) >= MAX_INPUTS or depth > 4 or v is None:
        return
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    if isinstance(v, str):
        if len(v) >= MIN_LEN:
            out.append(v)
            dec = urllib.parse.unquote_plus(v)
            if dec != v and len(dec) >= MIN_LEN:
                out.append(dec)
    elif isinstance(v, (list, tuple, set)):
        for x in v:
            _collect(x, out, depth + 1)
    elif isinstance(v, dict):
        for x in v.values():
            _collect(x, out, depth + 1)


def add_inputs(*values):
    """Mark more values (a parsed body, a path parameter) as tainted for the current request."""
    cur = _inputs.get()
    if cur is None:
        return
    for v in values:
        _collect(v, cur)


def _collect_query(qs, out):
    _collect(qs, out)
    _collect(urllib.parse.parse_qs(qs), out)
    # the raw (still percent-encoded) values too: apps often slice them straight out of the query string
    _collect([p.split("=", 1)[-1] for p in qs.split("&")], out)


def _env_inputs(environ):
    out = []
    qs = environ.get("QUERY_STRING", "")
    _collect_query(qs, out)
    _collect(environ.get("PATH_INFO", ""), out)
    for k, v in environ.items():
        if k.startswith("HTTP_") or k == "CONTENT_TYPE":
            _collect(v, out)
    return out


class IastMiddleware:
    """WSGI middleware: marks this request's inputs as tainted for downstream sinks."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if _state["opts"] is None:
            return self.app(environ, start_response)
        tok = _inputs.set(_env_inputs(environ))
        try:
            return self.app(environ, start_response)
        finally:
            _inputs.reset(tok)


class IastASGIMiddleware:
    """ASGI middleware: the same for FastAPI and Starlette."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if _state["opts"] is None or scope.get("type") != "http":
            return await self.app(scope, receive, send)
        out = []
        qs = scope.get("query_string", b"").decode("utf-8", "replace")
        _collect_query(qs, out)
        _collect(scope.get("path", ""), out)
        for _, v in scope.get("headers", []):
            _collect(v, out)
        tok = _inputs.set(out)
        try:
            return await self.app(scope, receive, send)
        finally:
            _inputs.reset(tok)


def _caller_location():
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if fn != __file__ and "site-packages" not in fn and "<frozen" not in fn:
            return f"{fn}:{f.f_lineno}"
        f = f.f_back
    return ""


def check_sink(vuln_type, text, severity="high"):
    """Check a sink's text against the current request's inputs; queue a finding on a hit."""
    inputs = _inputs.get()
    opts = _state["opts"]
    if opts is None or not inputs or not isinstance(text, str):
        return False
    if not any(v in text for v in inputs):
        return False
    loc = _caller_location()
    key = f"{vuln_type}|{loc}"
    ctx = trace.get_current_span().get_span_context()
    with _lock:
        if key in _state["seen"]:
            return True
        _state["seen"].add(key)
        _state["queue"].append({
            "vuln_type": vuln_type,
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else "",
            "code_location": loc, "severity": severity,
            "title": f"Request input reaches {vuln_type.replace('_', ' ')} sink"})
        if _state["timer"] is None:
            t = threading.Timer(opts["flush_s"], flush_iast)
            t.daemon = True
            _state["timer"] = t
            t.start()
    return True


def guard_sink(fn, vuln_type, severity="high"):
    """Wrap a sink whose first argument (after `self` for bound methods) is the sensitive text."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        first = a[0] if a else None
        check_sink(vuln_type, first, severity)
        return fn(*a, **kw)
    return wrapper


def flush_iast():
    """Send queued findings now. Never raises; a failed send drops the batch (best effort)."""
    with _lock:
        opts, findings = _state["opts"], _state["queue"]
        _state.update(queue=[], timer=None)
    if not opts or not findings:
        return
    url = f"{opts['api_url']}/v1/projects/{urllib.parse.quote(opts['project'], safe='')}/code-security/runtime"
    try:
        opts["post"](url, {"Authorization": f"Bearer {opts['key']}", "Content-Type": "application/json"},
                     json.dumps({"service": opts["service"], "findings": findings}).encode())
    except Exception:  # noqa: BLE001 - IAST must never break the app
        pass
