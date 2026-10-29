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
        return self.get_response(request)

    def process_exception(self, request, exception):
        route = getattr(getattr(request, "resolver_match", None), "route", "") or ""
        capture_exception(exception, **{"http.route": route})
