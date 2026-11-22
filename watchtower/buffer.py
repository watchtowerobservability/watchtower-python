"""Offline buffer: failed OTLP payloads are spooled to disk and replayed on the next export."""
import os
import urllib.request
import uuid


class SpoolExporter:
    """Wraps an OTLP span, log or metric exporter; on failure the encoded batch goes to `directory`.

    Other attributes (the metric reader reads the exporter's temporality preferences) pass through."""

    def __init__(self, inner, directory, url, headers, encode=None, max_files=500):
        self.inner, self.dir, self.url, self.headers = inner, directory, url, headers
        self.max_files = max_files
        if encode is None:
            from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

            def encode(spans):
                return encode_spans(spans).SerializeToString()

        self.encode = encode
        os.makedirs(directory, exist_ok=True)

    def __getattr__(self, name):
        if name == "inner":  # not set yet; avoid recursing
            raise AttributeError(name)
        return getattr(self.inner, name)

    def export(self, batch, *a, **kw):
        self.replay()
        try:
            result = self.inner.export(batch, *a, **kw)
        except Exception:
            self._spool(self.encode(batch))  # some exporter versions raise on connection errors
            raise
        if getattr(result, "name", "SUCCESS") != "SUCCESS":
            self._spool(self.encode(batch))
        return result

    def _files(self):
        """Batches oldest first; subdirectories (another signal's spool) are skipped."""
        names = [n for n in os.listdir(self.dir) if os.path.isfile(os.path.join(self.dir, n))]
        return sorted(names, key=lambda n: os.path.getmtime(os.path.join(self.dir, n)))

    def _spool(self, payload):
        files = self._files()
        for old in files[: max(0, len(files) - self.max_files + 1)]:  # drop-oldest
            os.remove(os.path.join(self.dir, old))
        with open(os.path.join(self.dir, f"{uuid.uuid4().hex}.pb"), "wb") as f:
            f.write(payload)

    def replay(self):
        for name in self._files():
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

    def shutdown(self, *a, **kw):
        self.inner.shutdown(*a, **kw)

    def force_flush(self, *a, **kw):
        return self.inner.force_flush(*a, **kw)
