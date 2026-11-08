"""Read-only tools over Prometheus, Alertmanager and Loki.

Only GET requests to query endpoints: these APIs can't change anything through them
(silences, deletes and admin endpoints are POST/DELETE and are never called).
"""

import re
import time
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
from langchain_core.tools import BaseTool, tool

from .common import ToolInputError, local_time, logged, num, trim, clip

MAX_SERIES = 40
RANGE_POINTS = 12
LOKI_WHERE = ("cluster", "node", "namespace", "pod", "container", "unit", "service_name")
_VOLATILE = re.compile(r"\d+")


def _shape(line: str) -> str:
    """A log line without its numbers (timestamps, ports, ids), so repeats of one message group together."""
    return _VOLATILE.sub("#", line)[:300]


def _labels(metric: dict) -> str:
    name = metric.get("__name__", "")
    rest = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{rest}}}" if rest or not name else name


class Monitoring:
    def __init__(self, prometheus_url: str, alertmanager_url: str, loki_url: str, timezone: str = "UTC",
                 client: httpx.Client | None = None):
        self.prometheus_url = prometheus_url.rstrip("/")
        self.alertmanager_url = alertmanager_url.rstrip("/")
        self.loki_url = loki_url.rstrip("/")
        self.tz = ZoneInfo(timezone)
        self.http = client or httpx.Client(timeout=20)

    def _t(self, ts: float) -> str:
        return local_time(ts, self.tz)

    def _prom(self, path: str, **params) -> dict:
        body = self.http.get(f"{self.prometheus_url}{path}", params=params).json()
        if body.get("status") != "success":
            raise ToolInputError(f"Prometheus: {body.get('errorType')}: {body.get('error')}")
        return body["data"]

    # ── Alertmanager ──────────────────────────────────────────────────────────
    def alerts(self) -> str:
        r = self.http.get(f"{self.alertmanager_url}/api/v2/alerts", params={"active": "true"})
        r.raise_for_status()
        lines = []
        for a in sorted(r.json(), key=lambda a: a["startsAt"]):
            labels = dict(a["labels"])
            name = labels.pop("alertname", "?")
            severity = labels.pop("severity", "")
            state = a["status"]["state"]
            mark = "" if state == "active" else f" [{state}]"
            since = self._t(datetime.fromisoformat(a["startsAt"].replace("Z", "+00:00")).timestamp())
            where = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
            summary = a.get("annotations", {}).get("summary", "")
            lines.append(f"{name} ({severity}){mark} since {since} {where} — {summary}".rstrip(" —"))
        return "\n".join(lines) if lines else "no alerts firing"

    # ── Prometheus ────────────────────────────────────────────────────────────
    def query(self, query: str) -> str:
        data = self._prom("/api/v1/query", query=query)
        result = data["result"]
        if data["resultType"] == "scalar":
            return num(result[1])
        if data["resultType"] == "string":
            return result[1]
        if data["resultType"] == "matrix":
            raise ToolInputError("query returns a range vector; wrap it in a function or use prometheus_query_range")
        lines = [f"{_labels(s['metric'])} {num(s['value'][1])}" for s in result[:MAX_SERIES]]
        return trim(lines, len(result), "series")

    def query_range(self, query: str, hours: float, end_hours_ago: float = 0) -> str:
        hours = min(max(float(hours), 0.1), 720)
        end = time.time() - float(end_hours_ago) * 3600
        start = end - hours * 3600
        step = max(15, int(hours * 3600 / 1500))  # dense enough that min/max catch short peaks
        result = self._prom("/api/v1/query_range", query=query, start=start, end=end, step=step)["result"]
        lines = [f"window {self._t(start)} – {self._t(end)}, step {step}s"]
        for s in result[:MAX_SERIES]:
            points = [(float(t), float(v)) for t, v in s["values"]]
            values = [v for _, v in points]
            t_max, v_max = max(points, key=lambda p: p[1])
            every = max(1, len(points) // RANGE_POINTS)
            cut = 0 if hours > 24 else 6  # keep the date only when the window spans days
            samples = ", ".join(f"{self._t(t)[cut:]}={num(v)}" for t, v in points[::every][:RANGE_POINTS])
            lines.append(f"{_labels(s['metric'])}: min {num(min(values))} avg {num(sum(values) / len(values))} "
                         f"max {num(v_max)} at {self._t(t_max)} last {num(values[-1])}\n  {samples}")
        return trim(lines, len(result) + 1, "series")

    def find_metrics(self, contains: str) -> str:
        names = [n for n in self._prom("/api/v1/label/__name__/values") if contains.lower() in n.lower()]
        return trim(names[:100], len(names), "names")

    def targets(self) -> str:
        targets = self._prom("/api/v1/targets", state="active")["activeTargets"]
        lines = []
        for t in sorted(targets, key=lambda t: (t["health"] == "up", t["labels"].get("job", ""))):
            labels = t["labels"]
            err = f" error: {t['lastError']}" if t.get("lastError") else ""
            lines.append(f"{t['health']} job={labels.get('job')} instance={labels.get('instance')} "
                         f"env={labels.get('env', '')} node={labels.get('node', '')}{err}")
        return trim(lines, len(lines), "targets")

    def alert_rules(self) -> str:
        lines = [f"{r['name']} [{r['state']}] for {int(r.get('duration', 0))}s: {r['query']}"
                 for g in self._prom("/api/v1/rules", type="alert")["groups"] for r in g["rules"]]
        return trim(lines, len(lines), "rules")

    # ── Loki ──────────────────────────────────────────────────────────────────
    def _loki(self, path: str, **params) -> dict:
        r = self.http.get(f"{self.loki_url}{path}", params=params)
        if r.status_code == 400:
            raise ToolInputError(f"Loki: {r.text.strip()[:300]}")
        r.raise_for_status()
        return r.json()["data"]

    def logs(self, logql: str, minutes: float = 60, limit: int = 100, end_minutes_ago: float = 0) -> str:
        limit = min(max(int(limit), 1), 300)
        end = time.time() - float(end_minutes_ago) * 60
        start = end - min(max(float(minutes), 1), 7 * 24 * 60) * 60
        # Fetch many more lines than we show: one noisy source repeating the same error must not use up the
        # whole budget and hide the others. Repeats are collapsed below.
        data = self._loki("/loki/api/v1/query_range", query=logql, start=int(start * 1e9), end=int(end * 1e9),
                          limit=min(limit * 20, 5000), direction="backward")
        if data["resultType"] == "matrix":  # a metric query such as count_over_time
            lines = [f"{_labels(s['metric'])}: last {num(s['values'][-1][1])} max {num(max(float(v) for _, v in s['values']))}"
                     for s in data["result"]]
            return trim(lines, len(lines), "series")
        groups: dict[tuple[str, str], dict] = {}
        fetched = 0
        for stream in data["result"]:
            labels = stream["stream"]
            where = "/".join(dict.fromkeys(labels[k] for k in LOKI_WHERE if labels.get(k)))
            for ts, line in stream["values"]:
                fetched += 1
                ts = int(ts)
                g = groups.setdefault((where, _shape(line)), {"first": ts, "last": ts, "n": 0, "line": line})
                g["n"] += 1
                if ts >= g["last"]:
                    g["last"], g["line"] = ts, line  # show the newest example
                g["first"] = min(g["first"], ts)
        if not groups:
            return "no log lines match"
        shown = sorted(groups.items(), key=lambda kv: kv[1]["last"])[-limit:]
        def t(ns: int) -> str:
            return f"{datetime.fromtimestamp(ns / 1e9, self.tz):%d.%m %H:%M:%S}"

        lines = []
        for (where, _), g in shown:
            when = t(g["last"]) if g["n"] == 1 else f"x{g['n']} {t(g['first'])}…{t(g['last'])[6:]}"
            lines.append(f"{when} {where} {g['line'][:400]}")
        header = (f"{fetched} lines{' (fetch limit reached, there may be more)' if fetched >= min(limit * 20, 5000) else ''}"
                  f", {len(groups)} distinct messages (repeats collapsed as xN first…last), "
                  f"showing {len(shown)}, newest last")
        return clip(header + "\n" + "\n".join(lines))

    def log_labels(self, label: str | None = None) -> str:
        start = int((time.time() - 6 * 3600) * 1e9)
        if label:
            values = self._loki(f"/loki/api/v1/label/{label}/values", start=start)
            return trim(sorted(values or []), what="values")
        return ", ".join(sorted(self._loki("/loki/api/v1/labels", start=start) or []))


def monitoring_tools(m: Monitoring) -> list[BaseTool]:
    @tool
    @logged
    def get_alerts() -> str:
        """Alerts firing right now in Alertmanager, both clusters, including silenced or inhibited ones (marked)."""
        return m.alerts()

    @tool
    @logged
    def prometheus_query(query: Annotated[str, "PromQL expression"]) -> str:
        """Instant PromQL query: the current value of each series."""
        return m.query(query)

    @tool
    @logged
    def prometheus_query_range(
        query: Annotated[str, "PromQL expression"],
        hours: Annotated[float, "window length ending now, in hours (max 720)"],
        end_hours_ago: Annotated[float, "shift the window into the past, e.g. 24 = ends yesterday at this time"] = 0,
    ) -> str:
        """PromQL over a past window. Per series: min/avg/max (with time of max), last value and 12 evenly spaced
        samples. Data is kept for 30 days."""
        return m.query_range(query, hours, end_hours_ago)

    @tool
    @logged
    def find_metrics(contains: Annotated[str, "substring of the metric name"]) -> str:
        """Metric names containing a substring, to discover what is collected."""
        return m.find_metrics(contains)

    @tool
    @logged
    def get_scrape_targets() -> str:
        """Prometheus scrape targets with health and last scrape error, unhealthy first."""
        return m.targets()

    @tool
    @logged
    def get_alert_rules() -> str:
        """All alert rules: name, state (inactive/pending/firing), duration and PromQL expression."""
        return m.alert_rules()

    @tool
    @logged
    def loki_query(
        logql: Annotated[str, 'LogQL, e.g. {cluster="production", namespace="gitea"} |= "error"'],
        minutes: Annotated[float, "window length ending now, in minutes (max 10080 = 7 days)"] = 60,
        limit: Annotated[int, "max distinct messages to show, 1..300"] = 100,
        end_minutes_ago: Annotated[float, "shift the window into the past"] = 0,
    ) -> str:
        """Search logs in Loki (infrastructure pods, systemd journal and Docker containers of both nodes, 30 days).
        Labels: cluster, env, node, job (pods|journal|docker); pods add namespace, pod, container; the journal adds
        unit, level. Identical messages (numbers ignored) are collapsed into one line with a count, so the result
        shows every distinct problem, newest last. To see which sources are noisy, use a metric query, e.g.
        sum by (cluster, namespace, container, unit) (count_over_time({job=~".+"} |~ "(?i)error" [1h]))."""
        return m.logs(logql, minutes, limit, end_minutes_ago)

    @tool
    @logged
    def loki_labels(label: Annotated[str | None, "label name to list values of; omit to list label names"] = None) -> str:
        """Label names in Loki, or the values of one label (last 6 hours), to build a LogQL selector."""
        return m.log_labels(label)

    return [get_alerts, prometheus_query, prometheus_query_range, find_metrics, get_scrape_targets,
            get_alert_rules, loki_query, loki_labels]
