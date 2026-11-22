import sys

import pytest

from watchtower import probes


def test_condition_subset_and_no_calls():
    ok = probes.compile_condition("user.plan == 'pro' && count >= 2 && !(region in ['eu'])")
    assert ok({"user": {"plan": "pro"}, "count": 3, "region": "us"})
    assert not ok({"user": {"plan": "free"}, "count": 3, "region": "us"})
    assert probes.compile_condition("")({})
    with pytest.raises(ValueError):
        probes.compile_condition("os.system('x')")
    with pytest.raises(ValueError):
        probes.compile_condition("a ==")


def test_capture_limits_depth_and_bytes():
    out = probes.capture({"a": {"b": {"c": {"d": {"e": 1}}}}, "s": "x" * 5000}, 2, 8192)
    assert out["a.b.c"] == "[depth limit]"
    assert len(out["s"]) == probes.VALUE_MAX


def target(order_id, total):
    note = "big" if total > 100 else "small"
    return note


@pytest.mark.skipif(sys.gettrace() is not None, reason="a coverage or debugger tracer owns settrace")
def test_function_and_line_probes_snapshot_locals():
    got = []
    p = probes.Probes("http://unused", "k", "svc", emit=lambda name, attrs: got.append((name, attrs)))
    line = target.__code__.co_firstlineno + 2  # the return line, after `note` is set
    p.set([
        {"id": "pr_fn", "location": {"function": "target"}, "condition": "total > 100", "capture": {}, "max_rate_per_min": 1},
        {"id": "pr_line", "location": {"file": "test_probes.py", "line": line}, "condition": "", "capture": {}},
    ])
    try:
        target("o1", 500)
        target("o2", 500)  # the function probe's rate limit (1/min) drops this one
        target("o3", 5)    # condition false for the function probe
    finally:
        p.set([])
    fn = [a for _, a in got if a["probe.id"] == "pr_fn"]
    assert len(fn) == 1 and fn[0]["code.snapshot.order_id"] == "o1"
    lines = [a for _, a in got if a["probe.id"] == "pr_line"]
    assert [a["code.snapshot.note"] for a in lines] == ["big", "big", "small"]
    assert sys.gettrace() is None


def charge(user, amount):
    inner = amount * 2
    return inner


@pytest.mark.skipif(sys.gettrace() is not None, reason="a coverage or debugger tracer owns settrace")
def test_span_probe_wraps_call_and_decoration_tags_it():
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    p = probes.Probes("http://unused", "k", "svc", emit=lambda *_: None)
    p.tracer = provider.get_tracer("test")
    line = charge.__code__.co_firstlineno + 2  # after `inner` is set
    p.set([
        {"id": "pr_span", "kind": "span", "location": {"function": "charge"}, "condition": "amount > 5", "capture": {}},
        {"id": "pr_tag", "kind": "span_decoration", "location": {"file": "test_probes.py", "line": line},
         "condition": "", "capture": {"tags": [{"name": "user.plan", "value": "{user.plan}/{inner}"}]}},
    ])
    try:
        assert charge({"plan": "pro"}, 10) == 20
        charge({"plan": "free"}, 1)  # condition false: no span
    finally:
        p.set([])
    spans = exp.get_finished_spans()
    assert [s.name for s in spans] == ["probe charge"]
    assert spans[0].attributes["probe.id"] == "pr_span"
    # The decoration ran inside the probe span (it was current), so the tag landed on it.
    assert spans[0].attributes["user.plan"] == "pro/20"
    assert trace.get_current_span() is trace.INVALID_SPAN or not trace.get_current_span().is_recording()


def test_render_fills_templates_from_locals():
    assert probes.render("{user.id} paid {amount}{missing}", {"user": {"id": "u1"}, "amount": 3}) == "u1 paid 3"
