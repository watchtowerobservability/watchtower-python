"""pytest plugin: each test becomes a span with the OTel `test.*` attributes; with a project, a score.

Enable with `--wt-trace` or WT_PYTEST=1 after `watchtower.init(...)` (e.g. in conftest.py).

Test optimization (`--wt-test-optimization` or WT_TEST_OPTIMIZATION=1, needs `api_url` and `project`)
asks `POST /v1/projects/{prj}/tests/plan` once per session, then:

- disabled tests are skipped; quarantined tests run but a failure is reported as xfail (ticket in the
  reason), so it does not fail the session;
- Test Impact Analysis skips the tests the change cannot reach: the changed files come from
  `--wt-git-base <rev>` (`git diff --name-only <rev>...HEAD`) or WT_CHANGED_FILES (comma-separated);
- early flake detection retries each test the project has never seen `efd_retries` times and marks
  the span `test.is_flaky` when the outcomes differ;
- `--wt-tia-record` records the files each test executed and uploads them as the coverage map.

A failing test's span carries `test.failure.locals`: the locals of the innermost frame in the test
file (repr, cut at 200 characters, secret-looking names redacted), so the failure can be replayed.
Test ids are `suite::classname.name` as in JUnit XML (suite: WT_TEST_SUITE, else `junit_suite_name`,
else `pytest`), the ids `wtctl ci upload-junit` stores.
"""
import json
import os
import re
import subprocess
import sys
import urllib.request

import pytest

from . import client

_SECRET = re.compile(r"pass|secret|token|key|auth|cred", re.I)
_MAX_LOCALS = 30


def pytest_addoption(parser):
    g = parser.getgroup("watchtower")
    g.addoption("--wt-trace", action="store_true", default=False, help="trace tests to Watchtower")
    g.addoption("--wt-test-optimization", action="store_true", default=False,
                help="quarantine, disabled tests, Test Impact Analysis and early flake detection")
    g.addoption("--wt-git-base", default=None, help="git revision the change is diffed against (TIA)")
    g.addoption("--wt-tia-record", action="store_true", default=False,
                help="record the files each test executes and upload the coverage map")


def _on(config):
    return config.getoption("--wt-trace") or os.environ.get("WT_PYTEST") == "1"


def _opt_on(config):
    return config.getoption("--wt-test-optimization") or os.environ.get("WT_TEST_OPTIMIZATION") == "1"


def _api(c, method, path, body=None):
    """One project API call; tests replace it."""
    url = f"{c.api_url}/v1/projects/{c.project}/{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**c.headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - the configured API URL
        return json.load(r)


def _suite(config):
    s = os.environ.get("WT_TEST_SUITE")
    if s:
        return s
    try:
        return config.getini("junit_suite_name") or "pytest"
    except ValueError:  # junitxml plugin not loaded
        return "pytest"


def test_id(item, suite):
    """`suite::classname.name` the way pytest's JUnit XML names the case."""
    parts = item.nodeid.split("::")
    mod = parts[0]
    if mod.endswith(".py"):
        mod = mod[:-3]
    cls = [mod.replace("/", ".").replace("\\", ".")] + parts[1:-1]
    return f"{suite}::{'.'.join(cls)}.{item.name}"


def _changed_files(config):
    env = os.environ.get("WT_CHANGED_FILES")
    if env:
        return [f.strip() for f in env.split(",") if f.strip()]
    base = config.getoption("--wt-git-base")
    if not base or base.startswith("-"):
        return []
    try:
        out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD", "--"],  # noqa: S603,S607
                             capture_output=True, text=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return out.split()


def safe_locals(frame_locals):
    """Locals for `test.failure.locals`: reprs cut at 200 characters, secret-looking names redacted."""
    out = {}
    for k, v in list(frame_locals.items())[:_MAX_LOCALS]:
        if k.startswith("@"):
            continue
        if _SECRET.search(k):
            out[k] = "[redacted]"
            continue
        try:
            r = repr(v)
        except Exception:  # noqa: BLE001 - a broken __repr__ must not hide the failure
            r = "<unrepresentable>"
        out[k] = r if len(r) <= 200 else r[:200] + "..."
    return out


def _failure_locals(item, excinfo):
    """Locals of the innermost traceback frame in the test's own file, else the innermost frame."""
    tb, pick = excinfo.tb, None
    path = str(getattr(item, "path", "") or getattr(item, "fspath", ""))
    while tb is not None:
        if pick is None or tb.tb_frame.f_code.co_filename == path:
            pick = tb.tb_frame
        tb = tb.tb_next
    return safe_locals(pick.f_locals) if pick is not None else {}


class _State:
    def __init__(self):
        self.plan = None
        self.suite = "pytest"
        self.coverage = {}


_state = _State()


def pytest_collection_modifyitems(session, config, items):
    _state.suite = _suite(config)
    c = client._client  # noqa: SLF001
    if not _opt_on(config) or c is None or not c.project:
        return
    ids = [test_id(i, _state.suite) for i in items]
    try:
        _state.plan = _api(c, "POST", "tests/plan", {"changed_files": _changed_files(config), "tests": ids})
    except Exception as e:  # noqa: BLE001 - telemetry down must not fail tests
        config.issue_config_time_warning(pytest.PytestWarning(f"watchtower test plan unavailable: {e}"), 2)
        return
    p = _state.plan
    disabled, skippable = set(p.get("disabled") or []), set(p.get("skippable") or [])
    quarantined = {q["id"]: q for q in p.get("quarantined") or []}
    new = set(p.get("new_tests") or [])
    for item, tid in zip(items, ids):
        item._wt_id = tid  # noqa: SLF001
        if tid in disabled:
            item.add_marker(pytest.mark.skip(reason="disabled in Watchtower"))
        elif tid in skippable and tid not in quarantined:
            item.add_marker(pytest.mark.skip(reason="Watchtower TIA: not affected by the change"))
        if tid in quarantined:
            item._wt_quarantine = quarantined[tid].get("ticket_url") or "no ticket"  # noqa: SLF001
        if tid in new:
            item._wt_new = True  # noqa: SLF001


def _tracer_files(root, files):
    def prof(frame, event, _arg):
        if event == "call":
            f = frame.f_code.co_filename
            if f.startswith(root) and "site-packages" not in f:
                files.add(os.path.relpath(f, root).replace("\\", "/"))
    return prof


class _Efd:
    """Early flake detection: a new test runs 1 + efd_retries times; the first run is reported."""

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(self, item, nextitem):
        retries = int((_state.plan or {}).get("efd_retries") or 0)
        if not getattr(item, "_wt_new", False) or retries <= 0:
            return None
        from _pytest.runner import runtestprotocol

        item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
        first = runtestprotocol(item, nextitem=nextitem, log=False)
        outcomes = {_outcome(first)}
        for _ in range(retries):
            outcomes.add(_outcome(runtestprotocol(item, nextitem=nextitem, log=False)))
        for rep in first:
            item.ihook.pytest_runtest_logreport(report=rep)
        item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
        item._wt_efd = (retries, len(outcomes) > 1)  # noqa: SLF001
        return True


def _outcome(reports):
    return "failed" if any(r.failed for r in reports) else "passed"


def pytest_configure(config):
    if not config.pluginmanager.has_plugin("watchtower-efd"):
        config.pluginmanager.register(_Efd(), "watchtower-efd")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """The test's span (current, so code under test nests in it) and the TIA file recorder."""
    prev, files = None, None
    if item.config.getoption("--wt-tia-record"):
        files = _state.coverage.setdefault(getattr(item, "_wt_id", None) or test_id(item, _state.suite), set())
        prev = sys.getprofile()
        sys.setprofile(_tracer_files(str(item.config.rootpath), files))
    c = client._client  # noqa: SLF001
    try:
        if not _on(item.config) or c is None:
            yield
            return
        with c.tracer.start_as_current_span(f"test {item.nodeid}") as span:
            item._wt_span = span  # noqa: SLF001
            span.set_attribute("test.framework", "pytest")
            span.set_attribute("test.suite.name", _state.suite)
            span.set_attribute("test.case.name", getattr(item, "_wt_id", None) or test_id(item, _state.suite))
            yield
            efd = getattr(item, "_wt_efd", None)
            if efd:
                span.set_attribute("test.is_new", True)
                span.set_attribute("test.efd.retries", efd[0])
                span.set_attribute("test.is_flaky", efd[1])
    finally:
        if files is not None:
            sys.setprofile(prev)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    ticket = getattr(item, "_wt_quarantine", None)
    if ticket and rep.when == "call" and rep.failed:
        rep.outcome = "skipped"
        rep.wasxfail = f"quarantined in Watchtower ({ticket})"
    span = getattr(item, "_wt_span", None)
    if span is None or rep.when != "call" or getattr(item, "_wt_reported", False):
        return
    item._wt_reported = True  # noqa: SLF001 - an EFD retry does not overwrite the first result
    span.set_attribute("test.outcome", rep.outcome)
    span.set_attribute("test.case.result.status", "pass" if rep.passed else "fail")
    if ticket:
        span.set_attribute("test.quarantined", True)
    if call.excinfo is not None and not rep.passed:
        span.record_exception(call.excinfo.value)
        span.set_attribute("test.failure.locals", json.dumps(_failure_locals(item, call.excinfo)))
    c = client._client  # noqa: SLF001
    if c and c.project:
        tid = format(span.get_span_context().trace_id, "032x")
        try:
            c.score(tid, 1 if rep.passed else 0, name="test_pass", comment=item.nodeid)
        except Exception:  # noqa: BLE001 - telemetry down must not fail tests
            pass


def pytest_sessionfinish(session, exitstatus):
    c = client._client  # noqa: SLF001
    if not _state.coverage or c is None or not c.project:
        return
    sha = os.environ.get("WT_SHA") or os.environ.get("GITHUB_SHA") or os.environ.get("CI_COMMIT_SHA") or ""
    tests = []
    for tid, files in _state.coverage.items():
        suite, _, test = tid.partition("::")
        tests.append({"suite": suite, "test": test, "files": sorted(files)[:2000]})
    try:
        _api(c, "PUT", "tests/coverage-map", {"sha": sha[:64], "tests": tests})
    except Exception:  # noqa: BLE001 - telemetry down must not fail tests
        pass
    _state.coverage = {}
