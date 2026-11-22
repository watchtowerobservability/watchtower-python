"""Continuous wall-clock profiler (pure Python, no native agent).

A daemon thread samples every thread's stack ``hz`` times a second through
``sys._current_frames()``, folds the stacks and posts them every ``period`` seconds to the
gateway's Pyroscope-compatible ``/ingest`` as ``<service>.wall`` (format ``folded``).
Labels (``env``, ``version`` and ``tags``) become profile labels. The sampler thread itself
is never sampled. Uploads that fail are dropped (profiles are best effort).

``with span():`` labels samples with the OpenTelemetry span, so a trace links to its exact
profile. The label belongs to the frame of the function that entered it, and a sample carries it
while that frame is on the sampled stack: with asyncio it follows the task across ``await`` and
other tasks on the loop thread stay unlabeled. Tasks the block starts are not labeled.
"""
import contextlib
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

DEFAULT_ENDPOINT = "http://localhost:4318"
MAX_DEPTH = 128


def _frame_name(f):
    co = f.f_code
    return f"{co.co_name} ({os.path.basename(co.co_filename)}:{co.co_firstlineno})"


def fold(frame):
    """Root-first ``a;b;c`` of one stack (``;`` in names replaced)."""
    out = []
    while frame is not None and len(out) < MAX_DEPTH:
        out.append(_frame_name(frame).replace(";", ":"))
        frame = frame.f_back
    return ";".join(reversed(out))


_spans = {}  # id(frame) -> (frame, [label prefixes of the spans it entered, innermost last])


def span_prefix(span_id, trace_id):
    return f"span_id={span_id:016x},trace_id={trace_id:032x}\t"


@contextlib.contextmanager
def span(span_id=None, trace_id=None):
    """Label this thread's samples with a span (default: the current OpenTelemetry span)."""
    if span_id is None:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        span_id, trace_id = ctx.span_id, ctx.trace_id
    if not span_id:
        yield
        return
    caller = sys._getframe(2)  # this generator, then contextlib's __enter__, then the caller
    entry = _spans.setdefault(id(caller), (caller, []))
    entry[1].append(span_prefix(span_id, trace_id or 0))
    try:
        yield
    finally:
        entry[1].pop()
        if not entry[1]:
            _spans.pop(id(caller), None)


def _label(frame):
    """The innermost span label of the frames on this stack."""
    while frame is not None and _spans:
        entry = _spans.get(id(frame))
        if entry is not None and entry[0] is frame and entry[1]:
            return entry[1][-1]
        frame = frame.f_back
    return ""


def sample_once(counts, skip_ident=None):
    for ident, frame in sys._current_frames().items():
        if ident == skip_ident:
            continue
        key = fold(frame)
        if key:
            key = _label(frame) + key
            counts[key] = counts.get(key, 0) + 1


def folded_body(counts):
    return "".join(f"{k} {n}\n" for k, n in counts.items()).encode()


def ingest_url(endpoint, service, tags, start, end, hz):
    labels = ",".join(f"{k}={v}" for k, v in sorted(tags.items()) if v)
    q = {"name": f"{service}.wall{{{labels}}}", "from": int(start), "until": int(end),
         "format": "folded", "sampleRate": hz, "spyName": "watchtower-python", "units": "samples"}
    return f"{endpoint.rstrip('/')}/ingest?{urllib.parse.urlencode(q)}"


class Profiler:
    def __init__(self, api_key=None, endpoint=None, service_name=None, environment=None,
                 release=None, tags=None, hz=100, period=15.0):
        self.api_key = api_key or os.environ.get("WT_INGEST_KEY")
        if not self.api_key:
            raise ValueError("api_key is required")
        self.endpoint = endpoint or os.environ.get("WT_OTLP_ENDPOINT") or DEFAULT_ENDPOINT
        self.service = service_name or os.environ.get("WT_SERVICE") or "app"
        self.tags = {"env": environment or os.environ.get("WT_ENVIRONMENT"),
                     "version": release or os.environ.get("WT_RELEASE"), **(tags or {})}
        self.hz, self.period = max(1, min(int(hz), 250)), max(1.0, float(period))
        self._stop = threading.Event()
        self._thread = None
        self.uploaded = self.dropped = 0

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="watchtower-profiler", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.period + 5)
            self._thread = None

    def _run(self):
        me = threading.get_ident()
        while not self._stop.is_set():
            counts, start = {}, time.time()
            while not self._stop.is_set() and time.time() - start < self.period:
                sample_once(counts, me)
                self._stop.wait(1.0 / self.hz)
            if counts:
                self.upload(counts, start, time.time())

    def upload(self, counts, start, end):
        req = urllib.request.Request(ingest_url(self.endpoint, self.service, self.tags, start, end, self.hz),
                                     data=folded_body(counts), method="POST",
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "text/plain"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            self.uploaded += 1
        except Exception:  # noqa: BLE001 - best effort, never raise into the app
            self.dropped += 1


def start(**kw):
    """Start a :class:`Profiler` with the SDK's environment defaults and return it."""
    return Profiler(**kw).start()
