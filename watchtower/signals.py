"""Logs and metrics: the official OTel logs and metrics SDKs, exported over OTLP/HTTP like traces.

`logging` records are bridged by the SDK's `LoggingHandler` (trace and span ids come from the
active span). Metrics use a periodic reader with delta temporality unless
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` says otherwise; runtime metrics (GC, memory,
threads, CPU) come from `opentelemetry-instrumentation-system-metrics` when it is installed."""
import logging
import os

# process.runtime.* (older releases) and process.* / cpython.* names; unknown keys are ignored.
RUNTIME = {
    "process.runtime.memory": ["rss", "vms"],
    "process.runtime.cpu.time": ["user", "system"],
    "process.runtime.gc_count": None,
    "process.runtime.thread_count": None,
    "process.runtime.cpu.utilization": None,
    "process.runtime.context_switches": ["involuntary", "voluntary"],
    "process.memory.usage": None,
    "process.memory.virtual": None,
    "process.cpu.time": ["user", "system"],
    "process.thread.count": None,
    "cpython.gc.collections": None,
}


def enabled(flag, env, otel_env):
    """Explicit argument, else `WATCHTOWER_*` (default on); `OTEL_*_EXPORTER=none` also turns it off."""
    if flag is not None:
        return bool(flag)
    if os.environ.get(otel_env, "").strip().lower() == "none":
        return False
    return os.environ.get(env, "on").strip().lower() not in ("off", "false", "0", "no")


def _spooled(exporter, buffer_dir, url, headers, sub, encode):
    if not buffer_dir:
        return exporter
    from .buffer import SpoolExporter

    return SpoolExporter(exporter, os.path.join(buffer_dir, sub), url, headers, encode=encode)


class _NotOtel(logging.Filter):
    def filter(self, record):
        # The exporters log their own failures; bridging those would feed back into the export.
        return not record.name.startswith("opentelemetry")


class _CodeLocation(logging.Filter):
    """Stamp code.filepath/code.lineno/code.function (record.pathname/lineno/funcName, free of cost) as
    attributes: LoggingHandler copies every non-reserved record attribute, so this holds whether or
    not the pinned OTel version stamps them itself."""
    def filter(self, record):
        record.__dict__.setdefault("code.filepath", record.pathname)
        record.__dict__.setdefault("code.lineno", record.lineno)
        record.__dict__.setdefault("code.function", record.funcName)
        return True


def start_logs(endpoint, headers, res, buffer_dir):
    """LoggerProvider + batch OTLP exporter, bridged from the root `logging` logger."""
    from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    url = f"{endpoint}/v1/logs"
    exp = _spooled(OTLPLogExporter(endpoint=url, headers=headers), buffer_dir, url, headers, "logs",
                   lambda batch: encode_logs(batch).SerializeToString())
    provider = LoggerProvider(resource=res)
    provider.add_log_record_processor(BatchLogRecordProcessor(exp))
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    handler.addFilter(_NotOtel())
    handler.addFilter(_CodeLocation())
    logging.getLogger().addHandler(handler)
    return provider, handler


def start_metrics(endpoint, headers, res, buffer_dir):
    """MeterProvider with a periodic OTLP reader; also set as the global meter provider."""
    from opentelemetry import metrics as api
    from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider, ObservableCounter
    from opentelemetry.sdk.metrics.export import AggregationTemporality, PeriodicExportingMetricReader

    pref = None
    if not os.environ.get("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"):
        d = AggregationTemporality.DELTA  # the OTel "delta" preference; up-down counters stay cumulative
        pref = {Counter: d, Histogram: d, ObservableCounter: d}
    url = f"{endpoint}/v1/metrics"
    exp = _spooled(OTLPMetricExporter(endpoint=url, headers=headers, preferred_temporality=pref),
                   buffer_dir, url, headers, "metrics", lambda data: encode_metrics(data).SerializeToString())
    provider = MeterProvider(resource=res, metric_readers=[PeriodicExportingMetricReader(exp)])
    api.set_meter_provider(provider)
    try:
        from opentelemetry.instrumentation.system_metrics import SystemMetricsInstrumentor
    except ImportError:  # the `runtime` extra is not installed
        return provider
    SystemMetricsInstrumentor(config=RUNTIME).instrument(meter_provider=provider)
    return provider
