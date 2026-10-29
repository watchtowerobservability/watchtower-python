from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExportResult

from watchtower import client as wt


class Capture:
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans += list(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


def test_exports_with_key_header():
    c = wt.Client("test-key-not-real", endpoint="http://x:4318", exporter=Capture())
    assert c.headers == {"Authorization": "Bearer test-key-not-real"}
    assert c.endpoint == "http://x:4318"


def test_default_endpoint(monkeypatch):
    monkeypatch.delenv("WT_OTLP_ENDPOINT", raising=False)
    assert wt.Client("k", exporter=Capture()).endpoint == wt.DEFAULT_ENDPOINT


def test_wt_resource_attrs_dropped():
    assert wt.safe_resource({"wt.tenant_id": "x", "env": "p"}) == {"env": "p"}
    c = wt.Client("k", resource={"wt.project_id": "evil"}, exporter=Capture())
    assert "wt.project_id" not in c.provider.resource.attributes


def test_observe_and_redact():
    cap = Capture()
    wt.init("k", exporter=cap, redact=lambda k, v: "[x]" if k == "secret" else v)

    @wt.observe("op")
    def f():
        trace.get_current_span().set_attribute("secret", "hunter2")

    f()
    wt.shutdown()
    assert cap.spans[0].name == "op"
    assert cap.spans[0].attributes["secret"] == "[x]"


def test_evaluate_tolerates_errors():
    c = wt.Client("k", exporter=Capture())
    rows = c.evaluate([{"input": 1}, {"input": 2}], lambda x: x * 2,
                      {"ok": lambda case, out: out == 2, "boom": lambda case, out: 1 / 0})
    assert rows[0]["scores"] == {"ok": True} and "boom" in rows[0]["errors"]


def test_evaluate_resume_reruns_only_failed_cases():
    c = wt.Client("k", exporter=Capture())
    ran, state = [], {"failed": False}

    def task(x):
        ran.append(x)
        if x == 2 and not state["failed"]:
            state["failed"] = True
            raise ValueError("flaky")
        return x

    first = c.evaluate([1, 2, 3], task, {})
    assert "task" in first[1]["errors"]
    ran.clear()
    again = c.evaluate_resume([1, 2, 3], task, {}, first)
    assert ran == [2] and len(again) == 3 and again[1]["errors"] == {}


def test_score_body(monkeypatch):
    c = wt.Client("k", project="p1", api_url="http://api", exporter=Capture())
    seen = {}
    monkeypatch.setattr(c, "_call", lambda m, p, b=None: seen.update(m=m, p=p, b=b) or {})
    c.score("a" * 32, 1, name="q")
    assert seen["p"] == "scores" and seen["b"]["source"] == "sdk"
