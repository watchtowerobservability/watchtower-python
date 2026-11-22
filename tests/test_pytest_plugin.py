pytest_plugins = ["pytester"]


def test_plugin_marks_outcome(pytester):
    pytester.makeconftest(
        "from opentelemetry.sdk.trace.export import SpanExportResult\n"
        "import watchtower\n"
        "class C:\n"
        "    spans = []\n"
        "    def export(self, s):\n"
        "        C.spans += list(s)\n"
        "        return SpanExportResult.SUCCESS\n"
        "    def shutdown(self): pass\n"
        "    def force_flush(self, t=0): return True\n"
        "watchtower.init('test-key-not-real', exporter=C())\n"
        "def pytest_sessionfinish(session):\n"
        "    watchtower.shutdown()\n"
        "    assert any(s.attributes.get('test.outcome') == 'passed' for s in C.spans)\n"
    )
    pytester.makepyfile("def test_a():\n    assert True\n")
    r = pytester.runpytest_subprocess("--wt-trace")
    r.assert_outcomes(passed=1)


_CONFTEST = (
    "import json\n"
    "from opentelemetry.sdk.trace.export import SpanExportResult\n"
    "import watchtower\n"
    "from watchtower import pytest_plugin as pp\n"
    "class C:\n"
    "    def export(self, spans):\n"
    "        with open('spans.jsonl', 'a') as f:\n"
    "            for s in spans:\n"
    "                f.write(json.dumps(dict(s.attributes)) + '\\n')\n"
    "        return SpanExportResult.SUCCESS\n"
    "    def shutdown(self): pass\n"
    "    def force_flush(self, t=0): return True\n"
    "def fake(c, method, path, body=None):\n"
    "    with open('calls.jsonl', 'a') as f:\n"
    "        f.write(json.dumps([method, path, body]) + '\\n')\n"
    "    if path == 'tests/plan':\n"
    "        return {'quarantined': [{'id': 'pytest::test_x.test_flaky', 'ticket_url': 'https://jira/T-1'}],\n"
    "                'disabled': ['pytest::test_x.test_off'], 'skippable': ['pytest::test_x.test_far'],\n"
    "                'new_tests': ['pytest::test_x.test_new'], 'efd_retries': 2}\n"
    "    return {'stored': len(body['tests'])}\n"
    "pp._api = fake\n"
    "c = watchtower.init('test-key-not-real', exporter=C(), project='prj_1', api_url='http://api.invalid')\n"
    "c.score = lambda *a, **k: None\n"
    "def pytest_sessionfinish(session):\n"
    "    watchtower.shutdown()\n"
)


def _lines(path):
    import json

    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_plan_quarantines_skips_and_retries_new_tests(pytester, monkeypatch):
    monkeypatch.setenv("WT_CHANGED_FILES", "src/a.py,src/b.py")
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(test_x=(
        "def test_flaky():\n    assert False\n"
        "def test_off():\n    assert False\n"
        "def test_far():\n    assert False\n"
        "def test_new():\n    open('runs.txt', 'a').write('x')\n"
    ))
    r = pytester.runpytest_subprocess("--wt-trace", "--wt-test-optimization", "--wt-tia-record")
    r.assert_outcomes(passed=1, skipped=2, xfailed=1)
    assert (pytester.path / "runs.txt").read_text() == "xxx", "a new test runs 1 + efd_retries times"
    calls = _lines(pytester.path / "calls.jsonl")
    plan = next(c for c in calls if c[1] == "tests/plan")
    assert plan[2]["changed_files"] == ["src/a.py", "src/b.py"]
    assert "pytest::test_x.test_new" in plan[2]["tests"]
    cov = next(c for c in calls if c[1] == "tests/coverage-map")
    by_test = {t["test"]: t["files"] for t in cov[2]["tests"]}
    assert "test_x.py" in by_test["test_x.test_new"]
    spans = _lines(pytester.path / "spans.jsonl")
    new = next(s for s in spans if s.get("test.case.name") == "pytest::test_x.test_new")
    assert new["test.is_new"] is True and new["test.is_flaky"] is False
    flaky = next(s for s in spans if s.get("test.case.name") == "pytest::test_x.test_flaky")
    assert flaky["test.quarantined"] is True


def test_failure_carries_redacted_locals(pytester):
    pytester.makeconftest(_CONFTEST)
    pytester.makepyfile(test_y=(
        "def test_math():\n"
        "    api_token = 'hunter2'\n"
        "    value = 41\n"
        "    assert value == 42\n"
    ))
    r = pytester.runpytest_subprocess("--wt-trace")
    r.assert_outcomes(failed=1)
    spans = _lines(pytester.path / "spans.jsonl")
    import json

    span = next(s for s in spans if "test.failure.locals" in s)
    loc = json.loads(span["test.failure.locals"])
    assert loc["value"] == "41"
    assert loc["api_token"] == "[redacted]"
    assert span["test.case.result.status"] == "fail"


def test_ids_and_locals_helpers():
    from types import SimpleNamespace

    from watchtower.pytest_plugin import safe_locals, test_id

    item = SimpleNamespace(nodeid="tests/test_a.py::TestC::test_b[1]", name="test_b[1]")
    assert test_id(item, "pytest") == "pytest::tests.test_a.TestC.test_b[1]"
    out = safe_locals({"password": "x", "long": "y" * 500, "n": 3})
    assert out["password"] == "[redacted]" and out["n"] == "3" and len(out["long"]) == 203
