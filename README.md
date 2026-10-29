# watchtower-sdk (Python)

[![CI](https://github.com/watchtowerobservability/watchtower-python/actions/workflows/ci.yml/badge.svg)](https://github.com/watchtowerobservability/watchtower-python/actions/workflows/ci.yml)

## Install

```sh
pip install https://github.com/watchtowerobservability/watchtower-python/releases/download/v0.1.0/watchtower_sdk-0.1.0-py3-none-any.whl
```

Thin distribution over the OpenTelemetry Python SDK: `Authorization: Bearer` header, endpoint default
(`WT_OTLP_ENDPOINT`), disk offline buffer, `wt.*`-safe resource attributes, a redaction hook, and
`get_prompt`, `score`, `evaluate`, `@observe`. The pytest plugin (`--wt-trace`) traces tests and scores pass/fail.

```python
import os, watchtower
watchtower.init(os.environ["WT_INGEST_KEY"], project=os.environ["WT_PROJECT_ID"], buffer_dir=".wt-buffer")
```

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

Install an extra, e.g. `pip install "watchtower-sdk[flask]"`, then call `instrument` once after `init`.

| Extra | Call | Adds |
| --- | --- | --- |
| `django` | `integrations.django.instrument()` + `WatchtowerMiddleware` | OTel server spans, `got_request_exception` and view-exception capture |
| `flask` | `integrations.flask.instrument(app)` | OTel spans, error capture |
| `fastapi` | `integrations.fastapi.instrument(app)` | OTel spans (Starlette too), error capture |
| `celery` | `integrations.celery.instrument()` | task spans, `task_failure` capture |
| `sqlalchemy` | `integrations.sqlalchemy.instrument(engine)` | query spans |
| `requests`, `httpx` | `integrations.http_clients.instrument()` | outgoing HTTP spans |
| `aws-lambda` | `@integrations.aws_lambda.wrap` | invocation span, capture, flush before freeze |
| `openai`, `anthropic` | `integrations.genai.instrument_openai(client)` / `instrument_anthropic(client)` | OTel GenAI spans (model, tokens, finish reasons; no prompt text) |
| `langchain` | `callbacks=[genai.WatchtowerCallbackHandler()]` | GenAI spans per LLM call |

## Configuration

| Name | Meaning | Default |
| --- | --- | --- |
| `WT_INGEST_KEY` | secret ingest key (`Authorization: Bearer`) | required |
| `WT_OTLP_ENDPOINT` | OTLP HTTP endpoint | `http://localhost:4318` |
| `WT_SERVICE` | `service.name` | `app` |
| `WT_ENVIRONMENT` | `deployment.environment.name` | unset |
| `WT_RELEASE` | `service.version` | unset |

Profiling: run a Pyroscope-compatible agent against the Watchtower profiles endpoint; no wrapper is shipped here.

## Provenance

Every release artifact carries a GitHub build attestation. Verify one with
`gh attestation verify <file> --repo watchtowerobservability/watchtower-python`.

A runnable example is in `examples/app.py`.

## License

Apache-2.0. Source of truth is the Watchtower monorepo; this repository is a published mirror.
