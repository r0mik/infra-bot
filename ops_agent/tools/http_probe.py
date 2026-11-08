"""GET/HEAD probe of homelab web endpoints, seen from inside the cluster.

Limited to LAN hosts (allowlisted suffixes and private IPs): it is for "does gitea answer?",
not a way to make the agent fetch arbitrary URLs.
"""

import ipaddress
import ssl
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from langchain_core.tools import BaseTool, tool

from .common import ToolInputError, logged


class HttpProbe:
    def __init__(self, allowed_suffixes: tuple[str, ...], ca_file: Path | None = None, transport=None):
        self.allowed_suffixes = tuple(s.lower().lstrip("*") for s in allowed_suffixes)
        verify: ssl.SSLContext | bool = True
        if ca_file and ca_file.exists():
            verify = ssl.create_default_context()
            verify.load_verify_locations(cafile=str(ca_file))  # Caddy's internal CA on top of the system ones
        self.http = httpx.Client(timeout=10, verify=verify, follow_redirects=False, transport=transport)

    def allowed(self, host: str) -> bool:
        try:
            return ipaddress.ip_address(host).is_private
        except ValueError:
            return host.lower().endswith(self.allowed_suffixes) or host.lower() in {s.lstrip(".") for s in self.allowed_suffixes}

    def check(self, url: str, method: str = "GET") -> str:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ToolInputError("need an http(s) URL")
        if not self.allowed(parts.hostname):
            raise ToolInputError(f"{parts.hostname} is outside the homelab; allowed: {', '.join(self.allowed_suffixes)}, private IPs")
        method = method.upper()
        if method not in ("GET", "HEAD"):
            raise ToolInputError("only GET or HEAD")
        start = time.monotonic()
        try:
            r = self.http.request(method, parts.geturl())
        except httpx.ConnectError as e:
            return f"{parts.geturl()}: connection failed: {e}"
        except httpx.TimeoutException:
            return f"{parts.geturl()}: timed out after 10s"
        ms = (time.monotonic() - start) * 1000
        where = f" → {r.headers['location']}" if r.is_redirect and "location" in r.headers else ""
        body = "" if method == "HEAD" else " body: " + " ".join(r.text[:300].split())
        server = r.headers.get("server", "")
        return f"{parts.geturl()}: HTTP {r.status_code} in {ms:.0f} ms{where} server={server}{body}"


def http_tools(probe: HttpProbe) -> list[BaseTool]:
    @tool
    @logged
    def http_check(
        url: Annotated[str, "e.g. https://git.example.home.arpa or http://whoami.staging.home.arpa"],
        method: Annotated[str, "GET or HEAD"] = "GET",
    ) -> str:
        """Request a homelab URL from inside the production cluster: status code, latency, redirect target, TLS or
        connection error, the start of the body. Only *.home.arpa and private IPs."""
        return probe.check(url, method)

    return [http_check]
