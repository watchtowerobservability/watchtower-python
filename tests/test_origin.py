from opentelemetry.sdk.trace import TracerProvider

from watchtower.origin import CodeOrigin, first_app_frame


def test_first_app_frame_skips_library_paths():
    f = first_app_frame(__import__("sys")._getframe(0))  # noqa: SLF001
    assert f and f[0].endswith("test_origin.py") and f[2] == "test_first_app_frame_skips_library_paths"


def test_span_gets_code_attributes():
    tp = TracerProvider()
    tp.add_span_processor(CodeOrigin())
    with tp.get_tracer("t").start_as_current_span("op") as span:
        a = span.attributes
    assert a["code.filepath"].endswith("test_origin.py")
    assert a["code.function"] == "test_span_gets_code_attributes"
    assert isinstance(a["code.lineno"], int)


def test_log_records_get_code_location():
    import logging
    from watchtower.signals import _CodeLocation
    r = logging.LogRecord("app", logging.INFO, "/srv/app/cart.py", 42, "hi", None, None, func="total")
    assert _CodeLocation().filter(r)
    assert (r.__dict__["code.filepath"], r.__dict__["code.lineno"], r.__dict__["code.function"]) == ("/srv/app/cart.py", 42, "total")
