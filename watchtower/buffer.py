"""Offline buffer: failed OTLP payloads are spooled to disk and replayed on the next export."""
import os
import urllib.request
import uuid


class SpoolExporter:
    """Wraps an OTLP span exporter; on failure the encoded batch goes to `directory`."""

    def __init__(self, inner, directory, url, headers, encode=None, max_files=500):
        self.inner, self.dir, self.url, self.headers = inner, directory, url, headers
        self.max_files = max_files
        if encode is None:
            from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

            def encode(spans):
                return encode_spans(spans).SerializeToString()

        self.encode = encode
        os.makedirs(directory, exist_ok=True)

    def export(self, spans):
        self.replay()
        result = self.inner.export(spans)
        if getattr(result, "name", "SUCCESS") != "SUCCESS":
            self._spool(self.encode(spans))
        return result

    def _spool(self, payload):
        files = sorted(os.listdir(self.dir), key=lambda n: os.path.getmtime(os.path.join(self.dir, n)))
        for old in files[: max(0, len(files) - self.max_files + 1)]:  # drop-oldest
            os.remove(os.path.join(self.dir, old))
        with open(os.path.join(self.dir, f"{uuid.uuid4().hex}.pb"), "wb") as f:
            f.write(payload)

    def replay(self):
        names = sorted(os.listdir(self.dir), key=lambda n: os.path.getmtime(os.path.join(self.dir, n)))
        for name in names:
            path = os.path.join(self.dir, name)
            with open(path, "rb") as f:
                body = f.read()
            req = urllib.request.Request(
                self.url, data=body, method="POST",
                headers={**self.headers, "Content-Type": "application/x-protobuf"},
            )
            try:
                urllib.request.urlopen(req, timeout=5).close()
            except Exception:  # noqa: BLE001
                return  # still offline; keep the rest in order
            os.remove(path)

    def shutdown(self):
        self.inner.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.inner.force_flush(timeout_millis)
