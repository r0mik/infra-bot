"""Read-only tools over the Prometheus and Alertmanager HTTP APIs.

Every tool returns compact plain text: it goes straight into the model's context,
so results are trimmed to keep a question at a few thousand tokens.
"""

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

MAX_SERIES = 40
MAX_CHARS = 6000
RANGE_POINTS = 12

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_alerts",
            "description": "Alerts that are firing right now in Alertmanager (both clusters, including silenced and "
            "inhibited ones, marked as such).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prometheus_query",
            "description": "Instant PromQL query: the current value of each series.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "PromQL expression"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prometheus_query_range",
            "description": "PromQL over a past window. Per series returns min/avg/max (with the time of the max), "
            f"the last value and {RANGE_POINTS} evenly spaced samples. Data is kept for 30 days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "PromQL expression"},
                    "hours": {"type": "number", "description": "Window length ending now, in hours (max 720)"},
                    "end_hours_ago": {
                        "type": "number",
                        "description": "Shift the window into the past, e.g. 24 = window ends yesterday at this time. "
                        "Default 0.",
                    },
                },
                "required": ["query", "hours"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_metrics",
            "description": "Metric names that contain a substring, to discover what is collected.",
            "parameters": {
                "type": "object",
                "properties": {"contains": {"type": "string"}},
                "required": ["contains"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_targets",
            "description": "Prometheus scrape targets with their health and last error.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_alert_rules",
            "description": "All alert rules: name, state (inactive/pending/firing), expression and duration.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


def _num(value: str) -> str:
    v = float(value)
    return str(int(v)) if v.is_integer() and abs(v) < 1e15 else f"{v:.4g}"


def _labels(metric: dict) -> str:
    name = metric.get("__name__", "")
    rest = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{rest}}}" if rest or not name else name


def _trim(lines: list[str], total: int, what: str) -> str:
    if total > len(lines):
        lines.append(f"… {total - len(lines)} more {what} not shown, narrow the query")
    text = "\n".join(lines) or "(empty result)"
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n… truncated"


class Monitoring:
    def __init__(self, prometheus_url: str, alertmanager_url: str, timezone: str = "UTC",
                 client: httpx.Client | None = None):
        self.prometheus_url = prometheus_url.rstrip("/")
        self.alertmanager_url = alertmanager_url.rstrip("/")
        self.tz = ZoneInfo(timezone)
        self.http = client or httpx.Client(timeout=20)

    def _time(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%d.%m %H:%M")

    def _prom(self, path: str, **params) -> dict:
        r = self.http.get(f"{self.prometheus_url}{path}", params=params)
        body = r.json()
        if body.get("status") != "success":
            raise ValueError(f"Prometheus: {body.get('errorType')}: {body.get('error')}")
        return body["data"]

    def call(self, name: str, arguments: str) -> str:
        """Run a tool by name. Errors are returned as text so the model can fix its query."""
        try:
            args = json.loads(arguments or "{}")
            match name:
                case "get_alerts":
                    return self.alerts()
                case "prometheus_query":
                    return self.query(args["query"])
                case "prometheus_query_range":
                    return self.query_range(args["query"], args["hours"], args.get("end_hours_ago", 0))
                case "find_metrics":
                    return self.find_metrics(args["contains"])
                case "get_targets":
                    return self.targets()
                case "get_alert_rules":
                    return self.alert_rules()
            return f"error: unknown tool {name}"
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            return f"error: {e}"

    def alerts(self) -> str:
        r = self.http.get(f"{self.alertmanager_url}/api/v2/alerts", params={"active": "true"})
        r.raise_for_status()
        alerts = sorted(r.json(), key=lambda a: a["startsAt"])
        lines = []
        for a in alerts:
            labels = dict(a["labels"])
            name = labels.pop("alertname", "?")
            severity = labels.pop("severity", "")
            state = a["status"]["state"]
            mark = "" if state == "active" else f" [{state}]"
            since = self._time(datetime.fromisoformat(a["startsAt"]).timestamp())
            where = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
            summary = a.get("annotations", {}).get("summary", "")
            lines.append(f"{name} ({severity}){mark} since {since} {where} — {summary}".rstrip(" —"))
        return "\n".join(lines) if lines else "no alerts firing"

    def query(self, query: str) -> str:
        data = self._prom("/api/v1/query", query=query)
        result = data["result"]
        if data["resultType"] in ("scalar", "string"):
            return _num(result[1]) if data["resultType"] == "scalar" else result[1]
        if data["resultType"] == "matrix":
            return "error: query returns a range vector; wrap it in a function or use prometheus_query_range"
        lines = [f"{_labels(s['metric'])} {_num(s['value'][1])}" for s in result[:MAX_SERIES]]
        return _trim(lines, len(result), "series")

    def query_range(self, query: str, hours: float, end_hours_ago: float = 0) -> str:
        hours = min(max(float(hours), 0.1), 720)
        end = time.time() - float(end_hours_ago) * 3600
        start = end - hours * 3600
        # Dense enough that min/max catch short peaks (24 h → 1 min); only the summary reaches the model.
        step = max(15, int(hours * 3600 / 1500))
        data = self._prom("/api/v1/query_range", query=query, start=start, end=end, step=step)
        result = data["result"]
        lines = [f"window {self._time(start)} – {self._time(end)}, step {step}s"]
        for s in result[:MAX_SERIES]:
            points = [(float(t), float(v)) for t, v in s["values"]]
            values = [v for _, v in points]
            t_max, v_max = max(points, key=lambda p: p[1])
            every = max(1, len(points) // RANGE_POINTS)
            cut = 0 if hours > 24 else 6  # keep the date only when the window spans days
            samples = ", ".join(f"{self._time(t)[cut:]}={_num(v)}" for t, v in points[::every][:RANGE_POINTS])
            lines.append(
                f"{_labels(s['metric'])}: min {_num(min(values))} avg {_num(sum(values) / len(values))} "
                f"max {_num(v_max)} at {self._time(t_max)} last {_num(values[-1])}\n  {samples}"
            )
        return _trim(lines, len(result) + 1, "series")

    def find_metrics(self, contains: str) -> str:
        names = [n for n in self._prom("/api/v1/label/__name__/values") if contains.lower() in n.lower()]
        return _trim(names[:100], len(names), "names")

    def targets(self) -> str:
        targets = self._prom("/api/v1/targets", state="active")["activeTargets"]
        lines = []
        for t in sorted(targets, key=lambda t: (t["health"] == "up", t["labels"].get("job", ""))):
            labels = t["labels"]
            err = f" error: {t['lastError']}" if t.get("lastError") else ""
            lines.append(f"{t['health']} job={labels.get('job')} instance={labels.get('instance')} "
                         f"env={labels.get('env', '')} node={labels.get('node', '')}{err}")
        return _trim(lines[:MAX_SERIES * 2], len(lines), "targets")

    def alert_rules(self) -> str:
        lines = []
        for group in self._prom("/api/v1/rules", type="alert")["groups"]:
            for rule in group["rules"]:
                lines.append(f"{rule['name']} [{rule['state']}] for {int(rule.get('duration', 0))}s: {rule['query']}")
        return _trim(lines, len(lines), "rules")
