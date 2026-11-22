"""Agent-framework spans: CrewAI and Bedrock Agents (LangGraph rides the langchain callback handler in
`genai.py`). Same shape as `genai_agents`: `invoke_agent` / `chat` / `execute_tool` spans, never any
prompt, completion or tool argument text."""
import functools

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .. import client as _c
from .genai_agents import _start


def _wrap(owner, attr, title, attrs_of):
    """Replace `owner.attr` with a method that runs inside an agent-style span. Missing attr is skipped."""
    fn = getattr(owner, attr, None)
    if fn is None:
        return

    @functools.wraps(fn)
    def run(self, *a, **kw):
        attrs = attrs_of(self)
        span = _c._need().tracer.start_span(f"{title} {attrs.get('name', '')}".strip(), kind=trace.SpanKind.INTERNAL)
        for k, v in attrs.items():
            if k != "name" and v:
                span.set_attribute(k, v)
        with trace.use_span(span, end_on_exit=True, record_exception=True, set_status_on_exception=True):
            return fn(self, *a, **kw)

    setattr(owner, attr, run)


def instrument_crewai(crewai=None):
    """Span `Crew.kickoff` (the crew run), `Agent.execute_task` (one agent's task) and tool runs. The
    model calls inside are the litellm / provider spans, nested here. Pass the `crewai` module, or it is imported."""
    if crewai is None:
        import crewai  # noqa: PLC0415 - optional dependency
    op = "gen_ai.operation.name"
    _wrap(crewai.Crew, "kickoff", "invoke_agent",
          lambda s: {"name": getattr(s, "name", None) or "crew", op: "invoke_agent",
                     "gen_ai.agent.name": getattr(s, "name", None) or "crew", "gen_ai.system": "crewai"})
    _wrap(crewai.Agent, "execute_task", "invoke_agent",
          lambda s: {"name": getattr(s, "role", ""), op: "invoke_agent", "gen_ai.agent.name": getattr(s, "role", ""),
                     "gen_ai.system": "crewai"})
    tools = getattr(getattr(crewai, "tools", None), "BaseTool", None)
    if tools is not None:
        _wrap(tools, "run", "execute_tool",
              lambda s: {"name": getattr(s, "name", ""), op: "execute_tool", "gen_ai.tool.name": getattr(s, "name", "")})
    return crewai


class _BedrockTrace:
    """Turns the `trace` events of a Bedrock `invoke_agent` stream into child spans of the agent run."""

    def __init__(self, parent):
        self.parent = parent
        self.tool = None

    def _child(self, name, attrs):
        s = _start(name, attrs, self.parent)
        return s

    def _end_tool(self):
        if self.tool is not None:
            self.tool.end()
            self.tool = None

    def event(self, ev):
        t = (((ev or {}).get("trace") or {}).get("trace") or {}).get("orchestrationTrace") or {}
        out = t.get("modelInvocationOutput") or {}
        usage = (out.get("metadata") or {}).get("usage") or {}
        if usage:
            s = self._child("chat", {"gen_ai.operation.name": "chat", "gen_ai.system": "aws.bedrock",
                                     "gen_ai.usage.input_tokens": usage.get("inputTokens"),
                                     "gen_ai.usage.output_tokens": usage.get("outputTokens")})
            s.end()
        inv = t.get("invocationInput") or {}
        ag = inv.get("actionGroupInvocationInput") or {}
        if ag or inv.get("knowledgeBaseLookupInput"):
            self._end_tool()
            name = ag.get("function") or ag.get("apiPath") or ag.get("actionGroupName") or "knowledge_base"
            self.tool = self._child(f"execute_tool {name}", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name})
        if t.get("observation"):
            self._end_tool()

    def close(self):
        self._end_tool()


def instrument_bedrock_agents(client):
    """Wrap `invoke_agent` on a boto3 `bedrock-agent-runtime` client. Forces `enableTrace=True` so the
    reply stream carries the orchestration trace; events pass through unchanged. The agent span ends when
    the stream is exhausted."""
    fn = client.invoke_agent

    @functools.wraps(fn)
    def invoke(*a, **kw):
        kw.setdefault("enableTrace", True)
        span = _start(f"invoke_agent {kw.get('agentId', '')}".strip(),
                      {"gen_ai.operation.name": "invoke_agent", "gen_ai.system": "aws.bedrock",
                       "gen_ai.agent.id": kw.get("agentId")})
        try:
            res = fn(*a, **kw)
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.end()
            raise
        stream = res.get("completion") if isinstance(res, dict) else None
        if stream is None:
            span.end()
            return res
        tr = _BedrockTrace(span)

        def gen():
            try:
                for ev in stream:
                    tr.event(ev)
                    yield ev
            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                raise
            finally:
                tr.close()
                span.end()

        res["completion"] = gen()
        return res

    client.invoke_agent = invoke
    return client
