from types import SimpleNamespace as NS

import pytest

import watchtower
from watchtower.integrations import aws_lambda, genai


def test_lambda_wrap_success_and_error(cap):
    ctx = NS(function_name="fn", aws_request_id="rid")

    @aws_lambda.wrap
    def ok(event, context):
        return {"ok": 1}

    @aws_lambda.wrap
    def bad(event, context):
        raise ValueError("nope")

    assert ok({}, ctx) == {"ok": 1}
    with pytest.raises(ValueError):
        bad({}, ctx)
    assert cap.spans[0].attributes["faas.invocation_id"] == "rid"
    assert cap.spans[1].status.status_code.name == "ERROR"


def test_openai_span(cap):
    res = NS(model="gpt-4o", id="r1", usage=NS(prompt_tokens=3, completion_tokens=5),
             choices=[NS(finish_reason="stop")])
    client = NS(chat=NS(completions=NS(create=lambda **kw: res)))
    genai.instrument_openai(client).chat.completions.create(model="gpt-4o", messages=[{"content": "secret"}])
    a = cap.spans[0].attributes
    assert a["gen_ai.system"] == "openai" and a["gen_ai.usage.input_tokens"] == 3
    assert a["gen_ai.response.finish_reasons"] == ("stop",)
    assert "secret" not in str(a)


def test_anthropic_span_and_error(cap):
    res = NS(model="claude", id="m1", usage=NS(input_tokens=7, output_tokens=2), stop_reason="end_turn")
    client = NS(messages=NS(create=lambda **kw: res))
    genai.instrument_anthropic(client).messages.create(model="claude")
    a = cap.spans[0].attributes
    assert a["gen_ai.usage.output_tokens"] == 2 and a["gen_ai.response.finish_reasons"] == ("end_turn",)

    def boom(**kw):
        raise RuntimeError("api down")

    client = NS(messages=NS(create=boom))
    with pytest.raises(RuntimeError):
        genai.instrument_anthropic(client).messages.create(model="claude")
    assert cap.spans[1].status.status_code.name == "ERROR"


def test_langchain_handler(cap):
    h = genai.WatchtowerCallbackHandler()
    h.on_llm_start({}, [], run_id="1", invocation_params={"model": "m"})
    h.on_llm_end(NS(llm_output={"token_usage": {"prompt_tokens": 1, "completion_tokens": 2}}), run_id="1")
    assert cap.spans[0].attributes["gen_ai.usage.output_tokens"] == 2


def test_flask(cap):
    flask = pytest.importorskip("flask")
    from watchtower.integrations import flask as wf

    app = flask.Flask(__name__)

    @app.route("/x")
    def x():
        raise ValueError("flask boom")

    wf.instrument(app)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    assert app.test_client().get("/x").status_code == 500
    assert any(e.name == "exception" for s in cap.spans for e in s.events)


def test_fastapi(cap):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from watchtower.integrations import fastapi as wfa

    app = FastAPI()

    @app.get("/x")
    def x():
        raise ValueError("fastapi boom")

    wfa.instrument(app)
    with pytest.raises(ValueError):
        TestClient(app).get("/x")
    assert any(e.name == "exception" for s in cap.spans for e in s.events)


def test_django_middleware(cap):
    pytest.importorskip("django")
    from watchtower.integrations.django import WatchtowerMiddleware

    WatchtowerMiddleware(lambda r: None).process_exception(NS(resolver_match=NS(route="a/")), ValueError("dj"))
    assert cap.spans[0].attributes["http.route"] == "a/"


def test_celery_failure_signal(cap):
    pytest.importorskip("celery")
    from watchtower.integrations import celery as wc

    wc._on_failure(sender=NS(name="tasks.add"), exception=ValueError("c"))
    assert cap.spans[0].attributes["celery.task"] == "tasks.add"


def test_client_env_names(monkeypatch):
    monkeypatch.setenv("WT_SERVICE", "svc")
    monkeypatch.setenv("WT_ENVIRONMENT", "prod")
    monkeypatch.setenv("WT_RELEASE", "1.2.3")
    from conftest import Capture

    r = watchtower.Client("k", exporter=Capture()).provider.resource.attributes
    assert (r["service.name"], r["deployment.environment.name"], r["service.version"]) == ("svc", "prod", "1.2.3")
