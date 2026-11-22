"""GenAI spans (OTel GenAI semconv) for openai, anthropic, AWS Bedrock, google-genai, LiteLLM and langchain;
Mistral, Ollama, LlamaIndex and the OpenAI Agents SDK are re-exported from `genai_more` and `genai_agents`.

Prompt/completion text is never recorded."""
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .. import client as _c
from .genai_agents import instrument_llamaindex, instrument_openai_agents  # noqa: F401 - re-export
from .genai_frameworks import instrument_bedrock_agents, instrument_crewai  # noqa: F401 - re-export
from .genai_more import instrument_mistral, instrument_ollama, traced  # noqa: F401 - re-export


def _first(*vals):
    return next((v for v in vals if v is not None), None)


def _traced(system, op, fn, model_kw="model", record=None):
    """Spans, streaming and tool calls come from `genai_more.traced`; `record` reads the response."""
    return traced(system, op, fn, model_kw, record or _record_response)


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
    """Wrap `client.chat.completions.create` on an `openai.OpenAI` or `openai.AsyncOpenAI` instance (`stream=True` too; pass
    `stream_options={"include_usage": True}` for token usage on streams)."""
    client.chat.completions.create = _traced("openai", "chat", client.chat.completions.create)
    return client


def instrument_anthropic(client):
    """Wrap `client.messages.create` on an `anthropic.Anthropic` or `AsyncAnthropic` instance (`stream=True` too)."""
    client.messages.create = _traced("anthropic", "chat", client.messages.create)
    return client


def _set_usage(span, i, o, reasons):
    if i is not None:
        span.set_attribute("gen_ai.usage.input_tokens", i)
    if o is not None:
        span.set_attribute("gen_ai.usage.output_tokens", o)
    reasons = [str(r) for r in reasons if r]
    if reasons:
        span.set_attribute("gen_ai.response.finish_reasons", reasons)


def _record_bedrock(span, res):
    # Converse returns a dict: usage.inputTokens/outputTokens and stopReason
    if not isinstance(res, dict):
        return
    u = res.get("usage") or {}
    rid = (res.get("ResponseMetadata") or {}).get("RequestId")
    if rid:
        span.set_attribute("gen_ai.response.id", rid)
    _set_usage(span, u.get("inputTokens"), u.get("outputTokens"), [res.get("stopReason")])


def _record_google(span, res):
    u = getattr(res, "usage_metadata", None)
    if getattr(res, "model_version", None):
        span.set_attribute("gen_ai.response.model", res.model_version)
    if getattr(res, "response_id", None):
        span.set_attribute("gen_ai.response.id", res.response_id)
    reasons = [getattr(getattr(c, "finish_reason", None), "name", None) or getattr(c, "finish_reason", None)
               for c in (getattr(res, "candidates", None) or [])]
    _set_usage(span, getattr(u, "prompt_token_count", None), getattr(u, "candidates_token_count", None), reasons)


def instrument_bedrock(client):
    """Wrap `converse` and `invoke_model` on a boto3 `bedrock-runtime` client (non-streaming).

    `invoke_model` bodies are model-specific, so only `converse` records token usage."""
    client.converse = _traced("aws.bedrock", "chat", client.converse, "modelId", _record_bedrock)
    if hasattr(client, "invoke_model"):
        client.invoke_model = _traced("aws.bedrock", "chat", client.invoke_model, "modelId", lambda span, res: None)
    return client


def instrument_google_genai(client):
    """Wrap `models.generate_content` and `aio.models.generate_content` on a `google.genai.Client`
    (Gemini API or Vertex AI)."""
    system = "vertex_ai" if getattr(client, "vertexai", False) else "gemini"
    client.models.generate_content = _traced(system, "chat", client.models.generate_content, record=_record_google)
    aio = getattr(getattr(client, "aio", None), "models", None)
    if aio is not None:
        aio.generate_content = _traced(system, "chat", aio.generate_content, record=_record_google)
    return client


def instrument_litellm(module=None):
    """Wrap `litellm.completion` and `litellm.acompletion` (OpenAI-shaped responses). Pass the module,
    or it is imported."""
    if module is None:
        import litellm as module  # noqa: PLC0415 - optional dependency
    module.completion = _traced("litellm", "chat", module.completion)
    if hasattr(module, "acompletion"):
        module.acompletion = _traced("litellm", "chat", module.acompletion)
    return module


try:
    from langchain_core.callbacks import BaseCallbackHandler as _Base
except ImportError:  # extra not installed
    _Base = object


class WatchtowerCallbackHandler(_Base):
    """Pass as `callbacks=[WatchtowerCallbackHandler()]` to any langchain chat model, chain or LangGraph
    graph. LangGraph nodes (chains whose metadata names `langgraph_node`) and the graph itself become
    `invoke_agent` spans, tools `execute_tool` spans, model calls `chat` spans, nested as they run."""

    def __init__(self):
        self._spans = {}
        self._up = {}  # run_id -> parent run_id, kept for runs that get no span so children skip over them

    def _open(self, run_id, parent_run_id, title, attrs):
        self._up[run_id] = parent_run_id
        up = parent_run_id
        while up is not None and up not in self._spans:
            up = self._up.get(up)
        ctx = trace.set_span_in_context(self._spans[up]) if up is not None else None
        span = _c._need().tracer.start_span(title, context=ctx)
        for k, v in attrs.items():
            if v:
                span.set_attribute(k, v)
        self._spans[run_id] = span

    def _close(self, run_id, error=None):
        self._up.pop(run_id, None)
        span = self._spans.pop(run_id, None)
        if span is None:
            return None
        if error is not None:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
        return span

    def on_chat_model_start(self, serialized, messages, *, run_id, **kw):
        self.on_llm_start(serialized, [], run_id=run_id, **kw)

    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, invocation_params=None, **kw):
        p = invocation_params or {}
        name = p.get("model") or p.get("model_name") or ""
        self._open(run_id, parent_run_id, f"chat {name}".strip(), {
            "gen_ai.operation.name": "chat", "gen_ai.system": "langchain", "gen_ai.request.model": name})

    def on_llm_end(self, response, *, run_id, **kw):
        span = self._close(run_id)
        if span is None:
            return
        usage = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        if "prompt_tokens" in usage:
            span.set_attribute("gen_ai.usage.input_tokens", usage["prompt_tokens"])
        if "completion_tokens" in usage:
            span.set_attribute("gen_ai.usage.output_tokens", usage["completion_tokens"])
        span.end()

    def on_llm_error(self, error, *, run_id, **kw):
        span = self._close(run_id, error)
        if span is not None:
            span.end()

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, metadata=None, name=None, **kw):
        node = (metadata or {}).get("langgraph_node")
        # Only LangGraph runs get spans (the graph root and its nodes): plain LCEL chains are noise.
        if node is None and not (parent_run_id is None and (metadata or {}).get("langgraph_step") is not None):
            self._up[run_id] = parent_run_id
            return
        who = node if (node and name == node) else (name or node or "graph")
        self._open(run_id, parent_run_id, f"invoke_agent {who}", {
            "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": who, "gen_ai.system": "langgraph"})

    def on_chain_end(self, outputs, *, run_id, **kw):
        span = self._close(run_id)
        if span is not None:
            span.end()

    def on_chain_error(self, error, *, run_id, **kw):
        span = self._close(run_id, error)
        if span is not None:
            span.end()

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, name=None, **kw):
        tool = name or (serialized or {}).get("name") or ""
        self._open(run_id, parent_run_id, f"execute_tool {tool}".strip(), {
            "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool})

    def on_tool_end(self, output, *, run_id, **kw):
        span = self._close(run_id)
        if span is not None:
            span.end()

    def on_tool_error(self, error, *, run_id, **kw):
        span = self._close(run_id, error)
        if span is not None:
            span.end()
