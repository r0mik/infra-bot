"""Read-only Kubernetes tools for several clusters.

Read-only twice over:
1. RBAC: the agent's ServiceAccount has a ClusterRole with only get/list/watch, without Secrets,
   pods/exec, pods/attach, pods/portforward or nodes/proxy (deploy/k8s/rbac.yaml).
2. Code: KubeCluster has a single request method and it can only send GET. Secrets are refused
   before a request is made, ConfigMap values are hidden (keys and sizes only).
"""

import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import httpx
import yaml
from langchain_core.tools import BaseTool, tool

from .common import MAX_CHARS, ToolInputError, age, clip, logged, trim

IN_CLUSTER_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


@dataclass(frozen=True)
class Kind:
    plural: str
    api: str  # "v1" for the core group, otherwise "group/version"
    namespaced: bool = True
    aliases: tuple[str, ...] = ()

    def path(self, namespace: str | None = None, name: str | None = None) -> str:
        base = "/api/v1" if self.api == "v1" else f"/apis/{self.api}"
        ns = f"/namespaces/{namespace}" if self.namespaced and namespace else ""
        return f"{base}{ns}/{self.plural}" + (f"/{name}" if name else "")


KINDS = [
    Kind("pods", "v1", aliases=("pod", "po")),
    Kind("services", "v1", aliases=("service", "svc")),
    Kind("configmaps", "v1", aliases=("configmap", "cm")),
    Kind("events", "v1", aliases=("event", "ev")),
    Kind("persistentvolumeclaims", "v1", aliases=("pvc",)),
    Kind("serviceaccounts", "v1", aliases=("sa",)),
    Kind("endpoints", "v1", aliases=("ep",)),
    Kind("nodes", "v1", namespaced=False, aliases=("node", "no")),
    Kind("namespaces", "v1", namespaced=False, aliases=("namespace", "ns")),
    Kind("persistentvolumes", "v1", namespaced=False, aliases=("pv",)),
    Kind("deployments", "apps/v1", aliases=("deployment", "deploy")),
    Kind("statefulsets", "apps/v1", aliases=("statefulset", "sts")),
    Kind("daemonsets", "apps/v1", aliases=("daemonset", "ds")),
    Kind("replicasets", "apps/v1", aliases=("replicaset", "rs")),
    Kind("jobs", "batch/v1", aliases=("job",)),
    Kind("cronjobs", "batch/v1", aliases=("cronjob", "cj")),
    Kind("ingresses", "networking.k8s.io/v1", aliases=("ingress", "ing")),
    Kind("networkpolicies", "networking.k8s.io/v1", aliases=("networkpolicy", "netpol")),
    Kind("gateways", "gateway.networking.k8s.io/v1", aliases=("gateway", "gtw")),
    Kind("httproutes", "gateway.networking.k8s.io/v1", aliases=("httproute", "route")),
    Kind("storageclasses", "storage.k8s.io/v1", namespaced=False, aliases=("storageclass", "sc")),
    Kind("horizontalpodautoscalers", "autoscaling/v2", aliases=("hpa",)),
    Kind("poddisruptionbudgets", "policy/v1", aliases=("pdb",)),
    Kind("applications", "argoproj.io/v1alpha1", aliases=("application", "app", "argocd")),
    Kind("sealedsecrets", "bitnami.com/v1alpha1", aliases=("sealedsecret",)),
]
_BY_NAME = {n: k for k in KINDS for n in (k.plural, *k.aliases)}
FORBIDDEN_KINDS = {"secret", "secrets"}


def kind(name: str) -> Kind:
    key = name.strip().lower()
    if key in FORBIDDEN_KINDS:
        raise ToolInputError("Secrets are not readable by this agent")
    if key not in _BY_NAME:
        raise ToolInputError(f"unknown kind {name!r}; known: {', '.join(k.plural for k in KINDS)}")
    return _BY_NAME[key]


class KubeCluster:
    """A cluster reached with a ServiceAccount bearer token. Only GET exists here on purpose."""

    def __init__(self, name: str, server: str, token_file: Path, ca_file: Path | None,
                 client: httpx.Client | None = None):
        self.name = name
        self.token_file = token_file
        if client is None:
            verify: ssl.SSLContext | bool = True
            if ca_file and ca_file.exists():
                verify = ssl.create_default_context(cafile=str(ca_file))
            client = httpx.Client(base_url=server, verify=verify, timeout=20)
        self._http = client

    @classmethod
    def in_cluster(cls, name: str, host: str, port: str) -> "KubeCluster":
        return cls(name, f"https://{host}:{port}", IN_CLUSTER_DIR / "token", IN_CLUSTER_DIR / "ca.crt")

    def get(self, path: str, **params) -> httpx.Response:
        # Projected tokens rotate, so read the file on every call.
        token = self.token_file.read_text().strip()
        params = {k: v for k, v in params.items() if v is not None}
        r = self._http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code in (400, 403, 404):
            try:
                message = r.json().get("message", r.text)
            except ValueError:
                message = r.text
            prefix = {400: "bad request", 403: "forbidden by RBAC (read-only agent)", 404: "not found"}[r.status_code]
            raise ToolInputError(f"{self.name}: {prefix}: {message[:400]}")
        r.raise_for_status()
        return r

    def json(self, path: str, **params) -> dict:
        return self.get(path, **params).json()


# ── one-line summaries per kind ──────────────────────────────────────────────

def pod_status(pod: dict) -> tuple[str, str, int]:
    """(status like kubectl's STATUS column, ready "1/2", restarts)."""
    status = pod.get("status", {})
    statuses = status.get("containerStatuses", []) or []
    phase = status.get("reason") or status.get("phase", "?")
    for c in status.get("initContainerStatuses", []) or []:
        st = c.get("state", {})
        if "waiting" in st or ("terminated" in st and st["terminated"].get("exitCode")):
            phase = "Init:" + (st.get("waiting") or st.get("terminated", {})).get("reason", "?")
            break
    else:
        for c in statuses:
            st = c.get("state", {})
            if "waiting" in st and st["waiting"].get("reason"):
                phase = st["waiting"]["reason"]
            elif "terminated" in st and st["terminated"].get("reason") and phase != "Succeeded":
                phase = st["terminated"]["reason"]
    if pod["metadata"].get("deletionTimestamp"):
        phase = "Terminating"
    ready = f"{sum(c.get('ready', False) for c in statuses)}/{len(pod.get('spec', {}).get('containers', []))}"
    return phase, ready, sum(c.get("restartCount", 0) for c in statuses)


def _conditions(obj: dict, only_bad: bool = True) -> str:
    out = []
    for c in obj.get("status", {}).get("conditions", []) or []:
        bad = (c.get("status") != "True") if c["type"] in ("Ready", "Available", "Accepted", "Programmed",
                                                           "Progressing", "Complete") else c.get("status") == "True"
        if bad or not only_bad:
            out.append(f"{c['type']}={c.get('status')}" + (f"({c.get('reason')})" if c.get("reason") else ""))
    return " ".join(out)


def summarize(k: Kind, obj: dict) -> str:
    meta = obj.get("metadata", {})
    name = f"{meta['namespace']}/{meta['name']}" if meta.get("namespace") else meta.get("name", "?")
    spec, status = obj.get("spec", {}) or {}, obj.get("status", {}) or {}
    created = age(meta.get("creationTimestamp"))
    match k.plural:
        case "pods":
            phase, ready, restarts = pod_status(obj)
            last = ""
            for c in status.get("containerStatuses", []) or []:
                term = c.get("lastState", {}).get("terminated")
                if term:
                    last = f" (last: {term.get('reason')} exit {term.get('exitCode')} {age(term.get('finishedAt'))} ago)"
            return f"{name} {ready} {phase} restarts={restarts}{last} age={created} node={spec.get('nodeName', '-')}"
        case "deployments" | "statefulsets":
            want = spec.get("replicas", 1)
            images = ",".join(c["image"] for c in spec.get("template", {}).get("spec", {}).get("containers", []))
            return (f"{name} ready={status.get('readyReplicas', 0)}/{want} updated={status.get('updatedReplicas', 0)} "
                    f"available={status.get('availableReplicas', 0)} age={created} image={images} {_conditions(obj)}").rstrip()
        case "daemonsets":
            return (f"{name} desired={status.get('desiredNumberScheduled', 0)} ready={status.get('numberReady', 0)} "
                    f"available={status.get('numberAvailable', 0)} age={created}")
        case "replicasets":
            return f"{name} ready={status.get('readyReplicas', 0)}/{spec.get('replicas', 0)} age={created}"
        case "nodes":
            ready = next((c["status"] for c in status.get("conditions", []) if c["type"] == "Ready"), "?")
            roles = ",".join(l.split("/", 1)[1] for l in meta.get("labels", {}) if l.startswith("node-role.kubernetes.io/"))
            ip = next((a["address"] for a in status.get("addresses", []) if a["type"] == "InternalIP"), "-")
            info = status.get("nodeInfo", {})
            cordon = " SchedulingDisabled" if spec.get("unschedulable") else ""
            return (f"{name} Ready={ready}{cordon} roles={roles or '-'} ip={ip} version={info.get('kubeletVersion')} "
                    f"age={created} {_conditions(obj)}").rstrip()
        case "services":
            ports = ",".join(f"{p.get('port')}{'→' + str(p['nodePort']) if p.get('nodePort') else ''}/{p.get('protocol', 'TCP')}"
                             for p in spec.get("ports", []))
            lb = ",".join(i.get("ip", i.get("hostname", "")) for i in status.get("loadBalancer", {}).get("ingress", []) or [])
            return f"{name} {spec.get('type')} {spec.get('clusterIP')} {ports}" + (f" lb={lb}" if lb else "") + f" age={created}"
        case "persistentvolumeclaims":
            cap = status.get("capacity", {}).get("storage", spec.get("resources", {}).get("requests", {}).get("storage"))
            return f"{name} {status.get('phase')} {cap} class={spec.get('storageClassName')} volume={spec.get('volumeName')} age={created}"
        case "persistentvolumes":
            claim = spec.get("claimRef", {})
            return (f"{name} {status.get('phase')} {spec.get('capacity', {}).get('storage')} "
                    f"claim={claim.get('namespace')}/{claim.get('name')} reclaim={spec.get('persistentVolumeReclaimPolicy')}")
        case "jobs":
            return (f"{name} succeeded={status.get('succeeded', 0)} failed={status.get('failed', 0)} "
                    f"active={status.get('active', 0)} age={created} {_conditions(obj, only_bad=False)}").rstrip()
        case "cronjobs":
            return (f"{name} schedule='{spec.get('schedule')}' suspend={spec.get('suspend', False)} "
                    f"last={age(status.get('lastScheduleTime'))} ago lastSuccess={age(status.get('lastSuccessfulTime'))} ago")
        case "events":
            return event_line(obj)
        case "configmaps":
            keys = ", ".join(f"{key} ({len(v)} B)" for key, v in (obj.get("data") or {}).items())
            return f"{name} keys: {keys or '-'} age={created}"
        case "applications":
            sync, health = status.get("sync", {}), status.get("health", {})
            op = status.get("operationState", {})
            msg = (op.get("message") or "")[:150] if op.get("phase") not in (None, "Succeeded") else ""
            return (f"{name} sync={sync.get('status')} health={health.get('status')} "
                    f"rev={str(sync.get('revision', ''))[:8]} {msg}").rstrip()
        case "httproutes":
            parents = status.get("parents", []) or []
            ok = all(c.get("status") == "True" for p in parents for c in p.get("conditions", []) if c["type"] in ("Accepted", "ResolvedRefs"))
            return f"{name} hosts={','.join(spec.get('hostnames', []))} accepted={ok if parents else 'no status'} age={created}"
        case "namespaces":
            return f"{name} {status.get('phase')} age={created}"
    return f"{name} age={created} {_conditions(obj)}".rstrip()


def _event_time(e: dict) -> str:
    return e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp") or e["metadata"].get("creationTimestamp") or ""


def event_line(e: dict) -> str:
    obj = e.get("involvedObject", {})
    count = f" x{e['count']}" if e.get("count", 1) > 1 else ""
    ns = f"{obj.get('namespace')}/" if obj.get("namespace") else ""
    return (f"{age(_event_time(e))} ago {e.get('type')} {e.get('reason')} {obj.get('kind')} {ns}{obj.get('name')}: "
            f"{(e.get('message') or '').strip()[:300]}{count}")


def _redact(k: Kind, obj: dict) -> dict:
    meta = obj.get("metadata", {})
    meta.pop("managedFields", None)
    (meta.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
    if k.plural == "configmaps":
        obj["data"] = {key: f"<{len(v)} bytes hidden>" for key, v in (obj.get("data") or {}).items()}
        obj["binaryData"] = {key: "<hidden>" for key in (obj.get("binaryData") or {})}
    if k.plural == "sealedsecrets":
        obj.get("spec", {}).pop("encryptedData", None)
    # Env values may hold credentials passed in plain text; names and valueFrom refs are enough to debug.
    pod_spec = obj.get("spec", {}) if k.plural == "pods" else obj.get("spec", {}).get("template", {}).get("spec", {})
    for c in (pod_spec or {}).get("containers", []) + (pod_spec or {}).get("initContainers", []):
        for env in c.get("env", []) or []:
            if "value" in env and len(env["value"]) > 0 and any(s in env["name"].upper() for s in ("PASS", "TOKEN", "KEY", "SECRET")):
                env["value"] = "<hidden>"
    return obj


def _cpu_m(v: str) -> float:
    if v.endswith("n"):
        return int(v[:-1]) / 1e6
    if v.endswith("u"):
        return int(v[:-1]) / 1e3
    if v.endswith("m"):
        return float(v[:-1])
    return float(v) * 1000


def _mem_mib(v: str) -> float:
    units = {"Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "K": 1 / 1048.576, "M": 1 / 1.048576, "G": 953.67}
    for u, f in units.items():
        if v.endswith(u):
            return float(v[: -len(u)]) * f
    return float(v) / 1048576


class Kubernetes:
    def __init__(self, clusters: dict[str, KubeCluster]):
        self.clusters = clusters

    def c(self, name: str) -> KubeCluster:
        if name not in self.clusters:
            raise ToolInputError(f"unknown cluster {name!r}; configured: {', '.join(self.clusters)}")
        return self.clusters[name]

    def get(self, cluster: str, kind_: str, namespace: str | None = None, name: str | None = None,
            label_selector: str | None = None, field_selector: str | None = None) -> str:
        k, c = kind(kind_), self.c(cluster)
        ns = None if namespace in (None, "", "all", "*") else namespace
        if name:
            if k.namespaced and not ns:
                raise ToolInputError("namespace is required with a name")
            return summarize(k, c.json(k.path(ns, name)))
        items = c.json(k.path(ns), labelSelector=label_selector, fieldSelector=field_selector, limit=500)["items"]
        if k.plural == "events":
            items.sort(key=_event_time)
            items = items[::-1]
        lines = [summarize(k, i) for i in items]
        return trim(lines, len(lines), k.plural)

    def describe(self, cluster: str, kind_: str, name: str, namespace: str | None = None) -> str:
        k, c = kind(kind_), self.c(cluster)
        if k.namespaced and not namespace:
            raise ToolInputError("namespace is required")
        obj = _redact(k, c.json(k.path(namespace, name)))
        meta = obj.get("metadata", {})
        head = {key: meta[key] for key in ("name", "namespace", "labels", "annotations", "ownerReferences",
                                            "creationTimestamp", "deletionTimestamp") if meta.get(key)}
        # Status first: it is what explains a problem, and the spec is what gets cut when it is long.
        doc = {"metadata": head, "status": obj.get("status"), "spec": obj.get("spec")}
        for extra in ("data", "binaryData", "subsets", "roleRef"):
            if obj.get(extra):
                doc[extra] = obj[extra]
        text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=160)
        budget = MAX_CHARS - 1500
        if len(text) > budget:
            text = text[:budget] + "\n… object truncated"
        events = ""
        if k.plural not in ("events", "namespaces"):
            selector = f"involvedObject.name={name}"
            evs = c.json(kind("events").path(namespace), fieldSelector=selector)["items"]
            evs.sort(key=_event_time)
            events = "\nEvents:\n" + ("\n".join(event_line(e) for e in evs[-10:]) or "(none)")
        return text + events

    def _find_pod(self, c: KubeCluster, namespace: str, name: str) -> tuple[str, str]:
        """Exact pod name, or the newest pod whose name starts with it (a deployment's name, say)."""
        try:
            c.json(kind("pods").path(namespace, name))
            return name, ""
        except ToolInputError:
            pods = [p for p in c.json(kind("pods").path(namespace))["items"] if p["metadata"]["name"].startswith(name)]
            if not pods:
                raise ToolInputError(f"no pod {name!r} (or starting with it) in {namespace}") from None
            pods.sort(key=lambda p: p["metadata"]["creationTimestamp"])
            chosen = pods[-1]["metadata"]["name"]
            others = f", {len(pods) - 1} other match(es)" if len(pods) > 1 else ""
            return chosen, f"(pod {chosen}{others})\n"

    def logs(self, cluster: str, namespace: str, pod: str, container: str | None = None, tail: int = 100,
             previous: bool = False, since_minutes: int | None = None, grep: str | None = None) -> str:
        c = self.c(cluster)
        pod, note = self._find_pod(c, namespace, pod)
        tail = min(max(int(tail), 1), 1000 if grep else 300)
        text = c.get(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", container=container, tailLines=tail,
                     previous=str(previous).lower() if previous else None, timestamps="true",
                     sinceSeconds=int(since_minutes * 60) if since_minutes else None).text
        lines = text.splitlines()
        if grep:
            lines = [l for l in lines if grep.lower() in l.lower()]
            note += f"({len(lines)} lines match {grep!r} in the last {tail})\n"
        return note + clip("\n".join(l[:500] for l in lines))

    def events(self, cluster: str, namespace: str | None = None, warnings_only: bool = True, limit: int = 30) -> str:
        c = self.c(cluster)
        ns = None if namespace in (None, "", "all", "*") else namespace
        items = c.json(kind("events").path(ns), fieldSelector="type=Warning" if warnings_only else None)["items"]
        items.sort(key=_event_time)
        items = items[-min(max(int(limit), 1), 60):]
        return "\n".join(event_line(e) for e in reversed(items)) or "no events (they are kept ~1 hour)"

    def top(self, cluster: str, what: str = "pods", namespace: str | None = None, sort_by: str = "cpu") -> str:
        c = self.c(cluster)
        if what.startswith("node"):
            items = c.json("/apis/metrics.k8s.io/v1beta1/nodes")["items"]
            rows = [(i["metadata"]["name"], _cpu_m(i["usage"]["cpu"]), _mem_mib(i["usage"]["memory"])) for i in items]
        else:
            ns = None if namespace in (None, "", "all", "*") else namespace
            path = f"/apis/metrics.k8s.io/v1beta1{f'/namespaces/{ns}' if ns else ''}/pods"
            rows = [(f"{i['metadata']['namespace']}/{i['metadata']['name']}",
                     sum(_cpu_m(x["usage"]["cpu"]) for x in i["containers"]),
                     sum(_mem_mib(x["usage"]["memory"]) for x in i["containers"])) for i in c.json(path)["items"]]
        rows.sort(key=lambda r: r[2] if sort_by.startswith("mem") else r[1], reverse=True)
        lines = [f"{n} cpu={cpu:.0f}m mem={mem:.0f}Mi" for n, cpu, mem in rows]
        return trim(lines, len(lines), "rows")

    def overview(self, cluster: str) -> str:
        c = self.c(cluster)
        out = [f"cluster {cluster}:"]
        nodes = c.json(kind("nodes").path())["items"]
        out += ["nodes:"] + [f"  {summarize(kind('nodes'), n)}" for n in nodes]

        bad_pods = []
        recent = datetime.now(timezone.utc) - timedelta(hours=24)
        for p in c.json(kind("pods").path())["items"]:
            phase, ready, restarts = pod_status(p)
            n_ready, n_all = map(int, ready.split("/"))
            restarted_recently = any(
                (t := cs.get("lastState", {}).get("terminated", {}).get("finishedAt"))
                and datetime.fromisoformat(t.replace("Z", "+00:00")) > recent
                for cs in p.get("status", {}).get("containerStatuses", []) or [])
            if phase == "Succeeded":
                continue
            if phase != "Running" or n_ready < n_all or restarted_recently:
                bad_pods.append(f"  {summarize(kind('pods'), p)}")
        out += [f"problem pods ({len(bad_pods)}):"] + (bad_pods[:20] or ["  none"])

        workloads = []
        for kname in ("deployments", "statefulsets"):
            for d in c.json(kind(kname).path())["items"]:
                want = d.get("spec", {}).get("replicas", 1)
                if d.get("status", {}).get("readyReplicas", 0) < want:
                    workloads.append(f"  {kname[:-1]} {summarize(kind(kname), d)}")
        for d in c.json(kind("daemonsets").path())["items"]:
            s = d.get("status", {})
            if s.get("numberReady", 0) < s.get("desiredNumberScheduled", 0):
                workloads.append(f"  daemonset {summarize(kind('daemonsets'), d)}")
        out += [f"workloads not fully ready ({len(workloads)}):"] + (workloads or ["  none"])

        try:
            apps = [a for a in c.json(kind("applications").path())["items"]
                    if a.get("status", {}).get("sync", {}).get("status") != "Synced"
                    or a.get("status", {}).get("health", {}).get("status") != "Healthy"]
            out += [f"argocd apps not Synced/Healthy ({len(apps)}):"] + (
                [f"  {summarize(kind('applications'), a)}" for a in apps] or ["  none"])
        except ToolInputError:
            pass  # no Argo CD in this cluster

        warnings = c.json(kind("events").path(), fieldSelector="type=Warning")["items"]
        warnings.sort(key=_event_time)
        out += [f"warning events, last hour ({len(warnings)}), newest first:"] + (
            [f"  {event_line(e)}" for e in reversed(warnings[-10:])] or ["  none"])
        return clip("\n".join(out))


def kubernetes_tools(k8s: Kubernetes) -> list[BaseTool]:
    clusters = ", ".join(k8s.clusters)
    ClusterArg = Annotated[str, f"cluster name: {clusters}"]

    @tool
    @logged
    def k8s_overview(cluster: ClusterArg) -> str:
        """Health summary of a cluster in one call: nodes, pods that are not Running/ready or restarted in the last
        24h, workloads with missing replicas, Argo CD apps not Synced/Healthy, recent warning events.
        Start here for 'is everything OK' or 'what is broken'."""
        return k8s.overview(cluster)

    @tool
    @logged
    def k8s_get(
        cluster: ClusterArg,
        kind: Annotated[str, "resource kind: pods, deployments, statefulsets, daemonsets, services, nodes, pvc, pv, "
                              "events, jobs, cronjobs, configmaps, namespaces, httproutes, gateways, applications "
                              "(Argo CD), sealedsecrets, ingresses, hpa, … (not secrets)"],
        namespace: Annotated[str | None, "namespace; omit or 'all' for all namespaces"] = None,
        name: Annotated[str | None, "one object by name"] = None,
        label_selector: Annotated[str | None, "e.g. app=gitea"] = None,
        field_selector: Annotated[str | None, "e.g. status.phase!=Running or spec.nodeName=staging"] = None,
    ) -> str:
        """List objects (like kubectl get), one summary line each with status, readiness, restarts, age."""
        return k8s.get(cluster, kind, namespace, name, label_selector, field_selector)

    @tool
    @logged
    def k8s_describe(
        cluster: ClusterArg,
        kind: Annotated[str, "resource kind, as for k8s_get"],
        name: Annotated[str, "object name"],
        namespace: Annotated[str | None, "namespace (required for namespaced kinds)"] = None,
    ) -> str:
        """Full status and spec of one object plus its recent events (like kubectl describe). ConfigMap values and
        secret-looking env values are hidden."""
        return k8s.describe(cluster, kind, name, namespace)

    @tool
    @logged
    def k8s_logs(
        cluster: ClusterArg,
        namespace: str,
        pod: Annotated[str, "pod name, or a prefix such as the deployment name (the newest matching pod is used)"],
        container: Annotated[str | None, "container name, needed when the pod has several"] = None,
        tail: Annotated[int, "last N lines, max 300 (1000 with grep)"] = 100,
        previous: Annotated[bool, "logs of the previous (crashed) container instance"] = False,
        since_minutes: Annotated[int | None, "only lines from the last N minutes"] = None,
        grep: Annotated[str | None, "keep only lines containing this text (case-insensitive)"] = None,
    ) -> str:
        """Container logs of a pod, newest last. Use previous=true to see why a crash-looping container died."""
        return k8s.logs(cluster, namespace, pod, container, tail, previous, since_minutes, grep)

    @tool
    @logged
    def k8s_events(
        cluster: ClusterArg,
        namespace: Annotated[str | None, "namespace; omit for all"] = None,
        warnings_only: bool = True,
        limit: Annotated[int, "max events, newest first (max 60)"] = 30,
    ) -> str:
        """Recent cluster events (scheduling failures, image pull errors, OOM kills, probe failures). Kubernetes
        keeps events for about an hour; for older history use loki_query or Prometheus."""
        return k8s.events(cluster, namespace, warnings_only, limit)

    @tool
    @logged
    def k8s_top(
        cluster: ClusterArg,
        what: Annotated[str, "pods or nodes"] = "pods",
        namespace: Annotated[str | None, "namespace for pods; omit for all"] = None,
        sort_by: Annotated[str, "cpu or memory"] = "cpu",
    ) -> str:
        """Current CPU (millicores) and memory (MiB) usage from metrics-server (like kubectl top)."""
        return k8s.top(cluster, what, namespace, sort_by)

    return [k8s_overview, k8s_get, k8s_describe, k8s_logs, k8s_events, k8s_top]
