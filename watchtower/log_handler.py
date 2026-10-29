"""`logging` handler: records become span events; error records with exc_info are captured."""
import logging

from opentelemetry import trace

from .errors import capture_exception


class WatchtowerHandler(logging.Handler):
    def __init__(self, level=logging.INFO):
        super().__init__(level)

    def emit(self, record):
        try:
            if record.exc_info and record.exc_info[1] is not None and record.levelno >= logging.ERROR:
                capture_exception(record.exc_info[1], **{"log.logger": record.name})
            span = trace.get_current_span()
            if span.is_recording():
                span.add_event("log", {"log.severity": record.levelname, "log.message": record.getMessage(),
                                       "log.logger": record.name})
        except Exception:  # noqa: BLE001
            self.handleError(record)
