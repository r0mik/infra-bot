"""The read-only guarantees: node command policy, RBAC manifest, GET-only Kubernetes client."""

import json
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from ops_agent.tools import node_policy as p
from ops_agent.tools.kubernetes import KubeCluster, Kubernetes
from ops_agent.tools.nodes import Node, Nodes

ROOT = Path(__file__).parent.parent


def ok(cmd: str) -> list[list[str]]:
    return p.validate(p.parse(cmd))


@pytest.mark.parametrize("cmd", [
    "uptime", "free -h", "df -h", "df -hT /var", "lsblk -f", "du -sh /var/lib", "du -h -d 1 /var",
    "cat /proc/loadavg", "ps aux --sort=-%mem", "top -bn1", "systemctl status k3s",
    "systemctl list-units --failed", "systemctl list-timers", "systemctl is-active docker.service",
    "journalctl -u k3s -p warning --since '1 hour ago' -n 50", "journalctl -k -b -1", "journalctl -b -u k3s",
    "journalctl -u containerd -g 'failed to pull'", "ip -br addr", "ip route get 1.1.1.1", "ip link show dev eth0",
    "ss -tlnp", "ping -c 3 192.168.1.1", "dig @192.168.1.208 gitea.example.home.arpa A +short",
    "docker ps", "docker stats --no-stream", "docker logs --tail 50 --since 30m adguard", "overview",
])
def test_allowed(cmd):
    assert ok(cmd)


@pytest.mark.parametrize("cmd", [
    "rm -rf /", "reboot", "systemctl restart k3s", "systemctl stop docker", "systemctl daemon-reload",
    "kubectl delete pod x", "k3s kubectl get secrets", "bash -c id", "sh", "python3 -c 1",
    "cat /etc/shadow", "cat /var/lib/rancher/k3s/server/token", "cat /proc/loadavg /etc/shadow",
    "df -h; rm -rf /", "uptime && reboot", "uptime | sh", "echo $(id)", "uptime `id`", "df > /etc/passwd",
    "journalctl --vacuum-time=1s", "journalctl --rotate", "journalctl -u k3s --output-fields=x", "journalctl -n 100000",
    "ip link set eth0 down", "ip addr add 1.2.3.4/24 dev eth0", "ip route del default", "ip route flush",
    "docker rm -f adguard", "docker exec adguard sh", "docker run alpine", "docker logs --follow adguard",
    "docker inspect adguard", "docker ps --format '{{json .}}'", "ss -K", "ping -c 1000 host", "ping -f host",
    "du --files0-from=/etc/shadow", "du -sh /var --exclude x", "df --output=source /", "top -b",
    "ps -ef e", "dig -f /etc/shadow", "dig x.y AXFR", "ping -c 1 -- -host", "systemctl status ../../x",
    "free -h\nreboot", "lsblk --raw /dev/sda", "",
])
def test_refused(cmd):
    with pytest.raises(p.PolicyError):
        ok(cmd)


def test_validated_argv_is_hardened():
    assert ok("journalctl -u k3s") == [["journalctl", "--no-pager", "-o", "short-iso", "-u", "k3s", "-n", "200"]]
    assert ok("ping -c 2 router")[0] == ["ping", "-c", "2", "-W", "2", "--", "router"]
    assert ok("docker logs adguard")[0][:3] == ["sudo", "-n", "/usr/bin/docker"]
    assert ok("du -sh /var")[0] == ["du", "-xsh", "/var"]  # -x: stay on one filesystem
    assert len(ok("overview")) == 4


def test_forced_command_entry_point(tmp_path):
    """The file as installed on the node: refuses through $SSH_ORIGINAL_COMMAND, runs allowed commands."""
    script = ROOT / "ops_agent/tools/node_policy.py"
    refused = subprocess.run(["python3", script], env={"SSH_ORIGINAL_COMMAND": "rm -rf /tmp/x"},
                             capture_output=True, text=True)
    assert refused.returncode == 126 and refused.stdout.startswith("refused:")
    allowed = subprocess.run(["python3", script], env={"SSH_ORIGINAL_COMMAND": "uname -a"},
                             capture_output=True, text=True)
    assert allowed.returncode == 0 and allowed.stdout.strip()


def test_node_tool_validates_before_ssh():
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=" 10:00 up 3 days\n", stderr="")

    nodes = Nodes({"staging": Node("staging", "ops-agent@192.168.1.208")}, Path("/k"), Path("/kh"), runner=runner)
    from ops_agent.tools.common import ToolInputError
    with pytest.raises(ToolInputError, match="refused"):
        nodes.run("staging", "systemctl restart k3s")
    assert calls == []  # never connected
    assert "up 3 days" in nodes.run("staging", "uptime")
    argv = calls[0]
    assert "StrictHostKeyChecking=yes" in argv and "BatchMode=yes" in argv
    assert argv[-2:] == ["--", "uptime"]


# ── RBAC ──────────────────────────────────────────────────────────────────────

def test_rbac_is_read_only():
    docs = list(yaml.safe_load_all((ROOT / "deploy/k8s/rbac.yaml").read_text()))
    role = next(d for d in docs if d["kind"] == "ClusterRole")
    for rule in role["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule
        assert "*" not in rule["resources"] and "*" not in rule["apiGroups"], rule
        for res in rule["resources"]:
            assert res not in {"secrets", "pods/exec", "pods/attach", "pods/portforward", "nodes/proxy",
                               "services/proxy", "serviceaccounts/token", "pods/ephemeralcontainers"}, res
    assert not any(d["kind"] in ("Role", "RoleBinding") for d in docs)
    binding = next(d for d in docs if d["kind"] == "ClusterRoleBinding")
    assert binding["roleRef"]["name"] == role["metadata"]["name"]


# example.yaml is public; production.yaml is a git-ignored local copy with real values, checked when present.
@pytest.mark.parametrize("manifest", [p for p in ("example.yaml", "production.yaml") if (ROOT / "deploy/k8s" / p).exists()])
def test_deployment_hardening(manifest):
    dep = yaml.safe_load((ROOT / "deploy/k8s" / manifest).read_text())
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]["securityContext"]
    assert c["readOnlyRootFilesystem"] and not c["allowPrivilegeEscalation"] and c["capabilities"]["drop"] == ["ALL"]
    assert spec["serviceAccountName"] == "ops-agent" and dep["spec"]["replicas"] == 1


# ── Kubernetes client ─────────────────────────────────────────────────────────

POD = {
    "metadata": {"name": "api-7d9f-abc", "namespace": "shop", "creationTimestamp": "2026-11-07T10:00:00Z"},
    "spec": {"nodeName": "staging", "containers": [{"name": "api", "image": "shop:1",
                                                    "env": [{"name": "DB_PASSWORD", "value": "hunter2"},
                                                            {"name": "MODE", "value": "prod"}]}]},
    "status": {"phase": "Running", "containerStatuses": [{
        "name": "api", "ready": False, "restartCount": 7,
        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
        "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-11-07T11:00:00Z"}}}]},
}
CM = {"metadata": {"name": "app", "namespace": "x", "managedFields": [{}]}, "data": {"config.yaml": "password: s3cret"}}


def kube(seen: list) -> Kubernetes:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, dict(req.url.params)))
        assert req.headers["authorization"] == "Bearer tok"
        path = req.url.path
        if path.endswith("/pods/api-7d9f-abc/log"):
            return httpx.Response(200, text="2026-11-07T11:00:00Z start\n2026-11-07T11:00:01Z ERROR out of memory\n")
        if path == "/api/v1/namespaces/shop/pods/api":
            return httpx.Response(404, json={"message": 'pods "api" not found'})
        if path.endswith("/pods/api-7d9f-abc"):
            return httpx.Response(200, json=POD)
        if path in ("/api/v1/namespaces/shop/pods", "/api/v1/pods"):
            return httpx.Response(200, json={"items": [POD]})
        if path == "/api/v1/namespaces/x/configmaps/app":
            return httpx.Response(200, json=CM)
        if path.endswith("/events"):
            return httpx.Response(200, json={"items": []})
        if path == "/api/v1/nodes":
            return httpx.Response(200, json={"items": [{"metadata": {"name": "staging", "labels": {
                "node-role.kubernetes.io/control-plane": "true"}}, "spec": {}, "status": {
                "conditions": [{"type": "Ready", "status": "True"}, {"type": "DiskPressure", "status": "True"}],
                "addresses": [{"type": "InternalIP", "address": "192.168.1.208"}],
                "nodeInfo": {"kubeletVersion": "v1.36.5+k3s1"}}}]})
        if "applications" in path:
            return httpx.Response(404, json={"message": "the server could not find the requested resource"})
        if path.startswith("/apis/apps/v1/"):
            return httpx.Response(200, json={"items": []})
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    token = Path(__file__).parent / "fixtures-token"
    token.write_text("tok\n")
    client = httpx.Client(base_url="https://k8s", transport=httpx.MockTransport(handler))
    return Kubernetes({"staging": KubeCluster("staging", "https://k8s", token, None, client=client)})


def test_kubernetes_only_gets_and_hides_values():
    seen = []
    k = kube(seen)
    from ops_agent.tools.common import ToolInputError
    with pytest.raises(ToolInputError, match="Secrets"):
        k.get("staging", "secrets", "x")
    with pytest.raises(ToolInputError, match="unknown cluster"):
        k.get("prod", "pods")

    line = k.get("staging", "pods", "shop")
    assert "0/1 CrashLoopBackOff restarts=7" in line and "OOMKilled exit 137" in line

    d = k.describe("staging", "pod", "api-7d9f-abc", "shop")
    assert "hunter2" not in d and "<hidden>" in d and "MODE" in d and "prod" in d and "Events:" in d
    cm = k.describe("staging", "cm", "app", "x")
    assert "s3cret" not in cm and "bytes hidden" in cm and "managedFields" not in cm

    logs = k.logs("staging", "shop", "api", grep="error")  # prefix → newest matching pod
    assert "(pod api-7d9f-abc)" in logs and "out of memory" in logs and "start" not in logs.split("\n", 2)[-1]

    overview = k.overview("staging")
    assert "DiskPressure=True" in overview and "problem pods (1)" in overview and "argocd" not in overview

    assert {m for m, _, _ in seen} == {"GET"}
    assert not hasattr(KubeCluster, "post") and not hasattr(KubeCluster, "delete")
    (Path(__file__).parent / "fixtures-token").unlink()
