"""FastAPI/Starlette: `instrument(app)` adds OTel server spans, error capture and payload capture."""
from opentelemetry import trace

from .. import capture
from ..errors import capture_exception


class _Tee:
    """Copies at most HARD_MAX * 2 bytes of the body chunks the app itself reads and sends; nothing extra is buffered."""

    def __init__(self, receive, send):
        self.req, self.res, self.res_type = bytearray(), bytearray(), None
        self._receive, self._send = receive, send

    async def receive(self):
        m = await self._receive()
        if m.get("type") == "http.request":
            self.req += m.get("body", b"")[: capture.HARD_MAX * 2 - len(self.req)]
        return m

    async def send(self, m):
        if m.get("type") == "http.response.start":
            self.res_type = _header(m.get("headers"), b"content-type")
        elif m.get("type") == "http.response.body":
            self.res += m.get("body", b"")[: capture.HARD_MAX * 2 - len(self.res)]
        await self._send(m)


def _header(headers, name):
    v = next((v for k, v in headers or [] if k.lower() == name), None)
    return v.decode("latin-1") if v is not None else None


class CaptureMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        tee = _Tee(receive, send) if scope.get("type") == "http" and capture.RULES else None
        try:
            await self.app(scope, tee.receive if tee else receive, tee.send if tee else send)
        except Exception as e:
            capture_exception(e)
            raise
        finally:
            if tee:
                # the OTel middleware wraps this one, so its server span is still current; Starlette's
                # router puts the matched route on the shared scope
                route = getattr(scope.get("route"), "path", None) or scope.get("path", "")
                capture.record(trace.get_current_span(), route, _header(scope.get("headers"), b"content-type"),
                               bytes(tee.req), tee.res_type, bytes(tee.res))


def instrument(app):
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    app.add_middleware(CaptureMiddleware)
