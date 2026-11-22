"""Payload and header capture rules (APM-016), set in Watchtower per service and route.

`apply(endpoint, key, service)` fetches `GET /v1/capture-rules?service=` (an ingestion key with
scope `probes:read`) once and turns the header lists into the OpenTelemetry Python settings the
HTTP server instrumentations read when they instrument an app
(`OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST`/`_RESPONSE`), so it must run before
`instrument(app)`. A variable the user already set wins. Credential headers never come back from
the server. `init(capture_rules=True)` (or `WT_CAPTURE_RULES=1`) calls it.

Bodies and GraphQL: `record(span, route, ...)` keeps at most the matching rule's `body_bytes`
(hard cap 8 KiB) of text-like request and response bodies as `http.request.body` /
`http.response.body`, after the default secret and PII detectors (a port of the pipeline's
redact stage; ingest applies the project's detectors again), plus `graphql.operation.type`,
`graphql.operation.name` and (with `graphql_query`) `graphql.document`. The Flask, Django
and FastAPI integrations call it. shortcut: rules are fetched once at init; poll when live edits ask for it.
"""
import json
import os
import re
import urllib.parse
import urllib.request

REQ = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST"
RESP = "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE"


def env_for(rules):
    """The header settings for `rules` (a list from the server): the union of every rule's names."""
    req, resp = [], []
    for r in rules:
        for src, dst in ((r.get("request_headers") or [], req), (r.get("response_headers") or [], resp)):
            for h in src:
                if isinstance(h, str) and h and h.lower() not in dst:
                    dst.append(h.lower())
    out = {}
    if req:
        out[REQ] = ",".join(req)
    if resp:
        out[RESP] = ",".join(resp)
    return out


def apply(endpoint=None, key=None, service=None, timeout_s=3, environ=None):
    """Fetch the rules and set the variables; returns the rules (empty on any failure)."""
    environ = os.environ if environ is None else environ
    base = (endpoint or environ.get("WT_API_URL", "https://api.watchtower.dev")).rstrip("/")
    q = urllib.parse.urlencode({"service": service or environ.get("OTEL_SERVICE_NAME", "unknown")})
    req = urllib.request.Request(f"{base}/v1/capture-rules?{q}",
                                 headers={"Authorization": f"Bearer {key or environ.get('WT_INGEST_KEY', '')}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310 - endpoint is the operator's
            rules = json.loads(r.read(1 << 20)).get("rules", [])
    except (OSError, ValueError):
        return []
    for k, v in env_for(rules).items():
        environ.setdefault(k, v)
    RULES[:] = [r for r in rules if isinstance(r, dict)]
    return rules


HARD_MAX = 8192
RULES = []
_R = "[REDACTED]"
_SECRET_KEYS = r"password|passwd|secret|token|access_token|refresh_token|client_secret|api_?key|authorization"
_JSON_SECRET = re.compile(r'("(?:' + _SECRET_KEYS + r')"\s*:\s*)"(?:[^"\\]|\\.)*"', re.I)
_FORM_SECRET = re.compile(r"\b((?:" + _SECRET_KEYS + r")=)[^&\s]*", re.I)
_PATTERNS = [re.compile(p, f) for p, f in (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", 0),
    (r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", 0),
    (r"bearer\s+[A-Za-z0-9._~+/-]+=*", re.I),
    (r"wt[ik]_[a-z0-9]{8}_[A-Za-z0-9_-]{43}", 0),
    (r"(?:AKIA|ASIA)[0-9A-Z]{16}", 0),
    (r"gh[pousr]_[A-Za-z0-9]{36,}", 0),
    (r"[rs]k_(?:live|test)_[A-Za-z0-9]{16,}", 0),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", 0),
    (r"\b\d{3}-\d{2}-\d{4}\b", 0),
)]
_CARD = re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{1,7}\b")
_GQL = re.compile(r"(?:^|\})\s*(query|mutation|subscription)\b\s*([_A-Za-z][_0-9A-Za-z]*)?")
_TEXT = re.compile(r"json|^text/|xml|x-www-form-urlencoded|graphql", re.I)


def _glob(pat, s):
    return re.fullmatch(".*".join(re.escape(p) for p in pat.split("*")), s, re.S) is not None


def rule_for(route):
    return next((r for r in RULES if not r.get("route") or _glob(r["route"], route)), None)


def _luhn(s):
    d = [int(c) for c in s if c.isdigit()]
    total = sum(x if i % 2 == 0 else (x * 2 - 9 if x * 2 > 9 else x * 2) for i, x in enumerate(reversed(d)))
    return len(d) >= 13 and total % 10 == 0


def redact_text(s):
    """Default secret and PII detectors: secret-named JSON and form fields, keys, tokens, emails, SSNs, Luhn-valid cards."""
    s = _FORM_SECRET.sub(r"\1" + _R, _JSON_SECRET.sub(r'\1"' + _R + '"', s))
    for p in _PATTERNS:
        s = p.sub(_R, s)
    return _CARD.sub(lambda m: _R if _luhn(m.group(0)) else m.group(0), s)


def _cap(s, n):
    return s.encode("utf-8")[:n].decode("utf-8", "ignore")


def excerpt(s, n):
    """Redact a wider window than kept so a secret cut at the edge is still masked, then cap."""
    return _cap(redact_text(_cap(s, n * 2)), n)


def graphql_op(doc):
    """(type, name) of a GraphQL document, `{ ... }` being an anonymous query; None when it is not one."""
    d = re.sub(r"#[^\n]*", "", doc)
    m = _GQL.search(d)
    if m:
        return m.group(1), m.group(2)
    return ("query", None) if d.lstrip().startswith("{") else None


def text_like(ctype):
    return bool(ctype) and _TEXT.search(ctype) is not None


def _text(body, ctype):
    if body is None or (ctype and not _TEXT.search(ctype)):
        return None
    if isinstance(body, bytes):
        return body[: HARD_MAX * 2].decode("utf-8", "ignore")
    return body if isinstance(body, str) else None


def record(span, route, req_type=None, req_body=None, res_type=None, res_body=None):
    """Set the body and GraphQL attributes the rule matching `route` asks for; no rule, nothing."""
    rule = rule_for(route or "")
    if rule is None or span is None:
        return
    n = min(int(rule.get("body_bytes") or 0), HARD_MAX)
    req, res = _text(req_body, req_type), _text(res_body, res_type)
    if n and req:
        span.set_attribute("http.request.body", excerpt(req, n))
    if n and res:
        span.set_attribute("http.response.body", excerpt(res, n))
    doc, name = None, None
    if req and "graphql" in (req_type or "").lower():
        doc = req
    elif req:
        try:
            j = json.loads(req)
        except ValueError:
            j = None
        if isinstance(j, dict) and isinstance(j.get("query"), str):
            doc, name = j["query"], j.get("operationName") if isinstance(j.get("operationName"), str) else None
    op = graphql_op(doc) if doc else None
    if op is None:
        return
    span.set_attribute("graphql.operation.type", op[0])
    if name or op[1]:
        span.set_attribute("graphql.operation.name", name or op[1])
    if rule.get("graphql_query"):
        span.set_attribute("graphql.document", excerpt(doc, HARD_MAX))
