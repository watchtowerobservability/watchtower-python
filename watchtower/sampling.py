"""Remote sampling (S5): poll GET /v1/sampling and apply the project's head rules in the process.

The decision uses fnv64a of the trace id like the gateway, so every service agrees on a trace.
A failed poll keeps the last good config (initially: keep everything). Spans dropped here never
reach the gateway, so its exact request counts see only what is kept.
"""
import json
import re
import threading
import urllib.parse
import urllib.request

from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult

_FNV_OFFSET, _FNV_PRIME, _MASK = 0xCBF29CE484222325, 0x100000001B3, (1 << 64) - 1


def trace_fraction(trace_id):
    """fnv64a over the 16 trace-id bytes, as a fraction of 2**64."""
    h = _FNV_OFFSET
    for b in trace_id.to_bytes(16, "big"):
        h = ((h ^ b) * _FNV_PRIME) & _MASK
    return h / float(1 << 64)


def _glob(glob, text):
    return re.fullmatch(re.escape(glob).replace(r"\*", ".*").replace(r"\?", "."), text, re.S) is not None


def ratio_for(cfg, service, env, name, attrs):
    """Keep ratio for a root span (1 keeps); the first matching ratio or drop rule wins."""
    if cfg.get("strategyType") == "PROBABILISTIC" and cfg.get("probabilisticSampling"):
        return min(1.0, max(0.0, float(cfg["probabilisticSampling"].get("samplingRate", 1))))
    for r in cfg.get("rules") or []:
        m = r.get("match") or {}
        if r.get("enabled") is False or m.get("min_duration_ms") is not None or m.get("status"):
            continue  # decided at the gateway
        if (m.get("service") and m["service"] != service) or (m.get("env") and m["env"] != env):
            continue
        if not name.startswith(m.get("span_name_prefix") or ""):
            continue
        if m.get("span_name_glob") and not _glob(m["span_name_glob"], name):
            continue
        if m.get("span_name_regex"):
            try:
                if not re.search(m["span_name_regex"], name):
                    continue
            except re.error:
                continue
        if any(str((attrs or {}).get(k)) != v for k, v in (m.get("attrs") or {}).items()):
            continue
        a = r.get("action") or {}
        if a.get("kind") == "drop":
            return 0.0
        if a.get("kind") == "ratio" and a.get("ratio") is not None:
            return min(1.0, max(0.0, float(a["ratio"])))
        return 1.0
    return 1.0


class RemoteSampler(Sampler):
    def __init__(self, endpoint, headers, service, env=None, interval=30.0, opener=urllib.request.urlopen):
        self.cfg, self._etag = {}, ""
        self._endpoint, self._headers, self._service, self._env = endpoint.rstrip("/"), headers, service, env or ""
        self._open, self._stop = opener, threading.Event()
        self._interval = max(1.0, float(interval))
        threading.Thread(target=self._run, name="wt-remote-sampling", daemon=True).start()

    def _run(self):
        self.poll()
        while not self._stop.wait(self._interval):
            self.poll()

    def poll(self):
        """One poll; any failure or a 304 keeps the current config."""
        q = {"service": self._service, **({"env": self._env} if self._env else {})}
        req = urllib.request.Request(f"{self._endpoint}/v1/sampling?{urllib.parse.urlencode(q)}",
                                     headers={**self._headers, **({"If-None-Match": self._etag} if self._etag else {})})
        try:
            with self._open(req, timeout=10) as r:
                self.cfg = json.load(r)
                self._etag = r.headers.get("ETag") or ""
        except Exception:  # noqa: BLE001 - fallback: keep the last good config
            pass

    def stop(self):
        self._stop.set()

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
        r = ratio_for(self.cfg, self._service, self._env, name, attributes)
        keep = r >= 1 or (r > 0 and trace_fraction(trace_id) < r)
        return SamplingResult(Decision.RECORD_AND_SAMPLE if keep else Decision.DROP)

    def get_description(self):
        return "WatchtowerRemoteSampler"
