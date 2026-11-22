"""GenAI spans for frameworks that report through their own instrumentation hooks: LlamaIndex
(the `llama_index.core.instrumentation` dispatcher) and the OpenAI Agents SDK (trace processors).

Agent runs become `invoke_agent` spans, model calls `chat` spans with tokens and finish reasons,
tool runs `execute_tool` spans with the tool name, all nested as the framework nests them. No
message, completion or tool argument text is recorded."""
import time

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .. import client as _c
from .genai_more import _g, apply, extract


def _start(name, attrs, parent=None):
    ctx = trace.set_span_in_context(parent) if parent is not None else None
    span = _c._need().tracer.start_span(name, context=ctx, kind=trace.SpanKind.INTERNAL)
    for k, v in attrs.items():
        if v is not None and v != "":
            span.set_attribute(k, v)
    return span


def _usage_of(resp):
    """Token usage of a LlamaIndex `ChatResponse`/`CompletionResponse`: the provider's raw response,
    else the counts LlamaIndex puts in `additional_kwargs`."""
    x = extract(_g(resp, "raw"))
    kw = _g(resp, "additional_kwargs") or {}
    x["input"] = x["input"] if x["input"] is not None else kw.get("prompt_tokens")
    x["output"] = x["output"] if x["output"] is not None else kw.get("completion_tokens")
    for tc in (_g(_g(resp, "message"), "additional_kwargs") or {}).get("tool_calls") or []:
        x["tools"].append((_g(_g(tc, "function"), "name"), _g(tc, "id")))
    x["tools"] = [(str(n), i) for n, i in x["tools"] if n]
    return x


class LlamaIndexSpans:
    """Maps LlamaIndex instrumentation events to spans (the handler below forwards to `handle`)."""

    def __init__(self):
        self.spans = {}
        self.t0 = {}

    def handle(self, event):
        kind = type(event).__name__
        key = getattr(event, "span_id", None) or getattr(event, "id_", None)
        if kind in ("LLMChatStartEvent", "LLMCompletionStartEvent"):
            model = (getattr(event, "model_dict", None) or {}).get("model") or ""
            op = "chat" if kind == "LLMChatStartEvent" else "text_completion"
            self.spans[key] = _start(f"{op} {model}".strip(), {
                "gen_ai.operation.name": op, "gen_ai.system": "llama_index", "gen_ai.request.model": model})
            self.t0[key] = time.monotonic()
        elif kind in ("LLMChatInProgressEvent", "LLMCompletionInProgressEvent"):
            span, t0 = self.spans.get(key), self.t0.pop(key, None)
            if span is not None and t0 is not None:
                span.set_attribute("gen_ai.response.time_to_first_chunk", time.monotonic() - t0)
        elif kind in ("LLMChatEndEvent", "LLMCompletionEndEvent"):
            span = self.spans.pop(key, None)
            self.t0.pop(key, None)
            if span is not None:
                apply(span, _usage_of(getattr(event, "response", None)))
                span.end()
        elif kind == "AgentToolCallEvent":
            tool = getattr(event, "tool", None)
            name = _g(tool, "name") or ""
            _start(f"execute_tool {name}".strip(), {
                "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name}).end()


def instrument_llamaindex(dispatcher=None):
    """Register a span handler on the LlamaIndex root dispatcher (or the one given)."""
    from llama_index.core.instrumentation import get_dispatcher  # noqa: PLC0415 - optional dependency
    from llama_index.core.instrumentation.event_handlers import BaseEventHandler  # noqa: PLC0415

    spans = LlamaIndexSpans()

    class WatchtowerEventHandler(BaseEventHandler):
        @classmethod
        def class_name(cls):
            return "WatchtowerEventHandler"

        def handle(self, event, **kwargs):
            try:
                spans.handle(event)
            except Exception:  # noqa: BLE001 - telemetry must never break the app
                pass

    (dispatcher or get_dispatcher()).add_event_handler(WatchtowerEventHandler())
    return spans


class AgentsSpans:
    """An OpenAI Agents SDK trace processor (duck-typed: the SDK calls these methods)."""

    def __init__(self):
        self.spans = {}

    def on_trace_start(self, t):
        pass

    def on_trace_end(self, t):
        pass

    def on_span_start(self, s):
        d = getattr(s, "span_data", None)
        typ = _g(d, "type") or ""
        parent = self.spans.get(getattr(s, "parent_id", None))
        if typ == "agent":
            name = _g(d, "name") or ""
            attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name, "gen_ai.system": "openai"}
            title = f"invoke_agent {name}".strip()
        elif typ in ("generation", "response"):
            model = _g(d, "model") or ""
            attrs = {"gen_ai.operation.name": "chat", "gen_ai.system": "openai", "gen_ai.request.model": model}
            title = f"chat {model}".strip()
        elif typ == "function":
            name = _g(d, "name") or ""
            attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name}
            title = f"execute_tool {name}".strip()
        elif typ == "handoff":
            attrs = {"gen_ai.operation.name": "handoff", "gen_ai.handoff.from_agent": _g(d, "from_agent"),
                     "gen_ai.handoff.to_agent": _g(d, "to_agent")}
            title = "handoff"
        elif typ == "guardrail":
            attrs = {"gen_ai.guardrail.name": _g(d, "name")}
            title = f"guardrail {_g(d, 'name') or ''}".strip()
        else:
            attrs, title = {}, typ or "agents"
        self.spans[getattr(s, "span_id", None)] = _start(title, attrs, parent)

    def on_span_end(self, s):
        span = self.spans.pop(getattr(s, "span_id", None), None)
        if span is None:
            return
        d = getattr(s, "span_data", None)
        typ = _g(d, "type")
        if typ == "generation":
            u = _g(d, "usage") or {}
            apply(span, {"id": None, "model": _g(d, "model"), "input": _g(u, "input_tokens"),
                         "output": _g(u, "output_tokens"), "reasons": [], "tools": []})
        elif typ == "response":
            apply(span, extract(_g(d, "response")))
        elif typ == "guardrail":
            span.set_attribute("gen_ai.guardrail.triggered", bool(_g(d, "triggered")))
        err = getattr(s, "error", None)
        if err:
            span.set_status(Status(StatusCode.ERROR, str(_g(err, "message") or "error")))
        span.end()

    def shutdown(self):
        for span in self.spans.values():
            span.end()
        self.spans.clear()

    def force_flush(self):
        pass


def instrument_openai_agents():
    """Add a trace processor to the OpenAI Agents SDK (`agents`); its own tracing keeps running."""
    from agents import add_trace_processor  # noqa: PLC0415 - optional dependency

    p = AgentsSpans()
    add_trace_processor(p)
    return p
