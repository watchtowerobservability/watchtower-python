from types import SimpleNamespace as NS

from watchtower.integrations import genai
from watchtower.integrations.genai_agents import AgentsSpans, LlamaIndexSpans


def _events(span):
    return [(e.name, dict(e.attributes)) for e in span.events]


def test_openai_stream_records_ttft_usage_and_tool_calls(cap):
    chunks = [
        NS(id="c1", model="gpt-4o", choices=[NS(finish_reason=None, delta=NS(tool_calls=[NS(id="t1", function=NS(name="get_weather"))]))], usage=None),
        NS(id="c1", model="gpt-4o", choices=[NS(finish_reason="tool_calls", delta=NS(tool_calls=[NS(id=None, function=NS(name=None))]))], usage=None),
        NS(id="c1", model="gpt-4o", choices=[], usage=NS(prompt_tokens=7, completion_tokens=3)),
    ]
    client = NS(chat=NS(completions=NS(create=lambda **kw: iter(chunks))))
    stream = genai.instrument_openai(client).chat.completions.create(model="gpt-4o", stream=True, messages=[{"content": "secret"}])
    assert cap.spans == []  # open until the stream ends
    assert len(list(stream)) == 3
    s = cap.spans[0]
    a = s.attributes
    assert a["gen_ai.usage.input_tokens"] == 7 and a["gen_ai.usage.output_tokens"] == 3
    assert a["gen_ai.response.finish_reasons"] == ("tool_calls",)
    assert a["gen_ai.response.time_to_first_chunk"] >= 0
    assert _events(s) == [("gen_ai.tool.call", {"gen_ai.tool.name": "get_weather", "gen_ai.tool.call.id": "t1"})]
    assert "secret" not in str(a)


def test_anthropic_tool_use_and_stream_events(cap):
    res = NS(id="m1", model="claude", usage=NS(input_tokens=5, output_tokens=2), stop_reason="tool_use",
             content=[NS(type="text", text="secret"), NS(type="tool_use", id="tu1", name="search")])
    client = NS(messages=NS(create=lambda **kw: res))
    genai.instrument_anthropic(client).messages.create(model="claude")
    assert _events(cap.spans[0]) == [("gen_ai.tool.call", {"gen_ai.tool.name": "search", "gen_ai.tool.call.id": "tu1"})]
    events = [
        NS(type="message_start", message=NS(id="m2", model="claude", usage=NS(input_tokens=9, output_tokens=1))),
        NS(type="content_block_start", content_block=NS(type="tool_use", id="tu2", name="lookup")),
        NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=4)),
    ]
    client2 = NS(messages=NS(create=lambda **kw: iter(events)))
    for _ in genai.instrument_anthropic(client2).messages.create(model="claude", stream=True):
        pass
    a = cap.spans[1].attributes
    assert a["gen_ai.usage.input_tokens"] == 9 and a["gen_ai.usage.output_tokens"] == 4
    assert a["gen_ai.response.finish_reasons"] == ("end_turn",) and a["gen_ai.response.id"] == "m2"
    assert _events(cap.spans[1])[0][1]["gen_ai.tool.name"] == "lookup"


def test_mistral_and_ollama(cap):
    res = NS(id="r", model="mistral-large", usage=NS(prompt_tokens=4, completion_tokens=6),
             choices=[NS(finish_reason="stop", message=NS(tool_calls=None))])
    stream = [NS(data=NS(id="r2", model="mistral-small", choices=[NS(finish_reason="stop", delta=NS())],
                         usage=NS(prompt_tokens=1, completion_tokens=2)))]
    m = genai.instrument_mistral(NS(chat=NS(complete=lambda **kw: res, stream=lambda **kw: iter(stream))))
    m.chat.complete(model="mistral-large", messages=[])
    list(m.chat.stream(model="mistral-small", messages=[]))
    a, b = cap.spans[0].attributes, cap.spans[1].attributes
    assert a["gen_ai.system"] == "mistral_ai" and a["gen_ai.usage.output_tokens"] == 6
    assert b["gen_ai.usage.input_tokens"] == 1 and b["gen_ai.response.model"] == "mistral-small"

    reply = {"model": "llama3.2", "prompt_eval_count": 12, "eval_count": 8, "done_reason": "stop",
             "message": {"content": "secret", "tool_calls": [{"function": {"name": "add", "arguments": {"a": 1}}}]}}
    o = genai.instrument_ollama(NS(chat=lambda **kw: reply, generate=lambda **kw: {"eval_count": 1}))
    o.chat(model="llama3.2", messages=[])
    c = cap.spans[2]
    assert c.attributes["gen_ai.system"] == "ollama" and c.attributes["gen_ai.usage.input_tokens"] == 12
    assert _events(c) == [("gen_ai.tool.call", {"gen_ai.tool.name": "add"})]
    assert "secret" not in str(c.attributes) and "arguments" not in str(_events(c))


def test_stream_error_ends_span_with_error(cap):
    def boom():
        yield NS(choices=[], usage=None)
        raise RuntimeError("cut")

    client = NS(chat=NS(completions=NS(create=lambda **kw: boom())))
    s = genai.instrument_openai(client).chat.completions.create(model="m", stream=True)
    try:
        list(s)
    except RuntimeError:
        pass
    assert cap.spans[0].status.status_code.name == "ERROR"


def test_llamaindex_events_become_spans(cap):
    h = LlamaIndexSpans()

    class LLMChatStartEvent(NS):
        pass

    class LLMChatEndEvent(NS):
        pass

    class AgentToolCallEvent(NS):
        pass

    h.handle(LLMChatStartEvent(span_id="s1", model_dict={"model": "gpt-4o-mini"}))
    raw = NS(usage=NS(prompt_tokens=3, completion_tokens=4), choices=[])
    msg = NS(additional_kwargs={"tool_calls": [NS(id="x", function=NS(name="calc"))]})
    h.handle(LLMChatEndEvent(span_id="s1", response=NS(raw=raw, additional_kwargs={}, message=msg)))
    h.handle(AgentToolCallEvent(span_id="s2", tool=NS(name="calc")))
    a = cap.spans[0].attributes
    assert a["gen_ai.system"] == "llama_index" and a["gen_ai.request.model"] == "gpt-4o-mini"
    assert a["gen_ai.usage.output_tokens"] == 4
    assert _events(cap.spans[0])[0][1]["gen_ai.tool.name"] == "calc"
    assert cap.spans[1].attributes["gen_ai.operation.name"] == "execute_tool"


def test_openai_agents_processor_nests_agent_model_and_tool_spans(cap):
    p = AgentsSpans()
    agent = NS(span_id="a", parent_id=None, span_data=NS(type="agent", name="triage"), error=None)
    gen = NS(span_id="g", parent_id="a", error=None,
             span_data=NS(type="generation", model="gpt-4.1", usage={"input_tokens": 10, "output_tokens": 5}))
    tool = NS(span_id="f", parent_id="a", span_data=NS(type="function", name="refund"),
              error={"message": "denied"})
    for s in (agent, gen):
        p.on_span_start(s)
    p.on_span_end(gen)
    p.on_span_start(tool)
    p.on_span_end(tool)
    p.on_span_end(agent)
    g, f, a = cap.spans
    assert a.attributes["gen_ai.agent.name"] == "triage" and a.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert g.attributes["gen_ai.usage.input_tokens"] == 10 and g.parent.span_id == a.context.span_id
    assert f.attributes["gen_ai.tool.name"] == "refund" and f.status.status_code.name == "ERROR"


def test_async_clients_span_the_awaited_call(cap):
    import asyncio

    res = NS(id="a1", model="gpt-4o", usage=NS(prompt_tokens=2, completion_tokens=5),
             choices=[NS(finish_reason="stop", message=NS(tool_calls=None))])

    ended_before = []

    async def create(**kw):
        ended_before.append(len(cap.spans))  # the call's own span is still open while it runs
        return res

    class _AsyncStream:
        def __init__(self):
            self._it = iter([NS(type="message_start", message=NS(id="m", model="claude", usage=NS(input_tokens=3, output_tokens=1))),
                             NS(type="message_delta", delta=NS(stop_reason="end_turn"), usage=NS(output_tokens=8))])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration from None

    async def stream(**kw):
        return _AsyncStream()

    async def run():
        out = await genai.instrument_openai(NS(chat=NS(completions=NS(create=create)))).chat.completions.create(model="gpt-4o")
        assert out is res
        s = await genai.instrument_anthropic(NS(messages=NS(create=stream))).messages.create(model="claude", stream=True)
        async for _ in s:
            pass
        m = genai.instrument_mistral(NS(chat=NS(complete=lambda **kw: res, complete_async=create)))
        await m.chat.complete_async(model="mistral-large")

    asyncio.run(run())
    assert ended_before == [0, 2]
    a, b, c = (s.attributes for s in cap.spans)
    assert a["gen_ai.usage.output_tokens"] == 5 and a["gen_ai.response.id"] == "a1"
    assert b["gen_ai.usage.input_tokens"] == 3 and b["gen_ai.usage.output_tokens"] == 8
    assert c["gen_ai.system"] == "mistral_ai"
