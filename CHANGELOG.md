# Changelog

## 0.1.0

- Initial release: OTel-based init, offline buffer, redaction, prompts/scores/evaluate, pytest plugin.
- Error capture (`capture_exception`, `sys.excepthook`, threading and asyncio hooks) with trace ids.
- `logging` handler (`WatchtowerHandler`).
- Integrations (extras): django, flask, fastapi, celery, sqlalchemy, requests, httpx, aws-lambda,
  openai, anthropic, langchain (OTel GenAI semconv).
- `WT_SERVICE`, `WT_ENVIRONMENT`, `WT_RELEASE`, `WT_INGEST_KEY` read from the environment.
