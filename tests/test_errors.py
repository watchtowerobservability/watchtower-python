import logging
import sys
import threading

import watchtower
from watchtower import client, errors


def test_capture_exception_new_span_and_dedupe(cap):
    try:
        raise ValueError("boom")
    except ValueError as e:
        tid = watchtower.capture_exception(e, **{"wt.tenant": "evil", "user": "u1"})
        assert watchtower.capture_exception(e) is None
    s = cap.spans[0]
    assert tid == format(s.context.trace_id, "032x")
    assert s.events[0].name == "exception" and s.status.status_code.name == "ERROR"
    assert s.attributes["user"] == "u1" and "wt.tenant" not in s.attributes


def test_capture_on_current_span(cap):
    with client._need().tracer.start_as_current_span("op") as sp:
        tid = watchtower.capture_exception(RuntimeError("x"))
        assert tid == format(sp.get_span_context().trace_id, "032x")
    assert [s.name for s in cap.spans] == ["op"]


def test_hooks_installed_and_removed(cap):
    assert sys.excepthook is errors._excepthook
    seen, orig = [], errors._prev["sys"]
    errors._prev["sys"] = lambda *a: seen.append(a)
    sys.excepthook(ValueError, ValueError("hook"), None)
    assert seen and cap.spans[0].events[0].attributes["exception.message"] == "hook"
    errors._prev["sys"] = orig
    watchtower.shutdown()
    assert sys.excepthook is orig


def test_thread_hook(cap):
    orig = errors._prev["thread"]
    errors._prev["thread"] = lambda a: None  # silence the default traceback print
    t = threading.Thread(target=lambda: 1 / 0)
    t.start()
    t.join()
    errors._prev["thread"] = orig
    assert cap.spans[0].events[0].attributes["exception.type"] == "ZeroDivisionError"


def test_asyncio_handler(cap):
    import asyncio

    loop = asyncio.new_event_loop()
    seen = []
    loop.set_exception_handler(lambda lp, ctx: seen.append(ctx))
    errors.install_asyncio(loop)
    loop.call_exception_handler({"message": "m", "exception": KeyError("k")})
    loop.close()
    assert seen and cap.spans[0].events[0].attributes["exception.type"] == "KeyError"


def test_logging_handler(cap):
    log = logging.getLogger("wt-test")
    log.setLevel(logging.INFO)
    h = watchtower.WatchtowerHandler()
    log.addHandler(h)
    try:
        with client._need().tracer.start_as_current_span("req"):
            log.info("hello %s", "there")
            try:
                1 / 0
            except ZeroDivisionError:
                log.exception("failed")
    finally:
        log.removeHandler(h)
    ev = cap.spans[0].events
    assert [e.name for e in ev] == ["log", "exception", "log"] or {e.name for e in ev} == {"log", "exception"}
    assert ev[0].attributes["log.message"] == "hello there"


def test_exception_locals_opt_in_masks_secrets(cap):
    import json

    def boom(password, count):
        total = count * 2
        raise ValueError("bad " + str(total))

    errors.enable_locals(True)
    try:
        try:
            boom("hunter2", 4)
        except ValueError as e:
            watchtower.capture_exception(e)
    finally:
        errors.enable_locals(False)
    frames = json.loads(cap.spans[0].events[0].attributes["exception.locals"])
    top = frames[0]
    assert top["function"] == "boom"
    assert top["vars"]["password"] == "[Filtered]" and top["vars"]["count"] == "4" and top["vars"]["total"] == "8"
    assert "hunter2" not in json.dumps(frames)


def test_exception_locals_off_by_default(cap):
    try:
        raise ValueError("x")
    except ValueError as e:
        watchtower.capture_exception(e)
    assert "exception.locals" not in cap.spans[0].events[0].attributes
