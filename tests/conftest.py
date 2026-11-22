import os

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult

from watchtower import client as wt

# Tests opt in to logs and metrics (tests/test_signals.py); elsewhere they would bridge every
# record and start a reader thread per client.
os.environ.setdefault("WATCHTOWER_LOGS", "off")
os.environ.setdefault("WATCHTOWER_METRICS", "off")


class Capture:
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans += list(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


@pytest.fixture
def cap():
    """An initialised client whose spans land in `cap.spans` as soon as they end."""
    c = Capture()
    client = wt.init("test-key-not-real", exporter=Capture())  # batch path goes nowhere; Simple path asserts
    client.provider.add_span_processor(SimpleSpanProcessor(c))
    yield c
    wt.shutdown()
    wt._client = None
