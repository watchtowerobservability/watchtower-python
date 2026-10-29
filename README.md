# watchtower-sdk (Python)

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

Profiling: run a Pyroscope-compatible agent against the Watchtower profiles endpoint; no wrapper is shipped here.

## Provenance

Every release artifact carries a GitHub build attestation. Verify one with
`gh attestation verify <file> --repo watchtowerobservability/watchtower-python`.

## License

Apache-2.0. Source of truth is the Watchtower monorepo; this repository is a published mirror.
