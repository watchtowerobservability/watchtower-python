import os

from opentelemetry.sdk.trace.export import SpanExportResult

from watchtower.buffer import SpoolExporter


class Fail:
    def export(self, spans):
        return SpanExportResult.FAILURE


def test_failed_export_is_spooled_and_capped(tmp_path):
    s = SpoolExporter(Fail(), str(tmp_path), "http://127.0.0.1:1/v1/traces", {}, encode=lambda sp: b"x", max_files=2)
    for _ in range(4):
        s.export([])
    assert len(os.listdir(tmp_path)) == 2
