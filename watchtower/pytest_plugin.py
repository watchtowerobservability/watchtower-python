"""pytest plugin: each test becomes a span; outcome is a `test.outcome` attribute and, with a project, a score.

Enable with `--wt-trace` or WT_PYTEST=1 after `watchtower.init(...)` (e.g. in conftest.py).
"""
import os

import pytest

from . import client


def pytest_addoption(parser):
    parser.addoption("--wt-trace", action="store_true", default=False, help="trace tests to Watchtower")


def _on(config):
    return config.getoption("--wt-trace") or os.environ.get("WT_PYTEST") == "1"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    c = client._client  # noqa: SLF001
    if not _on(item.config) or c is None:
        yield
        return
    with c.tracer.start_as_current_span(f"test {item.nodeid}") as span:
        item._wt_span = span  # noqa: SLF001
        yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    span = getattr(item, "_wt_span", None)
    rep = out.get_result()
    if span is not None and rep.when == "call":
        span.set_attribute("test.outcome", rep.outcome)
        c = client._client  # noqa: SLF001
        if c and c.project:
            tid = format(span.get_span_context().trace_id, "032x")
            try:
                c.score(tid, 1 if rep.passed else 0, name="test_pass", comment=item.nodeid)
            except Exception:  # noqa: BLE001 - telemetry down must not fail tests
                pass
