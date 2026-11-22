"""GenAI depth shared by every wrapper, plus Mistral, Ollama, LlamaIndex and the OpenAI Agents SDK.

Streaming calls keep their span open until the stream ends and record time to first chunk, token
usage, finish reasons and tool calls from the chunks. Tool calls become `gen_ai.tool.call` span
events carrying only the tool name and call id. Cost is derived server side from the model and
token counts. Prompt, completion and tool argument text is never recorded."""
import inspect
import time

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .. import client as _c
from ..errors import capture_exception


def _g(o, k):
    """Attribute or mapping key, whichever the SDK object offers."""
    if o is None:
        return None
    v = getattr(o, k, None)
    if v is None and isinstance(o, dict):
        v = o.get(k)
    return v


def _first(*vals):
    return next((v for v in vals if v is not None), None)


def extract(o):
    """Id, model, tokens, finish reasons and tool calls of a response, chunk or stream event of
    the OpenAI, Anthropic, Mistral (camel or snake case) or Ollama SDKs."""
    o = _first(_g(o, "data"), o) if _g(o, "choices") is None and _g(o, "type") is None else o
    msg = _first(_g(o, "message"), _g(o, "delta"))
    u = _first(_g(o, "usage"), _g(msg, "usage") if _g(o, "type") == "message_start" else None)
    out = {
        "id": _first(_g(o, "id"), _g(msg, "id") if _g(o, "type") == "message_start" else None),
        "model": _first(_g(o, "model"), _g(msg, "model") if _g(o, "type") == "message_start" else None),
        "input": _first(_g(u, "input_tokens"), _g(u, "prompt_tokens"), _g(u, "promptTokens"), _g(o, "prompt_eval_count")),
        "output": _first(_g(u, "output_tokens"), _g(u, "completion_tokens"), _g(u, "completionTokens"), _g(o, "eval_count")),
        "reasons": [],
        "tools": [],
    }
    for ch in _g(o, "choices") or []:
        out["reasons"].append(_first(_g(ch, "finish_reason"), _g(ch, "finishReason")))
        m = _first(_g(ch, "message"), _g(ch, "delta"))
        for tc in _first(_g(m, "tool_calls"), _g(m, "toolCalls")) or []:
            out["tools"].append((_g(_g(tc, "function"), "name"), _g(tc, "id")))
    out["reasons"] += [_g(o, "stop_reason"), _g(_g(o, "delta"), "stop_reason"), _g(o, "done_reason")]
    for block in _g(o, "content") if isinstance(_g(o, "content"), list) else []:
        if _g(block, "type") == "tool_use":
            out["tools"].append((_g(block, "name"), _g(block, "id")))
    cb = _g(o, "content_block")
    if _g(cb, "type") == "tool_use":
        out["tools"].append((_g(cb, "name"), _g(cb, "id")))
    if _g(o, "choices") is None:  # Ollama: message.tool_calls
        for tc in _g(_g(o, "message"), "tool_calls") or []:
            out["tools"].append((_g(_g(tc, "function"), "name"), None))
    out["reasons"] = [str(r) for r in out["reasons"] if r]
    out["tools"] = [(str(n), i) for n, i in out["tools"] if n]
    return out


def apply(span, x, seen=None):
    """Set the extracted fields on `span`; `seen` keeps tool calls of a stream from repeating."""
    seen = set() if seen is None else seen
    if x["id"]:
        span.set_attribute("gen_ai.response.id", str(x["id"]))
    if x["model"]:
        span.set_attribute("gen_ai.response.model", str(x["model"]))
    if isinstance(x["input"], int):
        span.set_attribute("gen_ai.usage.input_tokens", x["input"])
    if isinstance(x["output"], int):
        span.set_attribute("gen_ai.usage.output_tokens", x["output"])
    if x["reasons"]:
        span.set_attribute("gen_ai.response.finish_reasons", x["reasons"])
    for name, cid in x["tools"]:
        if (name, cid) in seen:
            continue
        seen.add((name, cid))
        span.add_event("gen_ai.tool.call", {"gen_ai.tool.name": name, **({"gen_ai.tool.call.id": str(cid)} if cid else {})})


def _fail(span, e):
    capture_exception(e)
    span.record_exception(e)
    span.set_status(Status(StatusCode.ERROR, str(e)))
    span.end()


class _Stream:
    """Proxies a sync or async stream; the span ends when the stream is exhausted, fails or closes."""

    def __init__(self, inner, span, started):
        self._inner, self._span, self._t0 = inner, span, started
        self._it = None
        self._first = True
        self._done = False
        self._reasons, self._seen = [], set()

    def __getattr__(self, k):
        return getattr(self._inner, k)

    def _chunk(self, c):
        if self._first:
            self._first = False
            self._span.set_attribute("gen_ai.response.time_to_first_chunk", time.monotonic() - self._t0)
        x = extract(c)
        self._reasons += [r for r in x["reasons"] if r not in self._reasons]
        x["reasons"] = self._reasons
        apply(self._span, x, self._seen)

    def _end(self, e=None):
        if self._done:
            return
        self._done = True
        if e is None:
            self._span.end()
        else:
            _fail(self._span, e)

    def __iter__(self):
        self._it = iter(self._inner)
        return self

    def __next__(self):
        try:
            c = next(self._it if self._it is not None else iter(self._inner))
        except StopIteration:
            self._end()
            raise
        except Exception as e:
            self._end(e)
            raise
        self._chunk(c)
        return c

    def __aiter__(self):
        self._it = self._inner.__aiter__()
        return self

    async def __anext__(self):
        try:
            c = await self._it.__anext__()
        except StopAsyncIteration:
            self._end()
            raise
        except Exception as e:
            self._end(e)
            raise
        self._chunk(c)
        return c

    async def __aenter__(self):
        if hasattr(self._inner, "__aenter__"):
            await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        self._end()
        return await self._inner.__aexit__(*exc) if hasattr(self._inner, "__aexit__") else False

    def __enter__(self):
        if hasattr(self._inner, "__enter__"):
            self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        self._end()
        return self._inner.__exit__(*exc) if hasattr(self._inner, "__exit__") else False

    def close(self):
        self._end()
        if hasattr(self._inner, "close"):
            self._inner.close()


def traced(system, op, fn, model_kw="model", record=None, always_stream=False):
    """Wrap `fn` in a client span; `record(span, res)` overrides the generic response reader.

    Async clients work too: when `fn` returns an awaitable, the span covers the awaited call."""

    def finish(span, res, t0, kw):
        if always_stream or kw.get("stream") is True:
            return _Stream(res, span, t0)
        if record is not None:
            record(span, res)
            apply(span, {**extract(res), "id": None, "model": None, "input": None, "output": None, "reasons": []})
        else:
            apply(span, extract(res))
        span.end()
        return res

    async def awaited(aw, span, t0, kw):
        try:
            with trace.use_span(span, end_on_exit=False):
                res = await aw
        except Exception as e:
            _fail(span, e)
            raise
        return finish(span, res, t0, kw)

    def inner(*a, **kw):
        model = kw.get(model_kw) or ""
        span = _c._need().tracer.start_span(f"{op} {model}".strip(), kind=trace.SpanKind.CLIENT)
        span.set_attribute("gen_ai.operation.name", op)
        span.set_attribute("gen_ai.system", system)
        span.set_attribute("gen_ai.request.model", model)
        for k in ("temperature", "max_tokens", "top_p"):
            if isinstance(kw.get(k), (int, float)):
                span.set_attribute(f"gen_ai.request.{k}", kw[k])
        t0 = time.monotonic()
        try:
            with trace.use_span(span, end_on_exit=False):
                res = fn(*a, **kw)
        except Exception as e:
            _fail(span, e)
            raise
        # openai's async `create` is a sync wrapper returning a coroutine, so test the result
        if inspect.isawaitable(res):
            return awaited(res, span, t0, kw)
        return finish(span, res, t0, kw)

    inner.__wrapped__ = fn
    return inner


def instrument_mistral(client):
    """Wrap `chat.complete`, `chat.stream` and their `_async` variants on a `mistralai.Mistral` instance."""
    client.chat.complete = traced("mistral_ai", "chat", client.chat.complete)
    for name, stream in (("stream", True), ("complete_async", False), ("stream_async", True)):
        if hasattr(client.chat, name):
            setattr(client.chat, name, traced("mistral_ai", "chat", getattr(client.chat, name), always_stream=stream))
    return client


def instrument_ollama(client=None):
    """Wrap `chat` and `generate` on an `ollama.Client` or `ollama.AsyncClient`, or on the `ollama`
    module when none is given."""
    if client is None:
        import ollama as client  # noqa: PLC0415 - optional dependency
    client.chat = traced("ollama", "chat", client.chat)
    if hasattr(client, "generate"):
        client.generate = traced("ollama", "text_completion", client.generate)
    return client
