from flask import got_request_exception

from ..errors import capture_exception


def instrument(app):
    from opentelemetry.instrumentation.flask import FlaskInstrumentor

    FlaskInstrumentor().instrument_app(app)
    got_request_exception.connect(lambda sender, exception, **kw: capture_exception(exception), app, weak=False)
