import functools
import json
import logging
import os
import urllib.request

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from . import signals

WT_PREFIX = "wt."
DEFAULT_ENDPOINT = "http://localhost:4318"
_client = None


class _Redactor(SpanProcessor):
    """Runs the user's redaction hook over span attributes before export."""

    def __init__(self, hook):
        self.hook = hook

    def on_end(self, span):
        attrs = span._attributes  # noqa: SLF001 - ReadableSpan has no setter
        if attrs is None:
            return
        # The live mapping is immutable once the span ended, so swap in a redacted copy.
        span._attributes = {k: self.hook(k, v) for k, v in attrs.items()}  # noqa: SLF001


def safe_resource(attrs):
    """Drop caller-supplied `wt.*` keys: tenant identity comes only from the key."""
    return {k: v for k, v in (attrs or {}).items() if not k.startswith(WT_PREFIX)}


class Client:
    def __init__(self, api_key, endpoint=None, api_url=None, project=None, service_name=None,
                 resource=None, environment=None, release=None, redact=None, buffer_dir=None, exporter=None,
                 logs=None, metrics=None, remote_sampling=None, sampling_interval=None, code_origin=None):
        if not api_key:
            raise ValueError("api_key is required")
        self.endpoint = (endpoint or os.environ.get("WT_OTLP_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
        self.api_url = (api_url or os.environ.get("WT_API_URL") or self.endpoint).rstrip("/")
        self.project = project or os.environ.get("WT_PROJECT_ID")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        base = {"service.name": service_name or os.environ.get("WT_SERVICE") or "app"}
        env, rel = environment or os.environ.get("WT_ENVIRONMENT"), release or os.environ.get("WT_RELEASE")
        if env:
            base["deployment.environment.name"] = env
        if rel:
            base["service.version"] = rel
        res = Resource.create({**base, **safe_resource(resource)})
        self.remote_sampler = None
        sampler = None
        if remote_sampling or (remote_sampling is None and os.environ.get("WT_REMOTE_SAMPLING") == "1"):
            from opentelemetry.sdk.trace.sampling import ParentBased

            from .sampling import RemoteSampler

            self.remote_sampler = RemoteSampler(
                self.endpoint, self.headers, base["service.name"], env,
                sampling_interval or float(os.environ.get("WT_SAMPLING_INTERVAL_S") or 30))
            sampler = ParentBased(self.remote_sampler)
        self.provider = TracerProvider(resource=res, **({"sampler": sampler} if sampler else {}))
        if code_origin or (code_origin is None and os.environ.get("WT_CODE_ORIGIN") == "1"):
            from .origin import CodeOrigin

            self.provider.add_span_processor(CodeOrigin())
        if redact:
            self.provider.add_span_processor(_Redactor(redact))
        if exporter is None:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            url = f"{self.endpoint}/v1/traces"
            exporter = OTLPSpanExporter(endpoint=url, headers=self.headers)
            if buffer_dir:
                from .buffer import SpoolExporter

                exporter = SpoolExporter(exporter, buffer_dir, url, self.headers)
        self.exporter = exporter
        self.provider.add_span_processor(BatchSpanProcessor(exporter))
        self.tracer = self.provider.get_tracer("watchtower")
        self.logger_provider = self.meter_provider = self._log_handler = None
        if signals.enabled(logs, "WATCHTOWER_LOGS", "OTEL_LOGS_EXPORTER"):
            self.logger_provider, self._log_handler = signals.start_logs(self.endpoint, self.headers, res, buffer_dir)
        if signals.enabled(metrics, "WATCHTOWER_METRICS", "OTEL_METRICS_EXPORTER"):
            self.meter_provider = signals.start_metrics(self.endpoint, self.headers, res, buffer_dir)

    def _call(self, method, path, body=None):
        url = f"{self.api_url}/v1/projects/{self.project}/llm/{path}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={**self.headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)

    def get_prompt(self, prompt_id):
        return self._call("GET", f"prompts/{prompt_id}")

    def score(self, trace_id, value, name="user_feedback", span_id=None, comment=None):
        body = {"trace_id": trace_id, "value": value, "name": name, "source": "sdk"}
        if span_id:
            body["span_id"] = span_id
        if comment:
            body["comment"] = comment
        return self._call("POST", "scores", body)

    def evaluate_resume(self, dataset, task, scorers, previous, **kw):
        """Re-run only the cases that errored or are missing in `previous` (rows of an earlier run)."""
        return self.evaluate(dataset, task, scorers, resume=previous, **kw)

    def evaluate(self, dataset, task, scorers, name="evaluate", error_tolerance=True, resume=None):
        """Run `task` per case under a span; each scorer result is recorded as a score (when a project is set).

        `resume`: rows of an earlier run; cases that finished without errors are kept, not re-run."""
        rows = []
        done = {r["case"]: r for r in (resume or []) if not r.get("errors")}
        for i, case in enumerate(dataset):
            if i in done:
                rows.append(done[i])
                continue
            with self.tracer.start_as_current_span(f"{name}.case") as span:
                tid = format(span.get_span_context().trace_id, "032x")
                row = {"case": i, "trace_id": tid, "scores": {}, "errors": {}}
                rows.append(row)
                try:
                    out = task(case["input"] if isinstance(case, dict) and "input" in case else case)
                except Exception as e:  # noqa: BLE001
                    if not error_tolerance:
                        raise
                    row["errors"]["task"] = str(e)
                    continue
                for sname, fn in scorers.items():
                    try:
                        v = fn(case, out)
                    except Exception as e:  # noqa: BLE001
                        if not error_tolerance:
                            raise
                        row["errors"][sname] = str(e)
                        continue
                    row["scores"][sname] = v
                    if self.project:
                        self.score(tid, v, name=sname)
        return rows

    def shutdown(self):
        if self.remote_sampler is not None:
            self.remote_sampler.stop()
        self.provider.shutdown()
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        for p in (self.logger_provider, self.meter_provider):
            if p is not None:
                p.shutdown()
        self.logger_provider = self.meter_provider = None


_profiler = None


def init(api_key=None, capture_errors=True, capture_rules=None, capture_locals=None, **kw):
    global _client
    _client = Client(api_key or os.environ.get("WT_INGEST_KEY"), **kw)
    if capture_rules or (capture_rules is None and os.environ.get("WT_CAPTURE_RULES") == "1"):
        from . import capture

        capture.apply(_client.api_url, api_key or os.environ.get("WT_INGEST_KEY"), kw.get("service_name"))
    if capture_errors:
        from .errors import enable_locals, install_hooks

        install_hooks()
        # exception replay: opt-in here (or WT_EXCEPTION_LOCALS=1) and in the project's issue settings
        enable_locals(capture_locals if capture_locals is not None else os.environ.get("WT_EXCEPTION_LOCALS") == "1")
    if os.environ.get("WT_PROFILING") in ("1", "true"):  # also set by the agent's lambda_instrument command
        from . import profiling

        global _profiler
        if _profiler is not None:
            _profiler.stop()
        try:
            _profiler = profiling.start(api_key=api_key, service_name=kw.get("service_name"))
        except ValueError:  # no key: profiling is best effort, never break init
            _profiler = None
    return _client


def _need():
    if _client is None:
        raise RuntimeError("watchtower.init() has not been called")
    return _client


def observe(name=None):
    """Decorator: wrap a function in a span named after it."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            with _need().tracer.start_as_current_span(name or fn.__qualname__):
                return fn(*a, **kw)
        return wrapper
    return deco


def get_prompt(prompt_id):
    return _need().get_prompt(prompt_id)


def score(trace_id, value, **kw):
    return _need().score(trace_id, value, **kw)


def evaluate(dataset, task, scorers, **kw):
    return _need().evaluate(dataset, task, scorers, **kw)


def evaluate_resume(dataset, task, scorers, previous, **kw):
    return _need().evaluate_resume(dataset, task, scorers, previous, **kw)


def shutdown():
    from .errors import uninstall_hooks

    uninstall_hooks()
    if _client:
        _client.shutdown()
