"""Diagnostic commands on the nodes over SSH.

The agent checks the command against node_policy before connecting; the node checks it again,
because the key is bound to `command="/usr/local/bin/ops-agent-ro"` (the same node_policy file)
with `restrict` (no pty, no forwarding) and belongs to an unprivileged user.
"""

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from langchain_core.tools import BaseTool, tool

from . import node_policy
from .common import ToolInputError, clip, logged

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Node:
    name: str
    target: str  # user@host


class Nodes:
    def __init__(self, nodes: dict[str, Node], key_file: Path, known_hosts: Path, timeout: int = 40, runner=None):
        self.nodes = nodes
        self.key_file = key_file
        self.known_hosts = known_hosts
        self.timeout = timeout
        self.runner = runner or subprocess.run

    def run(self, node: str, command: str) -> str:
        if node not in self.nodes:
            raise ToolInputError(f"unknown node {node!r}; configured: {', '.join(self.nodes)}")
        try:
            argv = node_policy.parse(command)
            node_policy.validate(argv)
        except node_policy.PolicyError as e:
            raise ToolInputError(f"refused: {e}") from None
        ssh = [
            "ssh", "-T", "-i", str(self.key_file),
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=10", "-o", "LogLevel=ERROR",
            self.nodes[node].target, "--", shlex.join(argv),
        ]
        try:
            p = self.runner(ssh, capture_output=True, text=True, timeout=self.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return f"error: no answer from {node} in {self.timeout}s"
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 255:
            return f"error: SSH to {node} failed: {out.strip()[:300]}"
        return clip(out)


def node_tools(nodes: Nodes) -> list[BaseTool]:
    names = ", ".join(nodes.nodes)

    @tool(description=(
        "Run one read-only diagnostic command on a node (host OS, outside Kubernetes) over SSH, as an "
        "unprivileged user. No shell: pipes, redirects, ; and $ are refused. Use it for host-level questions: "
        "systemd units (k3s, docker, containerd), the journal, disks, memory, processes, network, DNS, Docker "
        "containers running outside Kubernetes (AdGuard, node-exporter). Allowed commands:\n" + node_policy.HELP
    ))
    @logged
    def node_command(
        node: Annotated[str, f"node name: {names}"],
        command: Annotated[str, "the command line, e.g. `journalctl -u k3s -p warning --since '1 hour ago'`"],
    ) -> str:
        return nodes.run(node, command)

    return [node_command]
