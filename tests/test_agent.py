"""Monitoring tools, the LangGraph loop and the Telegram bot, with a scripted fake model."""

import json
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from ops_agent.bot import Bot
from ops_agent.graph import build_graph, context_window
from ops_agent.tools.http_probe import HttpProbe
from ops_agent.tools.monitoring import Monitoring, monitoring_tools

ALERTS = [{"labels": {"alertname": "NodeDown", "severity": "critical", "env": "staging", "node": "staging"},
           "annotations": {"summary": "staging down"}, "startsAt": "2026-11-04T08:00:00Z",
           "status": {"state": "active"}}]


def handler(req: httpx.Request) -> httpx.Response:
    path, q = req.url.path, req.url.params
    assert req.method == "GET"
    if path == "/api/v2/alerts":
        return httpx.Response(200, json=ALERTS)
    if path == "/api/v1/query":
        if q["query"] == "bad(":
            return httpx.Response(400, json={"status": "error", "errorType": "bad_data", "error": "parse error"})
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"__name__": "up", "node": "staging"}, "value": [1, "0"]}]}})
    if path == "/loki/api/v1/query_range":
        if "{" not in q["query"]:
            return httpx.Response(400, text="parse error: queries require at least one matcher")
        if "noisy" in q["query"]:
            assert int(q["limit"]) == 200  # 20x the shown limit
            spam = [[str(1762588800000000000 + i * 10**9), f"level=ERROR write tcp :9100->10.42.0.242:{40000 + i}: reset"]
                    for i in range(150)]
            rare = [[str(1762588700000000000), "level=ERROR disk quota exceeded"]]
            return httpx.Response(200, json={"data": {"resultType": "streams", "result": [
                {"stream": {"cluster": "staging", "node": "staging", "container": "node-exporter"}, "values": spam},
                {"stream": {"cluster": "staging", "namespace": "shop", "pod": "api-1"}, "values": rare}]}})
        return httpx.Response(200, json={"data": {"resultType": "streams", "result": [
            {"stream": {"cluster": "staging", "unit": "k3s.service"},
             "values": [["1762588801000000000", "second"], ["1762588800000000000", "first"]]}]}})
    return httpx.Response(404)


def monitoring() -> Monitoring:
    return Monitoring("http://prom", "http://am", "http://loki", "Asia/Dhaka",
                      client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_monitoring_tools():
    m = monitoring()
    tools = {t.name: t for t in monitoring_tools(m)}
    assert tools["get_alerts"].invoke({}).startswith("NodeDown (critical) since 04.11 14:00")
    assert tools["prometheus_query"].invoke({"query": "up"}) == 'up{node="staging"} 0'
    assert tools["prometheus_query"].invoke({"query": "bad("}) == "error: Prometheus: bad_data: parse error"
    logs = tools["loki_query"].invoke({"logql": '{unit="k3s.service"}'})
    assert logs.index("first") < logs.index("second") and "staging/k3s.service" in logs
    assert tools["loki_query"].invoke({"logql": "bad"}).startswith("error: Loki: parse error")

    noisy = tools["loki_query"].invoke({"logql": '{job="noisy"}', "limit": 10}).splitlines()
    assert noisy[0].startswith("151 lines, 2 distinct messages") and len(noisy) == 3
    assert "disk quota exceeded" in noisy[1] and "staging/shop/api-1" in noisy[1]  # older, still visible
    assert noisy[2].startswith("x150 08.11 14:00:00…14:02:29 staging/node-exporter")
    assert noisy[2].endswith("10.42.0.242:40149: reset")  # newest example


def test_http_probe_is_lan_only():
    probe = HttpProbe((".home.arpa",), transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")))
    assert "HTTP 200" in probe.check("https://gitea.example.home.arpa")
    assert "HTTP 200" in probe.check("http://192.168.1.208")
    for bad in ("https://example.com", "https://home.arpa.evil.com", "http://8.8.8.8"):
        try:
            probe.check(bad)
            raise AssertionError(bad)
        except ValueError:
            pass


class ScriptedLLM(BaseChatModel):
    """Returns the scripted replies in order; records what it saw and how tools were bound."""
    replies: list[AIMessage]
    seen: list[list[BaseMessage]] = []
    tool_choice: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, tool_choice=None, **kwargs):
        outer = self

        class Bound:
            def invoke(self, messages):
                outer.tool_choice.append(tool_choice)
                return outer.invoke(messages)
        return Bound()

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        reply = self.replies.pop(0) if self.replies else AIMessage("…")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def call(name: str, args: dict, id_: str) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": id_}])


@tool
def k8s_overview(cluster: str) -> str:
    """Cluster health."""
    return f"{cluster}: pod shop/api CrashLoopBackOff"


def test_graph_loop_memory_and_step_limit():
    llm = ScriptedLLM(replies=[
        call("k8s_overview", {"cluster": "staging"}, "1"),
        AIMessage("На staging падает shop/api."),
        AIMessage("Это тот же под, что и раньше."),
    ], seen=[], tool_choice=[])
    graph = build_graph(llm, [k8s_overview], lambda: "SYSTEM", max_steps=5, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t"}}
    out = graph.invoke({"messages": [("user", "что сломано?")], "steps": 0}, cfg)
    assert out["messages"][-1].content == "На staging падает shop/api."
    assert any(isinstance(m, ToolMessage) and "CrashLoopBackOff" in m.content for m in out["messages"])

    graph.invoke({"messages": [("user", "а это тот же?")], "steps": 0}, cfg)
    replay = llm.seen[-1]
    assert isinstance(replay[0], SystemMessage)
    # earlier turn replayed as text only: no tool call or tool result from it
    assert [type(m).__name__ for m in replay[1:]] == ["SystemMessage", "HumanMessage", "AIMessage", "HumanMessage"][1:]
    assert not any(isinstance(m, ToolMessage) for m in replay)

    # the step budget forces an answer without tools
    looping = ScriptedLLM(replies=[call("k8s_overview", {"cluster": "staging"}, str(i)) for i in range(3)]
                          + [AIMessage("Итог по собранным данным.")], seen=[], tool_choice=[])
    g = build_graph(looping, [k8s_overview], lambda: "S", max_steps=3)
    res = g.invoke({"messages": [("user", "q")], "steps": 0})
    assert res["messages"][-1].content == "Итог по собранным данным."
    assert looping.tool_choice[-1] == "none" and looping.tool_choice.count("none") == 1

    # a model that ignores tool_choice="none" still ends the loop
    stubborn = ScriptedLLM(replies=[call("k8s_overview", {"cluster": "staging"}, str(i)) for i in range(9)],
                           seen=[], tool_choice=[])
    res = build_graph(stubborn, [k8s_overview], lambda: "S", max_steps=2).invoke({"messages": [("user", "q")]})
    assert res["messages"][-1].content.startswith("Не уложился") and len(stubborn.seen) == 3


def test_context_window_keeps_last_turns():
    from langchain_core.messages import HumanMessage
    msgs = []
    for i in range(10):
        msgs += [HumanMessage(f"q{i}"), call("t", {}, str(i)), ToolMessage("r", tool_call_id=str(i)), AIMessage(f"a{i}")]
    msgs.append(HumanMessage("now"))
    window = context_window(msgs, history_turns=2)
    assert [m.content for m in window] == ["q8", "a8", "q9", "a9", "now"]


class FakeTelegram:
    def __init__(self):
        self.sent, self.edited, self.deleted = [], [], []

    def typing(self, chat_id):
        pass

    def send(self, chat_id, text):
        self.sent.append(text)
        return len(self.sent)

    def edit(self, chat_id, message_id, text):
        self.edited.append(text)

    def delete(self, chat_id, message_id):
        self.deleted.append(message_id)


def test_bot_progress_trace_and_commands(caplog):
    caplog.set_level("INFO", logger="ops_agent.bot")
    llm = ScriptedLLM(replies=[call("k8s_overview", {"cluster": "staging"}, "1"), AIMessage("Всё плохо.")],
                      seen=[], tool_choice=[])
    graph = build_graph(llm, [k8s_overview], lambda: "S", checkpointer=InMemorySaver())
    tg = FakeTelegram()
    bot = Bot(tg, graph, lambda: "no alerts firing", frozenset({1}))

    bot.process({"message": {"chat": {"id": 2}, "text": "hi"}})
    assert tg.sent == []  # unknown chat ignored

    bot.process({"message": {"chat": {"id": 1}, "text": "что сломано?"}})
    assert tg.sent[0].startswith("Смотрю:\n- k8s_overview(cluster=staging)")  # live progress
    assert tg.deleted == [1] and tg.sent[-1] == "Всё плохо."
    assert "question from 1: что сломано?" in caplog.text
    assert "with 1 tool calls" in caplog.text and caplog.text.rstrip().endswith("Всё плохо.")

    assert bot.handle(1, "/trace") == "Вызовы для последнего ответа:\n- k8s_overview(cluster=staging)"
    assert bot.handle(1, "/alerts") == "no alerts firing"
    first = bot._thread(1)
    bot.handle(1, "/reset")
    assert bot._thread(1) != first
