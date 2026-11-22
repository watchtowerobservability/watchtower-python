"""Dynamic probes for running Python code (PLT-044, P6-DI1): log lines and snapshots added from the
Watchtower source view attach at runtime, with no redeploy and no code change.

`start(endpoint, key, service)` polls `GET /v1/probes/active` (an ingestion key with scope
`probes:read`) and hooks every active probe. A probe names `module:function` (or a bare function
name) or `file` + `line`. A hit evaluates the probe's condition over the frame's locals (a small
subset with no calls, the same as the browser and Go SDKs, so it cannot run code), applies the
per-minute rate limit, flattens locals to `code.snapshot.*` (depth at most 3, 8 KiB) and calls
`emit("probe.snapshot", attrs)`. The default emitter adds a span event to the current span and logs
through `logging` at INFO on logger `watchtower.probes`.

Kind `span` wraps the named function in a span `probe <function>` (made current, so the spans the
call makes are its children); kind `span_decoration` sets `capture.tags` (values are message
templates over the locals) on the span active at the probe location.

The tracer is installed only while at least one probe is live; with no probe the interpreter runs
untraced. shortcut: sys.settrace adds per-call overhead while a probe is live; move to
sys.monitoring (3.12+) when probes stay on for long.
"""
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VALUE_MAX = 1024
_TOKEN = re.compile(r"""\s*(&&|\|\||==|!=|<=|>=|[<>!()\[\],]|'[^']*'|"[^"]*"|-?\d+(?:\.\d+)?|[A-Za-z_][\w.]*)""")
_log = logging.getLogger("watchtower.probes")


def compile_condition(src):
    """Compile a condition to a predicate over a dict; raises ValueError outside the subset."""
    if not src or not src.strip():
        return lambda _v: True
    toks, pos, src = [], 0, src.rstrip()
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise ValueError("bad condition")
        toks.append(m.group(1))
        pos = m.end()
    i = [0]

    def peek():
        return toks[i[0]] if i[0] < len(toks) else None

    def eat(s):
        if peek() == s:
            i[0] += 1
            return True
        return False

    def lit(x):
        if x in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[x]
        if x[0] in "'\"":
            return x[1:-1]
        return float(x) if "." in x else int(x)

    def get(v, name):
        for k in name.split("."):
            v = v.get(k) if isinstance(v, dict) else getattr(v, k, None)
        return v

    def atom():
        x = peek()
        if x is None:
            raise ValueError("bad condition")
        i[0] += 1
        if x == "(":
            e = or_()
            if not eat(")"):
                raise ValueError("bad condition")
            return e
        if x == "[":
            items = []
            while peek() != "]":
                n = peek()
                if n is None:
                    raise ValueError("bad condition")
                i[0] += 1
                if n != ",":
                    items.append(lit(n))
            i[0] += 1
            return lambda _v: items
        if x[0] in "'\"-0123456789" or x in ("true", "false", "null"):
            val = lit(x)
            return lambda _v: val
        if peek() == "(":
            raise ValueError("function calls are not allowed")
        return lambda v: get(v, x)

    def cmp():
        a = atom()
        for op in ("==", "!=", "<=", ">=", "<", ">", "in", "contains"):
            if eat(op):
                b = atom()
                return lambda v, op=op: _compare(op, a(v), b(v))
        return a

    def not_():
        if eat("!"):
            n = not_()
            return lambda v: not n(v)
        return cmp()

    def and_():
        left = not_()
        while eat("&&"):
            l, r = left, not_()
            left = lambda v, l=l, r=r: bool(l(v)) and bool(r(v))  # noqa: E731
        return left

    def or_():
        left = and_()
        while eat("||"):
            l, r = left, and_()
            left = lambda v, l=l, r=r: bool(l(v)) or bool(r(v))  # noqa: E731
        return left

    root = or_()
    if i[0] != len(toks):
        raise ValueError("bad condition")
    return lambda v: bool(root(v))


def _compare(op, x, y):
    try:
        if op == "==":
            return x == y
        if op == "!=":
            return x != y
        if op == "in":
            return isinstance(y, list) and x in y
        if op == "contains":
            return isinstance(x, str) and isinstance(y, str) and y in x
        return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]
    except TypeError:
        return False


_TEMPLATE = re.compile(r"\{([A-Za-z_][\w.]*)\}")


def render(template, values):
    """Fill `{a.b}` from the locals (missing names render empty), cut to 1 KiB."""
    def one(m):
        v = values
        for k in m.group(1).split("."):
            v = v.get(k) if isinstance(v, dict) else getattr(v, k, None)
        return "" if v is None else str(v)
    try:
        return _TEMPLATE.sub(one, template)[:VALUE_MAX]
    except Exception:  # noqa: BLE001 - a broken __str__ must not break the app
        return "[unprintable]"


def capture(values, depth, max_bytes):
    """Flatten to dotted names: nesting past `depth` collapses, values cut to 1 KiB, total to max_bytes."""
    out, used = {}, [0]

    def put(name, val):
        s = val[:VALUE_MAX]
        if used[0] + len(name) + len(s) <= max_bytes:
            used[0] += len(name) + len(s)
            out[name] = s

    def walk(prefix, v, d):
        if isinstance(v, dict):
            if d > depth:
                return put(prefix, "[depth limit]")
            for k in sorted(v, key=str):
                walk(f"{prefix}.{k}" if prefix else str(k), v[k], d + 1)
        else:
            try:
                put(prefix, str(v))
            except Exception:  # noqa: BLE001 - a broken __str__ must not break the app
                put(prefix, "[unprintable]")

    walk("", values, 0)
    return out


class _Live:
    def __init__(self, d):
        self.d, self.ok, self.win, self.n = d, compile_condition(d.get("condition", "")), 0, 0
        loc = d.get("location") or {}
        fn = loc.get("function") or ""
        self.module, _, self.func = fn.rpartition(":") if ":" in fn else ("", "", fn)
        self.file, self.line = loc.get("file") or "", int(loc.get("line") or 0)

    def matches_code(self, code):
        if self.func:
            return code.co_name == self.func and (not self.module or self.module.replace(".", "/") in code.co_filename)
        return bool(self.file) and code.co_filename.endswith(self.file)


class Probes:
    """The probe set of one service; `set()` replaces it, `poll()` refreshes it from the API."""

    def __init__(self, endpoint, key, service, env="", version="", emit=None, now=time.time):
        self.endpoint, self.key, self.service, self.env, self.version = endpoint.rstrip("/"), key, service, env, version
        self.emit, self.now, self.etag, self.live = emit or _default_emit, now, "", []
        self._lock = threading.Lock()
        self.tracer = None  # span probes use the global tracer unless one is set

    def set(self, defs):
        live = []
        for d in defs:
            try:
                live.append(_Live(d))
            except (ValueError, TypeError):
                continue  # a probe we cannot evaluate is skipped
        with self._lock:
            self.live = live
        _install(self if live else None)

    def poll(self):
        q = urllib.parse.urlencode({"service": self.service, "env": self.env, "version": self.version,
                                    "kinds": "log,snapshot,metric,span,span_decoration"})
        req = urllib.request.Request(f"{self.endpoint}/v1/probes/active?{q}", headers={"Authorization": f"Bearer {self.key}"})
        if self.etag:
            req.add_header("If-None-Match", self.etag)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - endpoint is the operator's
                self.etag = r.headers.get("ETag", "")
                self.set(json.loads(r.read(4 << 20)).get("probes", []))
        except urllib.error.HTTPError as e:
            if e.code != 304:
                _log.debug("probe poll: HTTP %s", e.code)
        except OSError as e:
            _log.debug("probe poll: %s", e)

    def allowed(self, p, values):
        """Not expired, inside the per-minute budget and the condition holds (counts the hit)."""
        t = self.now()
        if p.d.get("expires_at") and t >= p.d["expires_at"]:
            return False
        minute = int(t // 60)
        if p.win != minute:
            p.win, p.n = minute, 0
        if p.n >= min(p.d.get("max_rate_per_min") or 60, 60) or not p.ok(values):
            return False
        p.n += 1
        return True

    def decorate(self, p, frame, span=None):
        """span_decoration: set the probe's tags on the active span; returns the tags set."""
        values = dict(frame.f_locals)
        if not self.allowed(p, values):
            return {}
        tags = {str(t.get("name", "")): render(str(t.get("value", "")), values)
                for t in (p.d.get("capture") or {}).get("tags") or [] if t.get("name")}
        try:
            if span is None:
                from opentelemetry import trace

                span = trace.get_current_span()
            if span.is_recording():
                span.set_attributes(tags)
        except Exception:  # noqa: BLE001 - the app must never fail because of a probe
            _log.debug("probe decoration failed", exc_info=True)
        return tags

    def open_span(self, p, frame, tracer=None):
        """span: start `probe <function>` as the current span; returns (span, token) or None."""
        if not self.allowed(p, dict(frame.f_locals)):
            return None
        try:
            from opentelemetry import context, trace

            tracer = tracer or self.tracer or trace.get_tracer("watchtower.probes")
            span = tracer.start_span(f"probe {frame.f_code.co_name}", attributes={
                "probe.id": str(p.d.get("id", "")), "code.function": frame.f_code.co_name,
                "code.filepath": frame.f_code.co_filename, "code.lineno": frame.f_code.co_firstlineno})
            return span, context.attach(trace.set_span_in_context(span))
        except Exception:  # noqa: BLE001
            _log.debug("probe span failed", exc_info=True)
            return None

    def hit(self, p, frame):
        kind = p.d.get("kind")
        if kind == "span_decoration":
            self.decorate(p, frame)
            return
        if kind == "span":
            return  # spans open and close in the tracer (call/return)
        t = self.now()
        if p.d.get("expires_at") and t >= p.d["expires_at"]:
            return
        values = dict(frame.f_locals)
        minute = int(t // 60)
        if p.win != minute:
            p.win, p.n = minute, 0
        if p.n >= min(p.d.get("max_rate_per_min") or 60, 60) or not p.ok(values):
            return
        p.n += 1
        cap = p.d.get("capture") or {}
        attrs = {"probe.id": str(p.d.get("id", "")), "code.function": frame.f_code.co_name,
                 "code.filepath": frame.f_code.co_filename, "code.lineno": str(frame.f_lineno)}
        if p.d.get("message"):
            attrs["probe.message"] = str(p.d["message"])
        snap = capture(values, min(cap.get("locals_depth") or 3, 3), min(cap.get("max_bytes") or 8192, 8192))
        attrs.update({f"code.snapshot.{k}": v for k, v in snap.items()})
        try:
            self.emit("probe.snapshot", attrs)
        except Exception:  # noqa: BLE001 - the app must never fail because of a probe
            _log.debug("probe emit failed", exc_info=True)


def _default_emit(name, attrs):
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span.is_recording():
            span.add_event(name, attrs)
    except ImportError:
        pass
    _log.info(name, extra={"wt_attrs": attrs})


_active = None


def _tracer(frame, event, _arg):
    probes = _active
    if probes is None or event != "call":
        return None
    hits = [p for p in probes.live if p.matches_code(frame.f_code)]
    if not hits:
        return None
    opened = []
    for p in hits:
        if p.func and p.d.get("kind") == "span":
            o = probes.open_span(p, frame)
            if o:
                opened.append(o)
        elif p.func:
            probes.hit(p, frame)
    lines = [p for p in hits if not p.func and p.line]
    if not lines and not opened:
        return None

    def local(f, ev, _a):
        if ev == "line":
            for p in lines:
                if f.f_lineno == p.line:
                    probes.hit(p, f)
        elif ev == "return" and f is frame:
            _close(opened)
        elif ev == "exception" and f is frame and opened:
            for span, _ in opened:
                span.set_attribute("error", True)
        return local

    return local


def _close(opened):
    """End the call's probe spans, innermost first, restoring the caller's context."""
    try:
        from opentelemetry import context

        while opened:
            span, token = opened.pop()
            span.end()
            context.detach(token)
    except Exception:  # noqa: BLE001
        _log.debug("probe span close failed", exc_info=True)


def _install(probes):
    global _active
    _active = probes
    fn = _tracer if probes else None
    every = getattr(threading, "settrace_all_threads", None)  # 3.12+: running threads too
    if every is not None:
        every(fn)
        return
    # shortcut: before 3.12 only the calling thread and threads started later are traced; call
    # Probes.poll() from the main thread (or upgrade Python) to reach code already running.
    sys.settrace(fn)
    threading.settrace(fn)


def start(endpoint=None, key=None, service=None, env="", version="", interval_s=30, emit=None):
    """Poll active probes in a daemon thread; returns the Probes object."""
    p = Probes(endpoint or os.environ.get("WT_API_URL", "https://api.watchtower.dev"),
               key or os.environ.get("WT_INGEST_KEY", ""), service or os.environ.get("OTEL_SERVICE_NAME", "unknown"),
               env, version, emit)

    def loop():
        while True:
            p.poll()
            time.sleep(max(interval_s, 5))

    threading.Thread(target=loop, name="watchtower-probes", daemon=True).start()
    return p
