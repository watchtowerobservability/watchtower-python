import asyncio
import json

from watchtower import appsec

RULES = {
    "mode": "monitor",
    "rules": [
        {"id": "sqli-union", "category": "sqli", "fields": ["query", "body", "path"],
         "pattern": r"\bunion\b(?:\s+all)?\s+(?:distinct\s+)?select\b"},
        {"id": "scanner-ua", "category": "scanner", "fields": ["user_agent"], "pattern": "sqlmap|nikto"},
        {"id": "broken", "category": "custom", "fields": ["path"], "pattern": "("},
    ],
    "denylist": {"ip": [{"value": "203.0.113.9", "expires_at": None}], "user": [{"value": "mallory", "expires_at": 1}]},
}


def teardown_function():
    appsec.set_ruleset(None)


def wsgi(query="", headers=None, peer="198.51.100.7"):
    env = {"PATH_INFO": "/search", "QUERY_STRING": query, "REMOTE_ADDR": peer}
    for k, v in (headers or {}).items():
        env["HTTP_" + k.upper().replace("-", "_")] = v
    return env


def test_requests_are_decoded_and_matched():
    appsec.set_ruleset(RULES)
    r = appsec.from_wsgi(wsgi("q=1%20UNION+SELECT%20pw", {"X-Forwarded-For": "192.0.2.1, 10.0.0.1"}))
    assert r["query"] == "q=1 UNION SELECT pw"
    assert r["ip"] == "192.0.2.1"
    d = appsec.inspect(r)
    assert (d["action"], d["rule_id"]) == ("monitor", "sqli-union")
    assert appsec.inspect({"path": "/", "user_agent": "sqlmap/1.7"})["rule_id"] == "scanner-ua"
    assert appsec.inspect({"path": "/books", "query": "q=union station"})["action"] == "allow"
    assert not appsec.is_user_blocked("mallory"), "expired entry"


def test_wsgi_blocks_in_block_mode_and_for_denylisted_ips():
    seen = {}

    def app(environ, start_response):
        start_response("200 OK", [])
        return [b"ok"]

    def start_response(status, headers):
        seen["status"] = status

    mw = appsec.AppsecMiddleware(app)
    appsec.set_ruleset(dict(RULES, mode="block"))
    body = mw(wsgi("q=1 union select 2"), start_response)
    assert seen["status"].startswith("403") and json.loads(body[0])["status"] == 403
    appsec.set_ruleset(RULES)
    assert mw(wsgi("q=1 union select 2"), start_response) == [b"ok"]
    mw(wsgi("", peer="203.0.113.9"), start_response)
    assert seen["status"].startswith("403")


def test_asgi_blocks_before_the_app():
    appsec.set_ruleset(dict(RULES, mode="block"))
    sent, called = [], []

    async def app(scope, receive, send):
        called.append(True)

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "path": "/x", "raw_path": b"/x", "query_string": b"a=1+union+select+2",
             "headers": [(b"user-agent", b"curl")], "client": ("192.0.2.5", 1)}
    asyncio.run(appsec.AppsecASGIMiddleware(app)(scope, None, send))
    assert not called and sent[0]["status"] == 403


def test_poll_uses_key_and_etag(monkeypatch):
    seen = {}

    class Resp:
        headers = {"ETag": '"abc"'}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return json.dumps(RULES).encode()

    def fake(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return Resp()

    monkeypatch.setattr(appsec.urllib.request, "urlopen", fake)
    body, etag = appsec.fetch("https://edge.test", "wti_x", "api")
    assert seen["url"] == "https://edge.test/v1/appsec/ruleset?service=api"
    assert seen["auth"] == "Bearer wti_x" and etag == '"abc"' and body["mode"] == "monitor"
