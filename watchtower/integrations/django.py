"""Django: add `watchtower.integrations.django.WatchtowerMiddleware` to MIDDLEWARE and call `instrument()`."""
from ..errors import capture_exception


def instrument():
    from django.core.signals import got_request_exception
    from opentelemetry.instrumentation.django import DjangoInstrumentor

    DjangoInstrumentor().instrument()
    got_request_exception.connect(_on_exception, dispatch_uid="watchtower")


def _on_exception(sender, request=None, **kw):
    capture_exception()


class WatchtowerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .. import capture

        if not capture.RULES:
            return self.get_response(request)
        # read (text bodies only, uploads stay streamed) before the view consumes the stream
        body = request.body[: capture.HARD_MAX * 2] if capture.text_like(request.content_type) else None
        response = self.get_response(request)
        from opentelemetry import trace

        route = getattr(getattr(request, "resolver_match", None), "route", "") or request.path
        res = None if getattr(response, "streaming", False) else response.content
        capture.record(trace.get_current_span(), route, request.content_type, body,
                       response.get("Content-Type"), res)
        return response

    def process_exception(self, request, exception):
        route = getattr(getattr(request, "resolver_match", None), "route", "") or ""
        capture_exception(exception, **{"http.route": route})
