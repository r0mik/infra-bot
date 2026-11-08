"""Formatting shared by all tools: every result goes straight into the model's context, so keep it short."""

import json
import logging
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("ops_agent.tools")

MAX_CHARS = 6000
MAX_LINES = 60


class ToolInputError(ValueError):
    """A bad argument from the model: returned to it as text so it can retry."""


def trim(lines: list[str], total: int | None = None, what: str = "lines") -> str:
    total = len(lines) if total is None else total
    shown = lines[:MAX_LINES]
    if total > len(shown):
        shown = [*shown, f"… {total - len(shown)} more {what} not shown, narrow the query"]
    text = "\n".join(shown) or "(empty result)"
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n… truncated"


def clip(text: str, limit: int = MAX_CHARS) -> str:
    """Keep the tail: for logs and command output the latest lines matter most."""
    text = text.strip() or "(no output)"
    return text if len(text) <= limit else "… (earlier output cut)\n" + text[-limit:]


def num(value) -> str:
    v = float(value)
    return str(int(v)) if v.is_integer() and abs(v) < 1e15 else f"{v:.4g}"


def age(ts: str | None, now: datetime | None = None) -> str:
    """Kubernetes timestamp → '5m', '3h', '12d'."""
    if not ts:
        return "-"
    then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    seconds = int(((now or datetime.now(timezone.utc)) - then).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{max(seconds, 0)}s"


def local_time(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%d.%m %H:%M")


def logged(fn):
    """Log each call with its full result (so a wrong answer can be traced to what the model saw) and
    turn expected failures into text the model can react to instead of an exception."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
        except ToolInputError as e:
            result = f"error: {e}"
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500]
            result = f"error: HTTP {e.response.status_code} from {e.request.url.host}: {body}"
        except httpx.HTTPError as e:
            result = f"error: {type(e).__name__}: {e}"
        log.info("tool %s(%s) ->\n%s", fn.__name__, json.dumps(kwargs, ensure_ascii=False, default=str), result)
        return result

    return wrapper
