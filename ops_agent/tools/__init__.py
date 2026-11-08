"""All agent tools. Every one of them is read-only; see each module's docstring for how that is enforced."""

import logging
import os
import shutil
import tempfile
from pathlib import Path

from langchain_core.tools import BaseTool

from ..config import Settings
from .http_probe import HttpProbe, http_tools
from .kubernetes import KubeCluster, Kubernetes, kubernetes_tools
from .monitoring import Monitoring, monitoring_tools
from .nodes import Node, Nodes, node_tools

log = logging.getLogger(__name__)


def _private_key_copy(src: Path) -> Path:
    """ssh refuses a key readable by its group, which is how a Secret mounted with fsGroup looks.
    Copy it to the (writable, memory-backed) /tmp with mode 0600."""
    fd, path = tempfile.mkstemp(prefix="ssh-key-")
    os.close(fd)
    shutil.copyfile(src, path)
    os.chmod(path, 0o600)
    return Path(path)


def build(settings: Settings) -> tuple[list[BaseTool], Monitoring]:
    monitoring = Monitoring(settings.prometheus_url, settings.alertmanager_url, settings.loki_url, settings.timezone)
    tools = monitoring_tools(monitoring)

    clusters = {}
    for name, server in settings.clusters.items():
        if server == "in-cluster":
            if "KUBERNETES_SERVICE_HOST" not in os.environ:
                log.warning("cluster %s disabled: not running in a pod", name)
                continue
            clusters[name] = KubeCluster.in_cluster(name, os.environ["KUBERNETES_SERVICE_HOST"],
                                                    os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
        else:
            d = settings.kube_secrets_dir / name
            if not (d / "token").exists():
                log.warning("cluster %s disabled: no token in %s", name, d)
                continue
            clusters[name] = KubeCluster(name, server, d / "token", d / "ca.crt")
    if clusters:
        tools += kubernetes_tools(Kubernetes(clusters))

    if settings.nodes and not settings.ssh_key_file.exists():
        log.warning("node_command disabled: no SSH key at %s", settings.ssh_key_file)
    elif settings.nodes:
        nodes = {n: Node(n, target) for n, target in settings.nodes.items()}
        tools += node_tools(Nodes(nodes, _private_key_copy(settings.ssh_key_file), settings.ssh_known_hosts))

    tools += http_tools(HttpProbe(settings.http_allowed_suffixes, settings.http_ca_file))
    return tools, monitoring
