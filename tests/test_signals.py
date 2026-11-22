"""Logs and metrics reach a fake OTLP endpoint; log records carry the active trace and span ids."""
import http.server
import logging
import os
import threading

import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.sdk.trace.export import SpanExportResult

from watchtower import client as wt
from watchtower import signals


class _Spans:
    def export(self, spans):
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


class _Otlp(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server naming
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.got.append((self.path, self.headers.get("Authorization"), body))
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def otlp(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", raising=False)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Otlp)
    srv.got = []
    srv.url = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _bodies(srv, path):
    return [b for p, _, b in srv.got if p == path]


def _records(srv):
    return [r for b in _bodies(srv, "/v1/logs") for rl in ExportLogsServiceRequest.FromString(b).resource_logs
            for sl in rl.scope_logs for r in sl.log_records]


def _metrics(srv, name):
    return [m for b in _bodies(srv, "/v1/metrics") for rm in ExportMetricsServiceRequest.FromString(b).resource_metrics
            for sm in rm.scope_metrics for m in sm.metrics if m.name == name]


def test_logging_records_exported_with_trace_correlation(otlp):
    c = wt.Client("test-key-not-real", endpoint=otlp.url, exporter=_Spans(), logs=True, metrics=False)
    log = logging.getLogger("app.signals")
    log.setLevel(logging.INFO)
    with c.tracer.start_as_current_span("op") as span:
        log.info("hello %s", "world")
        ctx = span.get_span_context()
    c.logger_provider.force_flush()
    c.shutdown()
    assert all(a == "Bearer test-key-not-real" for p, a, _ in otlp.got if p == "/v1/logs")
    rec = next(r for r in _records(otlp) if r.body.string_value == "hello world")
    assert rec.trace_id == ctx.trace_id.to_bytes(16, "big")
    assert rec.span_id == ctx.span_id.to_bytes(8, "big")
    assert rec.severity_text == "INFO"
    assert c._log_handler is None  # shutdown unhooked the root logger


def test_metrics_exported_with_delta_temporality(otlp):
    c = wt.Client("k", endpoint=otlp.url, exporter=_Spans(), logs=False, metrics=True)
    c.meter_provider.get_meter("t").create_counter("jobs.done").add(3, {"queue": "a"})
    c.meter_provider.force_flush()
    c.shutdown()
    m = _metrics(otlp, "jobs.done")[0]
    assert m.sum.aggregation_temporality == 1  # AGGREGATION_TEMPORALITY_DELTA
    assert m.sum.data_points[0].as_int == 3


def test_failed_logs_and_metrics_spooled_then_replayed(tmp_path, otlp, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "1")  # keep the exporter's retries short
    down = wt.Client("k", endpoint="http://127.0.0.1:1", buffer_dir=str(tmp_path), exporter=_Spans(),
                     logs=True, metrics=True)
    logging.getLogger("app.spool").warning("while offline")
    down.meter_provider.get_meter("t").create_counter("offline.count").add(1)
    down.logger_provider.force_flush()
    down.meter_provider.force_flush()
    down.shutdown()
    assert os.listdir(tmp_path / "logs") and os.listdir(tmp_path / "metrics")

    up = wt.Client("k", endpoint=otlp.url, buffer_dir=str(tmp_path), exporter=_Spans(), logs=True, metrics=True)
    logging.getLogger("app.spool").warning("back online")
    up.meter_provider.get_meter("t").create_counter("online.count").add(1)
    up.logger_provider.force_flush()
    up.meter_provider.force_flush()
    up.shutdown()
    bodies = {r.body.string_value for r in _records(otlp)}
    assert {"while offline", "back online"} <= bodies
    assert _metrics(otlp, "offline.count") and _metrics(otlp, "online.count")
    assert not os.listdir(tmp_path / "logs") and not os.listdir(tmp_path / "metrics")


def test_switches(monkeypatch):
    monkeypatch.setenv("WATCHTOWER_LOGS", "off")
    assert not signals.enabled(None, "WATCHTOWER_LOGS", "OTEL_LOGS_EXPORTER")
    assert signals.enabled(True, "WATCHTOWER_LOGS", "OTEL_LOGS_EXPORTER")
    monkeypatch.setenv("WATCHTOWER_METRICS", "on")
    monkeypatch.setenv("OTEL_METRICS_EXPORTER", "none")
    assert not signals.enabled(None, "WATCHTOWER_METRICS", "OTEL_METRICS_EXPORTER")
