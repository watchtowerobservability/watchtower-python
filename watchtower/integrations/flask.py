from flask import got_request_exception

from ..errors import capture_exception


def instrument(app):
    from opentelemetry.instrumentation.flask import FlaskInstrumentor

    FlaskInstrumentor().instrument_app(app)
    app.after_request(_capture_payload)
    got_request_exception.connect(lambda sender, exception, **kw: capture_exception(exception), app, weak=False)


def _capture_payload(response):
    """Payload capture rules (APM-016): the server span is still current in after_request."""
    from opentelemetry import trace
    from flask import request

    from .. import capture

    if capture.RULES:
        rule = request.url_rule.rule if request.url_rule else request.path
        res = None if response.is_streamed else response.get_data()
        req = request.get_data(cache=True)[: capture.HARD_MAX * 2] if capture.text_like(request.content_type) else None
        capture.record(trace.get_current_span(), rule, request.content_type, req, response.content_type, res)
    return response
