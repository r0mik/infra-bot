import json
from types import SimpleNamespace

import httpx

from cluster_bot.agent import Agent
from cluster_bot.main import Bot
from cluster_bot.monitoring import Monitoring
from cluster_bot.telegram import Telegram, TelegramError

ALERTS = [
    {
        "labels": {"alertname": "HostHighTemperature", "severity": "warning", "env": "staging", "node": "staging"},
        "annotations": {"summary": "CPU at 93 °C"},
        "startsAt": "2026-11-04T09:15:02.123456789Z",
        "status": {"state": "suppressed", "silencedBy": [], "inhibitedBy": []},
    },
    {
        "labels": {"alertname": "NodeDown", "severity": "critical", "env": "production", "node": "production"},
        "annotations": {},
        "startsAt": "2026-11-04T08:00:00Z",
        "status": {"state": "active", "silencedBy": [], "inhibitedBy": []},
    },
]


def prom_handler(request: httpx.Request) -> httpx.Response:
    path, q = request.url.path, request.url.params
    if path == "/api/v2/alerts":
        return httpx.Response(200, json=ALERTS)
    if path == "/api/v1/query":
        if q["query"] == "bad(":
            return httpx.Response(400, json={"status": "error", "errorType": "bad_data", "error": "parse error"})
        if q["query"] == "up[5m]":
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})
        result = [{"metric": {"__name__": "up", "job": "node", "node": n}, "value": [1, "1"]}
                  for n in ("production", "staging")]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": result}})
    if path == "/api/v1/query_range":
        start, step = float(q["start"]), int(q["step"])
        values = [[start + i * step, str(40 + i % 50)] for i in range(200)]
        result = [{"metric": {"node": "staging"}, "values": values}]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": result}})
    if path == "/api/v1/label/__name__/values":
        return httpx.Response(200, json={"status": "success", "data": ["node_load1", "up", "node_hwmon_temp_celsius"]})
    return httpx.Response(404, text="not found")


def monitoring() -> Monitoring:
    client = httpx.Client(transport=httpx.MockTransport(prom_handler))
    return Monitoring("http://prometheus:9090", "http://alertmanager:9093", "Asia/Dhaka", client=client)


def test_alerts_sorted_with_state_and_local_time():
    lines = monitoring().alerts().splitlines()
    assert lines[0].startswith("NodeDown (critical) since 04.11 14:00 env=production,node=production")
    assert "[suppressed]" in lines[1] and lines[1].endswith("— CPU at 93 °C")


def test_query_and_errors_come_back_as_text():
    m = monitoring()
    assert m.call("prometheus_query", json.dumps({"query": "up"})).splitlines() == [
        'up{job="node",node="production"} 1', 'up{job="node",node="staging"} 1']
    assert m.call("prometheus_query", json.dumps({"query": "bad("})) == "error: Prometheus: bad_data: parse error"
    assert "range vector" in m.call("prometheus_query", json.dumps({"query": "up[5m]"}))
    assert m.call("nope", "{}") == "error: unknown tool nope"


def test_query_range_summarises():
    out = monitoring().query_range("temp", hours=24)
    header, series, samples = out.splitlines()
    assert header.startswith("window ")
    assert 'node="staging"' in series and "min 40" in series and "max 89" in series
    assert len(samples.split(", ")) == 12
    week = monitoring().query_range("temp", hours=168).splitlines()[2]
    assert all(len(s.split("=")[0]) == len("04.11 14:00") for s in week.strip().split(", "))


def test_reasoning_effort_sent_only_when_set():
    llm = FakeLLM()
    Agent(llm, "m", monitoring(), reasoning_effort="none").answer([], "?")
    Agent(llm, "m", monitoring()).answer([], "?")
    assert llm.calls[0]["reasoning_effort"] == "none" and "reasoning_effort" not in llm.calls[-1]


def test_context_goes_into_system_prompt():
    llm = FakeLLM()
    Agent(llm, "m", monitoring(), context="- node1: the only server\n").answer([], "?")
    Agent(llm, "m", monitoring()).answer([], "?")
    first, last = llm.calls[0]["messages"][0]["content"], llm.calls[-1]["messages"][0]["content"]
    assert "- node1: the only server\n\nHow to answer:" in first
    assert "node1" not in last and "{context}" not in last


def test_find_metrics():
    assert monitoring().find_metrics("NODE").splitlines() == ["node_load1", "node_hwmon_temp_celsius"]


class FakeLLM:
    """Asks for one tool call, then answers with what the tool returned."""

    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool_results = [m["content"] for m in kwargs["messages"] if isinstance(m, dict) and m["role"] == "tool"]
        if tool_results:
            message = SimpleNamespace(content=f"Горит: {tool_results[0].splitlines()[0]}", tool_calls=None)
        else:
            call = SimpleNamespace(id="c1", function=SimpleNamespace(name="get_alerts", arguments="{}"))
            message = SimpleNamespace(
                content=None, tool_calls=[call],
                model_dump=lambda **_: {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            )
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def typing(self, chat_id):
        pass

    def send(self, chat_id, text):
        self.sent.append((chat_id, text))


def make_bot():
    m, llm, tg = monitoring(), FakeLLM(), FakeTelegram()
    return Bot(tg, Agent(llm, "test-model", m, "Asia/Dhaka"), m, frozenset({42})), llm, tg


def update(chat_id, text):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def test_question_goes_through_tool_and_keeps_history():
    bot, llm, tg = make_bot()
    bot.process(update(42, "что горит?"))
    assert tg.sent == [(42, "Горит: NodeDown (critical) since 04.11 14:00 env=production,node=production")]
    assert llm.calls[0]["tools"] and "Now is" in llm.calls[0]["messages"][0]["content"]
    bot.process(update(42, "а ещё?"))
    second = llm.calls[2]["messages"]
    assert [m["role"] for m in second[1:3]] == ["user", "assistant"]


def test_commands_skip_the_model_and_strangers_are_ignored():
    bot, llm, tg = make_bot()
    bot.process(update(42, "/alerts@ExampleBot"))
    bot.process(update(7, "привет"))
    assert len(tg.sent) == 1 and tg.sent[0][1].startswith("NodeDown")
    assert llm.calls == []


def test_telegram_errors_never_contain_the_token():
    def handler(request):
        return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})

    tg = Telegram("123:SECRET", client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        tg.send(1, "x")
    except TelegramError as e:
        assert "SECRET" not in str(e) and e.__cause__ is None
    else:
        raise AssertionError("expected TelegramError")
