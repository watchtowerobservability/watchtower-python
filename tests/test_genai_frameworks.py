from types import SimpleNamespace as NS

from watchtower.integrations import genai


def test_langgraph_nodes_and_tools_nest(cap):
    h = genai.WatchtowerCallbackHandler()
    h.on_chain_start({}, {}, run_id="g", metadata={"langgraph_step": 0}, name="LangGraph")
    h.on_chain_start({}, {}, run_id="n", parent_run_id="g", metadata={"langgraph_node": "agent"}, name="agent")
    h.on_chain_start({}, {}, run_id="seq", parent_run_id="n", metadata={"langgraph_node": "agent"}, name="RunnableSeq")
    h.on_tool_start({"name": "search"}, "q", run_id="t", parent_run_id="seq", name="search")
    h.on_tool_end("r", run_id="t")
    h.on_chain_end({}, run_id="seq")
    h.on_chain_end({}, run_id="n")
    h.on_chain_end({}, run_id="g")
    by = {s.name: s for s in cap.spans}
    assert by["execute_tool search"].attributes["gen_ai.tool.name"] == "search"
    assert by["execute_tool search"].parent.span_id == by["invoke_agent agent"].context.span_id
    assert by["invoke_agent agent"].parent.span_id == by["invoke_agent LangGraph"].context.span_id


def test_plain_chain_gets_no_span(cap):
    h = genai.WatchtowerCallbackHandler()
    h.on_chain_start({}, {}, run_id="c", metadata={}, name="chain")
    h.on_chain_end({}, run_id="c")
    assert cap.spans == []


def test_crewai_spans(cap):
    class Agent:
        role = "writer"

        def execute_task(self, task):
            return task

    class Crew:
        name = "c1"

        def kickoff(self):
            return Agent().execute_task("x")

    genai.instrument_crewai(NS(Crew=Crew, Agent=Agent))
    assert Crew().kickoff() == "x"
    names = [s.name for s in cap.spans]
    assert names == ["invoke_agent writer", "invoke_agent c1"]
    assert cap.spans[0].attributes["gen_ai.agent.name"] == "writer"


def test_bedrock_agent_trace_becomes_spans(cap):
    events = [
        {"trace": {"trace": {"orchestrationTrace": {"modelInvocationOutput": {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 7}}}}}}},
        {"trace": {"trace": {"orchestrationTrace": {"invocationInput": {"actionGroupInvocationInput": {"function": "lookup"}}}}}},
        {"trace": {"trace": {"orchestrationTrace": {"observation": {"type": "ACTION_GROUP"}}}}},
        {"chunk": {"bytes": b"hi"}},
    ]
    seen = {}

    def invoke_agent(**kw):
        seen.update(kw)
        return {"completion": iter(events)}

    c = genai.instrument_bedrock_agents(NS(invoke_agent=invoke_agent))
    out = list(c.invoke_agent(agentId="A1", sessionId="s")["completion"])
    assert out == events and seen["enableTrace"] is True
    by = {s.name: s for s in cap.spans}
    assert by["chat"].attributes["gen_ai.usage.output_tokens"] == 7
    assert by["execute_tool lookup"].parent.span_id == by["invoke_agent A1"].context.span_id
