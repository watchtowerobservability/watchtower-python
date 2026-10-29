from celery.signals import task_failure

from ..errors import capture_exception


def _on_failure(sender=None, exception=None, **kw):
    capture_exception(exception, **{"celery.task": getattr(sender, "name", "")})


def instrument():
    from opentelemetry.instrumentation.celery import CeleryInstrumentor

    CeleryInstrumentor().instrument()
    task_failure.connect(_on_failure, weak=False, dispatch_uid="watchtower")
