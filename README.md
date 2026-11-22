# watchtower-observability (Python)

[![CI](https://github.com/watchtowerobservability/watchtower-python/actions/workflows/ci.yml/badge.svg)](https://github.com/watchtowerobservability/watchtower-python/actions/workflows/ci.yml)

## Install

```sh
pip install https://github.com/watchtowerobservability/watchtower-python/releases/download/v0.1.2/watchtower_sdk-0.1.2-py3-none-any.whl
```

Thin distribution over the OpenTelemetry Python SDK: `Authorization: Bearer` header, endpoint default
(`WT_OTLP_ENDPOINT`), disk offline buffer, `wt.*`-safe resource attributes, a redaction hook, and
`get_prompt`, `score`, `evaluate`, `@observe`. The pytest plugin (`--wt-trace`) traces tests and scores pass/fail.

```python
import os, watchtower
watchtower.init(os.environ["WT_INGEST_KEY"], project=os.environ["WT_PROJECT_ID"], buffer_dir=".wt-buffer")
```

## Logs and metrics

`init` also starts the OpenTelemetry logs and metrics SDKs, exported over OTLP/HTTP to the same
endpoint with the same key (`/v1/logs`, `/v1/metrics`). Records of the standard `logging` module are
bridged with the active trace and span ids. Meters come from `opentelemetry.metrics.get_meter(...)`
(the client sets the global meter provider); counters and histograms use delta temporality unless
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` is set. Install `watchtower-observability[runtime]` for
GC, memory, thread and CPU metrics. With `buffer_dir`, failed log and metric batches are spooled to
`<buffer_dir>/logs` and `<buffer_dir>/metrics` (500 batches each, oldest dropped) and replayed first.
The standard `OTEL_BLRP_*` and `OTEL_METRIC_EXPORT_INTERVAL` variables apply.

## Quick start

```python
import watchtower, logging
watchtower.init(service_name="api")            # key from WT_INGEST_KEY; hooks capture unhandled errors
logging.getLogger().addHandler(watchtower.WatchtowerHandler())
try: risky()
except Exception as e: trace_id = watchtower.capture_exception(e)
```

Errors are recorded on the current span (or a new `exception` span) and return the trace id.
`sys.excepthook` and `threading.excepthook` are chained by `init`; for asyncio call `watchtower.install_asyncio(loop)`.

## Integrations

Install an extra, e.g. `pip install "watchtower-observability[flask]"`, then call `instrument` once after `init`.

| Extra | Call | Adds |
| --- | --- | --- |
| `django` | `integrations.django.instrument()` + `WatchtowerMiddleware` | OTel server spans, `got_request_exception` and view-exception capture |
| `flask` | `integrations.flask.instrument(app)` | OTel spans, error capture |
| `fastapi` | `integrations.fastapi.instrument(app)` | OTel spans (Starlette too), error capture |
| `celery` | `integrations.celery.instrument()` | task spans, `task_failure` capture |
| `sqlalchemy` | `integrations.sqlalchemy.instrument(engine)` | query spans |
| `requests`, `httpx` | `integrations.http_clients.instrument()` | outgoing HTTP spans |
| `aws-lambda` | `@integrations.aws_lambda.wrap` | invocation span, capture, flush before freeze |
| `azure-functions` | `@integrations.azure_functions.wrap` | invocation span with cold start and trigger, capture, flush |
| `openai`, `anthropic` | `integrations.genai.instrument_openai(client)` / `instrument_anthropic(client)` | OTel GenAI spans (model, tokens, finish reasons; no prompt text) |
| `langchain` | `callbacks=[genai.WatchtowerCallbackHandler()]` | GenAI spans per LLM call |
| `mistralai`, `ollama` | `genai.instrument_mistral(client)` / `instrument_ollama(client_or_None)` | GenAI spans, streams included (time to first chunk, tokens, tool call names) |
| `llama-index`, `openai-agents` | `genai.instrument_llamaindex()` / `instrument_openai_agents()` | agent, model and tool spans nested as the framework runs them |

### Embedded and in-memory datastores (no extra; pass the object you already have)

Spans follow OTel db semconv (`db.system.name`, `db.operation.name`, `db.namespace`, `db.collection.name`).
SQL spans carry `db.query.hash` (decimal XXH3 of the normalized text, docs/product/instrumentation/sql-normalization.md) and `db.query.summary`,
never the raw statement; `with_text=True` adds it with every literal replaced by `?`. `observe_*` reports
`wt.embedded.*` health metrics (file/WAL size, cache hits/misses, map use, compaction debt, ...).

| Store | Spans | Metrics |
| --- | --- | --- |
| sqlite3 (also pysqlite3, sqlcipher3) | `datastores.instrument_sqlite3(conn)` | `observe_sqlite(path)` |
| aiosqlite | `instrument_aiosqlite(conn, path)` | `observe_sqlite(path)` |
| apsw | `instrument_apsw(conn)` | `observe_apsw(conn, apsw)`: page-cache hits/misses, memory |
| duckdb | `instrument_duckdb(conn, path)` | `observe_duckdb(conn, path)`: blocks, buffer memory, WAL |
| lmdb | `kvstores.instrument_lmdb(env)`: span per transaction | `observe_lmdb(env)`: map size/use, readers, entries |
| plyvel (LevelDB) | `instrument_plyvel(db, path)` | `observe_plyvel(db, path)` |
| rocksdict (RocksDB) | `instrument_rocksdict(db, path)` | `observe_rocksdict(db, path)`: keys, block cache, memtables, pending compaction |
| tinydb | `instrument_tinydb(db, path)` | `observe_tinydb(db, path)` |
| diskcache | - | `observe_diskcache(cache)` |
| cachetools | - | `instrument_cachetools(cache, name)`: hits, misses, evictions in place |
| functools.lru_cache | - | `observe_lru_cache(fn)` |
| anything else | `datastores.traced(fn, system, op)` / `Proxy(obj, wraps)` | `datastores.observe(system, ns, fn)` |

## App and API Protection

An in-app WAF fed by your project's ruleset (key scope `probes:read`). In `monitor` mode it
records attacks on the current span. In `block` mode, and always for denylisted IPs and users, it
answers 403 before the app runs.

```python
from watchtower import appsec
appsec.start()  # WT_API_URL, WT_INGEST_KEY, OTEL_SERVICE_NAME
app.wsgi_app = appsec.AppsecMiddleware(app.wsgi_app)      # Flask / Django
# ASGI (FastAPI, Starlette): app = appsec.AppsecASGIMiddleware(app)
# in the login view: appsec.track_login(email, ok)
```

See `docs/product/security/app-api-protection.md`.

## Configuration

| Name | Meaning | Default |
| --- | --- | --- |
| `WT_INGEST_KEY` | secret ingest key (`Authorization: Bearer`) | required |
| `WT_OTLP_ENDPOINT` | OTLP HTTP endpoint | `http://localhost:4318` |
| `WT_SERVICE` | `service.name` | `app` |
| `WT_ENVIRONMENT` | `deployment.environment.name` | unset |
| `WT_RELEASE` | `service.version` | unset |
| `WATCHTOWER_LOGS` | `on`/`off`: bridge `logging` to OTLP logs (`OTEL_LOGS_EXPORTER=none` also turns it off) | `on` |
| `WATCHTOWER_METRICS` | `on`/`off`: OTLP metrics (`OTEL_METRICS_EXPORTER=none` also turns it off) | `on` |

## Profiling

```python
from watchtower import profiling
profiling.start(service_name="api")   # key and endpoint from WT_INGEST_KEY / WT_OTLP_ENDPOINT
```

A pure-Python wall-clock sampler (100 Hz, every thread, no native agent) posts folded stacks to
the gateway's `/ingest` every 15 s as `<service>.wall`. Inside `with profiling.span():` samples
carry the current OpenTelemetry span, so a trace links to its exact profile. The label follows the
function that entered the block, so with asyncio it stays with its task across `await` and other
tasks stay unlabeled; tasks the block starts are not labeled. For CPU
time with native frames run py-spy against `/ingest` instead.

## Provenance

Every release artifact carries a GitHub build attestation. Verify one with
`gh attestation verify <file> --repo watchtowerobservability/watchtower-python`.

A runnable example is in `examples/app.py`.

## License

Apache-2.0. Source of truth is the Watchtower monorepo; this repository is a published mirror.
