"""FastAPI/Starlette: `instrument(app)` adds OTel server spans and error capture."""
from ..errors import capture_exception


class CaptureMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        try:
            await self.app(scope, receive, send)
        except Exception as e:
            capture_exception(e)
            raise


def instrument(app):
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    app.add_middleware(CaptureMiddleware)
