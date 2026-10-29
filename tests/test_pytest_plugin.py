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
