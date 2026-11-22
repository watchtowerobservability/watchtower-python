"""App and API Protection in-app WAF (W52).

`start(endpoint, key, service)` polls the project's ruleset from the edge
(`GET /v1/appsec/ruleset`, an ingestion key with scope `probes:read`, ETag, every `poll_s`) in a
daemon thread: mode (`monitor` or `block`), the enabled pattern rules and the denylist.
`AppsecMiddleware(app)` (WSGI: Flask, Django) and `AppsecASGIMiddleware(app)` (FastAPI,
Starlette) inspect every request before the app: path, query, User-Agent and the other header
values, percent-decoded once, against the rules the worker runs over stored spans. A match is
recorded on the current span (`appsec.event`, `appsec.rule_id`, `appsec.category`,
`appsec.blocked`); in `block` mode, and for denylisted IPs or users in either mode, the request
gets 403 problem+json and the app never runs. Install the middleware inside the OpenTelemetry
one so the server span is current. Rules are matched case-insensitively; one that Python cannot
compile is skipped. Nothing happens until `start` (or `set_ruleset`) is called.
"""
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from opentelemetry import trace

DENYLIST = "denylist"
_PROBLEM = json.dumps({"type": "about:blank", "title": "Forbidden", "status": 403,
                       "detail": "Request blocked by Watchtower App and API Protection"}).encode()
_state = {"mode": "monitor", "rules": [], "ip": {}, "user": {}}
_lock = threading.Lock()


def _expiry(entries):
    return {e["value"]: (e.get("expires_at") or float("inf")) for e in entries or [] if isinstance(e, dict) and e.get("value")}


def set_ruleset(r):
    """Replace the active ruleset (a dict as the edge serves it; None clears it)."""
    r = r or {}
    rules = []
    for rule in r.get("rules") or []:
        try:
            rules.append((rule, re.compile(rule["pattern"], re.I)))
        except (re.error, KeyError, TypeError):
            continue  # not portable to Python: the worker still evaluates it
    deny = r.get("denylist") or {}
    with _lock:
        _state.update(mode="block" if r.get("mode") == "block" else "monitor", rules=rules,
                      ip=_expiry(deny.get("ip")), user=_expiry(deny.get("user")))


def _listed(kind, value, now):
    return bool(value) and _state[kind].get(value, 0) > now


def is_user_blocked(user_id, now=None):
    """Whether `user_id` is denylisted (call after authentication to refuse a listed user)."""
    return _listed("user", user_id, time.time() if now is None else now)


def active():
    return bool(_state["rules"] or _state["ip"] or _state["user"])


def inspect(req, now=None):
    """Judge one request: a dict with `path`, `query`, `user_agent`, `body`, `headers`, `ip`, `user`."""
    now = time.time() if now is None else now
    if _listed("ip", req.get("ip"), now) or _listed("user", req.get("user"), now):
        return {"action": "block", "rule_id": DENYLIST, "category": "abuse"}
    for rule, rx in _state["rules"]:
        for f in rule.get("fields") or []:
            v = req.get(f)
            m = rx.search(v) if v else None
            if m:
                action = "block" if _state["mode"] == "block" else "monitor"
                return {"action": action, "rule_id": rule.get("id", ""), "category": rule.get("category", ""),
                        "match": m.group(0)[:200]}
    return {"action": "allow"}


def _client_ip(headers, peer):
    xff = headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return headers.get("x-real-ip") or peer or ""


def _request(raw_path, raw_query, headers, peer, user):
    """shortcut: trusts X-Forwarded-For for the client IP; run behind a proxy that overwrites it."""
    return {
        "path": urllib.parse.unquote(raw_path),
        "query": urllib.parse.unquote_plus(raw_query),
        "user_agent": headers.get("user-agent", ""),
        "headers": "\n".join(v for k, v in headers.items() if k != "user-agent"),
        "ip": _client_ip(headers, peer),
        "user": user,
    }


def from_wsgi(environ, user=None):
    headers = {k[5:].replace("_", "-").lower(): v for k, v in environ.items() if k.startswith("HTTP_")}
    path = environ.get("RAW_URI") or environ.get("REQUEST_URI") or ""
    path = path.split("?", 1)[0] or urllib.parse.quote(environ.get("PATH_INFO", "/").encode("latin-1", "replace"))
    return _request(path, environ.get("QUERY_STRING", ""), headers, environ.get("REMOTE_ADDR"), user)


def from_asgi(scope, user=None):
    headers = {}
    for k, v in scope.get("headers") or []:
        name = k.decode("latin-1").lower()
        headers[name] = f"{headers[name]}, {v.decode('latin-1')}" if name in headers else v.decode("latin-1")
    raw = scope.get("raw_path")
    path = raw.decode("latin-1") if raw else urllib.parse.quote(scope.get("path", "/"))
    peer = (scope.get("client") or ("",))[0]
    return _request(path, (scope.get("query_string") or b"").decode("latin-1"), headers, peer, user)


def annotate(decision, ip=None):
    span = trace.get_current_span()
    if not span.is_recording():
        return
    if ip:
        span.set_attribute("appsec.client_ip", ip)
    if decision["action"] == "allow":
        return
    span.set_attributes({"appsec.event": True, "appsec.rule_id": decision.get("rule_id", ""),
                         "appsec.category": decision.get("category", ""),
                         "appsec.blocked": decision["action"] == "block"})


def _judge(req):
    d = inspect(req)
    annotate(d, req.get("ip"))
    return d["action"] == "block"


def track_login(login, success):
    """Mark the current request as a login attempt; failed ones feed the account-takeover rules."""
    span = trace.get_current_span()
    span.set_attributes({"appsec.login": login, "appsec.login.success": bool(success)})
    if success:
        span.set_attribute("appsec.auth", True)


class AppsecMiddleware:
    """WSGI middleware; `user(environ)` names the caller when known."""

    def __init__(self, app, user=None):
        self.app, self.user = app, user

    def __call__(self, environ, start_response):
        if active() and _judge(from_wsgi(environ, self.user(environ) if self.user else None)):
            start_response("403 Forbidden", [("Content-Type", "application/problem+json"),
                                             ("Content-Length", str(len(_PROBLEM)))])
            return [_PROBLEM]
        return self.app(environ, start_response)


class AppsecASGIMiddleware:
    """ASGI middleware (HTTP scopes only); `user(scope)` names the caller when known."""

    def __init__(self, app, user=None):
        self.app, self.user = app, user

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and active() and _judge(from_asgi(scope, self.user(scope) if self.user else None)):
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/problem+json"),
                                    (b"content-length", str(len(_PROBLEM)).encode())]})
            await send({"type": "http.response.body", "body": _PROBLEM})
            return
        await self.app(scope, receive, send)


def fetch(endpoint, key, service, etag="", timeout_s=5):
    """One poll: (ruleset or None when unchanged or failed, etag)."""
    q = urllib.parse.urlencode({"service": service})
    headers = {"Authorization": f"Bearer {key}"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(f"{endpoint}/v1/appsec/ruleset?{q}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:  # noqa: S310 - endpoint is the operator's
            return json.loads(r.read(4 << 20)), r.headers.get("ETag", "")
    except (OSError, ValueError):  # 304 arrives as HTTPError, an OSError
        return None, etag


def start(endpoint=None, key=None, service=None, environ=None):
    """Fetch the ruleset now and keep polling in a daemon thread; returns a `threading.Event` that stops it."""
    environ = os.environ if environ is None else environ
    base = (endpoint or environ.get("WT_API_URL", "https://api.watchtower.dev")).rstrip("/")
    key = key or environ.get("WT_INGEST_KEY", "")
    service = service or environ.get("OTEL_SERVICE_NAME", "unknown")
    stop = threading.Event()
    state = {"etag": "", "poll": 30}

    def once():
        body, state["etag"] = fetch(base, key, service, state["etag"])
        if isinstance(body, dict):
            set_ruleset(body)
            state["poll"] = max(10, int(body.get("poll_s") or 30))

    def loop():
        while not stop.wait(state["poll"]):
            once()

    once()
    threading.Thread(target=loop, name="watchtower-appsec", daemon=True).start()
    return stop
