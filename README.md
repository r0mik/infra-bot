# ops-agent

Read-only on-call agent for a homelab, built on LangGraph. It answers questions in Telegram
("всё ли в порядке?", "почему рестартится под в shop?", "что с диском на staging?") and does first-line
troubleshooting across Kubernetes, the nodes' host OS and monitoring. It never changes anything; when a fix is
needed it proposes the commands for you to run.

Successor of cluster-bot (branch `cluster-bot`, Prometheus + Alertmanager only): it has all of its tools plus
Kubernetes, Loki, the nodes over SSH and HTTP checks.

```
Telegram ─► bot.py (long polling, /alerts /trace /reset, live "Смотрю: …" progress)
              └─► LangGraph (graph.py)          START → agent ⇄ tools → END, ≤12 model calls per question,
                    │                           memory per chat (InMemorySaver), 30 min idle = fresh thread
                    ├─► LLM: OpenAI gpt-6-luna (any OpenAI-compatible endpoint via LLM_BASE_URL)
                    └─► tools (all read-only):
                          monitoring   get_alerts, prometheus_query(_range), find_metrics, get_scrape_targets,
                                       get_alert_rules, loki_query, loki_labels
                          kubernetes   k8s_overview, k8s_get, k8s_describe, k8s_logs, k8s_events, k8s_top
                                       clusters: production (in-cluster SA), staging (SA token)
                          nodes        node_command: allowlisted commands over SSH (production, staging)
                          http         http_check: GET/HEAD of *.home.arpa and private IPs
```

## How read-only is enforced

Each kind of access is a tool, and each tool is limited on both ends: in the agent's code and in the system it
talks to. A bug or a prompt injection in the agent can't get past the second limit.

| Access | Limit in the agent | Limit on the other side |
|---|---|---|
| Kubernetes | `KubeCluster` only has a GET method. Secrets are refused, ConfigMap values and secret-looking env values are hidden | ClusterRole `ops-agent-readonly`: only get/list/watch, no `secrets`, `pods/exec`, `pods/attach`, `pods/portforward`, `nodes/proxy` ([rbac.yaml](deploy/k8s/rbac.yaml), checked by `tests/test_readonly.py`) |
| Nodes | `node_policy.validate` checks every argument before SSH is opened; no shell, no `; \| $ > \``  | The node user `ops-agent`: unprivileged, no password, group `systemd-journal` only. Its key is `restrict` (no pty or forwarding), `from=` pod CIDR/LAN, `command="/usr/local/bin/ops-agent-ro"`: the same `node_policy.py` checks `$SSH_ORIGINAL_COMMAND` again. sudo only for three exact docker read commands |
| Prometheus, Loki, Alertmanager | only GET on query endpoints (no silences, no admin API) | — (their query APIs are read-only) |
| HTTP | GET/HEAD only, host must be `*.home.arpa` or a private IP | — |

Allowed node commands (also shown to the model): `overview`, `uptime`, `free`, `df`, `lsblk`, `du`, `cat` of a
few /proc and /etc files, `ps aux`, `top -bn1`, `sensors`, `systemctl status|is-active|list-units|list-timers`,
`journalctl` with filters, `ip … show`, `ss`, `ping -c ≤5`, `dig`, `docker ps|stats|logs`. See
[node_policy.py](ops_agent/tools/node_policy.py). To add one, add a validator there, a test case, and re-run
`deploy/node/install.sh` on both nodes.

The model sees logs and command output. The prompt tells it to treat them as data, and the limits above mean an
injected instruction still can't change anything. Data does go to OpenAI: ConfigMap values and env vars named
`*PASS*`, `*TOKEN*`, `*KEY*` and `*SECRET*` are hidden, but logs are sent as they are.

## Telegram

Only one process may long-poll a bot token. Alertmanager's Telegram receiver only sends messages, so the agent
can share its bot. `getUpdates failed: 409 Conflict` in the log means a second poller or a webhook uses the token.

Commands: `/alerts` (no model, instant), `/trace` (tool calls behind the last answer), `/reset`, `/help`.

## Configuration (env)

| Variable | Default |
|---|---|
| `ALLOWED_CHAT_IDS` | required, comma-separated |
| `LLM_MODEL`, `LLM_BASE_URL`, `LLM_REASONING_EFFORT` | `gpt-6-luna`, unset (= OpenAI), unset (manifest: `none`) |
| `LLM_API_KEY_FILE`, `TELEGRAM_TOKEN_FILE` | `/secrets/llm/api-key`, `/secrets/telegram/telegram-bot-token` |
| `PROMETHEUS_URL`, `ALERTMANAGER_URL`, `LOKI_URL` | `http://prometheus.monitoring:9090`, `http://alertmanager.monitoring:9093`, `http://loki.monitoring:3100` |
| `K8S_CLUSTERS` | `production=in-cluster`; others as `name=https://host:6443`, with `token` and `ca.crt` in `$KUBE_SECRETS_DIR/<name>/` |
| `NODES` | unset = no node_command; `name=ops-agent@ip,…` |
| `SSH_KEY_FILE`, `SSH_KNOWN_HOSTS` | `/secrets/ssh/id_ed25519`, `/secrets/ssh/known_hosts` |
| `HTTP_ALLOWED_SUFFIXES`, `HTTP_CA_FILE` | `.home.arpa`, unset (your internal root CA, if any) |
| `CONTEXT_FILE` | `/config/context.md`, optional: your setup's description for the prompt, see below |
| `MAX_STEPS`, `HISTORY_TURNS`, `TZ` | `12`, `6`, `UTC` |

A cluster without a token file or nodes without a key are skipped with a warning, so the agent starts with
whatever access is set up.

### Describing your setup

The model only knows what the tools return, so tell it what your nodes, clusters, apps, metric labels and log
sources are: write a short file like [context.example.md](context.example.md), put it into ConfigMap
`ops-agent-context` (`kubectl -n ops-agent create configmap ops-agent-context --from-file=context.md`) and it
is added to the system prompt. Without it the agent still works, it just explores more.

## Develop

```bash
uv sync && uv run pytest -q
```

Try the node policy on a node without installing anything (runs as you, only allowlisted commands):

```bash
ssh admin@node1 "SSH_ORIGINAL_COMMAND='overview' python3 -" < ops_agent/tools/node_policy.py
```

## Deploy

```bash
TAG=$(date +%Y%m%d)-1
STAGING="--context staging"   # a second cluster, optional
mkdir -p secrets              # git-ignored

# 1. image
docker buildx build --platform linux/amd64 -t <registry>/ops-agent:$TAG --push .

# 2. read-only identity in every cluster (+ a long-lived token where the agent doesn't run)
kubectl apply --server-side -f deploy/k8s/rbac.yaml
kubectl $STAGING apply --server-side -f deploy/k8s/rbac.yaml -f deploy/k8s/staging-token.yaml

# 3. secrets, namespace ops-agent (paste the values, don't put them in files you commit)
pbpaste | kubectl -n ops-agent create secret generic ops-agent-telegram --from-file=telegram-bot-token=/dev/stdin
pbpaste | kubectl -n ops-agent create secret generic ops-agent-llm --from-file=api-key=/dev/stdin
kubectl $STAGING -n ops-agent get secret ops-agent-token -o jsonpath='{.data.token}' | base64 -d > secrets/token
kubectl $STAGING -n ops-agent get secret ops-agent-token -o jsonpath='{.data.ca\.crt}' | base64 -d > secrets/ca.crt
kubectl -n ops-agent create secret generic ops-agent-staging-kube --from-file=secrets/token --from-file=secrets/ca.crt
kubectl -n ops-agent create configmap ops-agent-context --from-file=context.md          # optional
kubectl -n ops-agent create configmap http-ca --from-file=ca.crt=<your-root-ca.crt>    # optional

# 4. node access: one key for the agent, installed on every node with the forced command
ssh-keygen -t ed25519 -N '' -C ops-agent -f secrets/id_ed25519
export FROM=10.42.0.0/16,<your LAN CIDR>      # pod CIDR + LAN the agent connects from
deploy/node/install.sh admin@node1 secrets/id_ed25519.pub
deploy/node/install.sh admin@node2 secrets/id_ed25519.pub
ssh-keyscan -t ed25519 node1 node2 > secrets/known_hosts
kubectl -n ops-agent create secret generic ops-agent-ssh \
  --from-file=secrets/id_ed25519 --from-file=secrets/known_hosts

# 5. start: copy deploy/k8s/example.yaml, fill in the <placeholders>
kubectl apply -f my-ops-agent.yaml
kubectl -n ops-agent logs -f deploy/ops-agent      # "tools: get_alerts, …, node_command, http_check"
rm -rf secrets
```

Check that it really is read-only:

```bash
SA=system:serviceaccount:ops-agent:ops-agent
kubectl auth can-i --list --as=$SA | grep -vE '\[(get|list|watch)( |\])' || true   # only discovery/self-review lines
kubectl auth can-i get secrets --as=$SA -A            # no
kubectl auth can-i create pods/exec --as=$SA -A       # no
kubectl auth can-i delete pods --as=$SA -A            # no
# from the agent's key: a refused command
ssh -i secrets/id_ed25519 ops-agent@node2 'systemctl restart k3s'   # refused: systemctl restart not allowed
```

Update the node policy after editing `node_policy.py`: re-run the `install.sh` lines (the key stays).
Revoke node access: `sudo userdel -r ops-agent; sudo rm /usr/local/bin/ops-agent-ro /etc/sudoers.d/ops-agent` on
each node.
