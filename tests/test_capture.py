import json

from watchtower import capture


def test_env_is_the_union_of_header_lists():
    env = capture.env_for([
        {"request_headers": ["X-Request-Id", "x-tenant"], "response_headers": ["x-cache"]},
        {"request_headers": ["x-request-id"], "response_headers": []},
    ])
    assert env == {capture.REQ: "x-request-id,x-tenant", capture.RESP: "x-cache"}
    assert capture.env_for([]) == {}


def test_apply_keeps_user_settings_and_survives_failures(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, _n):
            return json.dumps({"rules": [{"request_headers": ["x-a"], "response_headers": ["x-b"]}]}).encode()

    seen = []
    monkeypatch.setattr(capture.urllib.request, "urlopen", lambda req, timeout: seen.append(req) or Resp())
    env = {capture.RESP: "x-mine"}
    rules = capture.apply("http://edge", "k", "checkout", environ=env)
    assert rules and env[capture.REQ] == "x-a" and env[capture.RESP] == "x-mine"
    assert seen[0].full_url == "http://edge/v1/capture-rules?service=checkout"
    assert seen[0].get_header("Authorization") == "Bearer k"

    def boom(req, timeout):
        raise OSError("down")

    monkeypatch.setattr(capture.urllib.request, "urlopen", boom)
    assert capture.apply("http://edge", "k", "x", environ={}) == []


class _Span:
    def __init__(self):
        self.attrs = {}

    def set_attribute(self, k, v):
        self.attrs[k] = v


def test_bodies_are_capped_redacted_and_route_scoped(monkeypatch):
    monkeypatch.setattr(capture, "RULES", [{"route": "/api/*", "body_bytes": 40}])
    s = _Span()
    body = json.dumps({"user": "a@b.io", "password": "hunter2", "card": "4111 1111 1111 1111"}).encode()
    capture.record(s, "/api/login", "application/json", body, "image/png", b"\x89PNG")
    got = s.attrs["http.request.body"]
    assert len(got.encode()) <= 40 and "hunter2" not in got and "a@b.io" not in got
    assert "http.response.body" not in s.attrs
    other = _Span()
    capture.record(other, "/health", "text/plain", b"x")
    assert other.attrs == {}
    assert capture.redact_text("4111 1111 1111 1112") == "4111 1111 1111 1112"


def test_graphql_operation_and_document(monkeypatch):
    monkeypatch.setattr(capture, "RULES", [{"graphql_query": True}])
    s = _Span()
    q = '# c\nmutation AddUser { add(email: "x@y.io") { id } }'
    capture.record(s, "/graphql", "application/json", json.dumps({"query": q}).encode())
    assert s.attrs["graphql.operation.type"] == "mutation"
    assert s.attrs["graphql.operation.name"] == "AddUser"
    assert "x@y.io" not in s.attrs["graphql.document"]
    assert capture.graphql_op("{ me { id } }") == ("query", None)
    assert capture.graphql_op("shoes") is None


def test_asgi_tee_records_what_the_app_read_and_sent(monkeypatch):
    import asyncio

    from watchtower.integrations import fastapi as wfa

    monkeypatch.setattr(capture, "RULES", [{"route": "/login", "body_bytes": 200}])
    span = _Span()
    monkeypatch.setattr(wfa.trace, "get_current_span", lambda: span)
    sent = []

    class Route:
        path = "/login"

    async def app(scope, receive, send):
        assert (await receive())["body"] == b'{"password": "hunter2"}'
        scope["route"] = Route()  # what FastAPI's router does
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"email": "a@b.io"}'})

    async def receive():
        return {"type": "http.request", "body": b'{"password": "hunter2"}', "more_body": False}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "path": "/login", "headers": [(b"content-type", b"application/json")]}
    asyncio.run(wfa.CaptureMiddleware(app)(scope, receive, send))
    assert len(sent) == 2 and sent[1]["body"] == b'{"email": "a@b.io"}'
    assert span.attrs["http.request.body"] == '{"password": "[REDACTED]"}'
    assert span.attrs["http.response.body"] == '{"email": "[REDACTED]"}'

    monkeypatch.setattr(capture, "RULES", [])
    quiet = _Span()
    monkeypatch.setattr(wfa.trace, "get_current_span", lambda: quiet)
    asyncio.run(wfa.CaptureMiddleware(app)(dict(scope), receive, send))
    assert quiet.attrs == {}
