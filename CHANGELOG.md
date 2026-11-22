# Changelog

## 0.1.2

- Distribution renamed to `watchtower-observability` (`watchtower-sdk` on PyPI belongs to another project). Install with `pip install watchtower-observability`; the import package is still `watchtower`.
- Sync with the monorepo: log code location, in-app WAF and exception replay.

- Logs and metrics: `init` wires the OTel logs SDK (the `logging` bridge, with trace and span ids) and the metrics SDK (periodic OTLP reader, delta temporality), switched by `WATCHTOWER_LOGS` / `WATCHTOWER_METRICS` (default on) or `logs=` / `metrics=`. Runtime metrics with the `runtime` extra. The offline buffer covers logs and metrics in `<buffer_dir>/logs` and `/metrics`.
- GenAI: async clients (`AsyncOpenAI`, `AsyncAnthropic`, Mistral `*_async`, Ollama `AsyncClient`, `litellm.acompletion`, google-genai `aio`) are wrapped like the sync ones; the span covers the awaited call and async streams.

- GenAI: `instrument_mistral`, `instrument_ollama`, `instrument_llamaindex` (instrumentation dispatcher) and `instrument_openai_agents` (trace processor). Streams (`stream=True`, Mistral `chat.stream`) keep the span open to the end and record time to first chunk and usage; tool calls become `gen_ai.tool.call` events (name and id only) for every wrapper.
- GenAI: `instrument_bedrock` (boto3 `converse`/`invoke_model`), `instrument_google_genai` (`models.generate_content`) and `instrument_litellm` (`litellm.completion`).

## 0.1.0

- Initial release: OTel-based init, offline buffer, redaction, prompts/scores/evaluate, pytest plugin.
- Error capture (`capture_exception`, `sys.excepthook`, threading and asyncio hooks) with trace ids.
- `logging` handler (`WatchtowerHandler`).
- Integrations (extras): django, flask, fastapi, celery, sqlalchemy, requests, httpx, aws-lambda,
  openai, anthropic, langchain (OTel GenAI semconv).
- `WT_SERVICE`, `WT_ENVIRONMENT`, `WT_RELEASE`, `WT_INGEST_KEY` read from the environment.
