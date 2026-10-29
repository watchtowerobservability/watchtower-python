"""GenAI spans (OTel GenAI semconv) for openai, anthropic and langchain. Prompt/completion text is never recorded."""
import functools

from opentelemetry.trace import Status, StatusCode

from .. import client as _c
from ..errors import capture_exception


def _first(*vals):
    return next((v for v in vals if v is not None), None)


def _traced(system, op, fn):
    @functools.wraps(fn)
    def inner(*a, **kw):
        model = kw.get("model", "")
        with _c._need().tracer.start_as_current_span(f"{op} {model}".strip()) as span:
            span.set_attribute("gen_ai.operation.name", op)
            span.set_attribute("gen_ai.system", system)
            span.set_attribute("gen_ai.request.model", model)
            for k in ("temperature", "max_tokens", "top_p"):
                if kw.get(k) is not None:
                    span.set_attribute(f"gen_ai.request.{k}", kw[k])
            try:
                res = fn(*a, **kw)
            except Exception as e:
                capture_exception(e)
                raise
            _record_response(span, res)
            return res

    return inner


def _record_response(span, res):
    if getattr(res, "model", None):
        span.set_attribute("gen_ai.response.model", res.model)
    if getattr(res, "id", None):
        span.set_attribute("gen_ai.response.id", res.id)
    u = getattr(res, "usage", None)
    # openai reports prompt/completion_tokens, anthropic input/output_tokens
    i = _first(getattr(u, "input_tokens", None), getattr(u, "prompt_tokens", None))
    o = _first(getattr(u, "output_tokens", None), getattr(u, "completion_tokens", None))
    if i is not None:
        span.set_attribute("gen_ai.usage.input_tokens", i)
    if o is not None:
        span.set_attribute("gen_ai.usage.output_tokens", o)
    reasons = [getattr(ch, "finish_reason", None) for ch in (getattr(res, "choices", None) or [])]
    reasons = [r for r in reasons if r] or ([res.stop_reason] if getattr(res, "stop_reason", None) else [])
    if reasons:
        span.set_attribute("gen_ai.response.finish_reasons", reasons)


def instrument_openai(client):
    """Wrap `client.chat.completions.create` on an `openai.OpenAI` instance (non-streaming)."""
    client.chat.completions.create = _traced("openai", "chat", client.chat.completions.create)
    return client


def instrument_anthropic(client):
    """Wrap `client.messages.create` on an `anthropic.Anthropic` instance (non-streaming)."""
    client.messages.create = _traced("anthropic", "chat", client.messages.create)
    return client


try:
    from langchain_core.callbacks import BaseCallbackHandler as _Base
except ImportError:  # extra not installed
    _Base = object


class WatchtowerCallbackHandler(_Base):
    """Pass as `callbacks=[WatchtowerCallbackHandler()]` to any langchain chat model or chain."""

    def __init__(self):
        self._spans = {}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kw):
        self.on_llm_start(serialized, [], run_id=run_id, **kw)

    def on_llm_start(self, serialized, prompts, *, run_id, invocation_params=None, **kw):
        p = invocation_params or {}
        name = p.get("model") or p.get("model_name") or ""
        span = _c._need().tracer.start_span(f"chat {name}".strip())
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.system", "langchain")
        span.set_attribute("gen_ai.request.model", name)
        self._spans[run_id] = span

    def on_llm_end(self, response, *, run_id, **kw):
        span = self._spans.pop(run_id, None)
        if span is None:
            return
        usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        if "prompt_tokens" in usage:
            span.set_attribute("gen_ai.usage.input_tokens", usage["prompt_tokens"])
        if "completion_tokens" in usage:
            span.set_attribute("gen_ai.usage.output_tokens", usage["completion_tokens"])
        span.end()

    def on_llm_error(self, error, *, run_id, **kw):
        span = self._spans.pop(run_id, None)
        if span is not None:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
            span.end()
