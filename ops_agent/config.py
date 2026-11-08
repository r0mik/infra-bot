import os
from dataclasses import dataclass
from pathlib import Path


def _secret(file_env: str, default_file: str) -> str:
    """Secrets come from mounted files, never from plain env or the manifest."""
    return Path(os.environ.get(file_env, default_file)).read_text().strip()


def _optional_file(file_env: str, default_file: str) -> str:
    path = Path(os.environ.get(file_env, default_file))
    return path.read_text() if path.is_file() else ""


def _pairs(value: str) -> dict[str, str]:
    """'a=x,b=y' → {'a': 'x', 'b': 'y'}"""
    out = {}
    for item in filter(None, (i.strip() for i in value.split(","))):
        name, _, target = item.partition("=")
        out[name.strip()] = target.strip()
    return out


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_chats: frozenset[int]
    llm_api_key: str
    llm_model: str
    llm_base_url: str | None
    llm_reasoning_effort: str | None
    timezone: str
    context: str  # description of this setup for the system prompt, see context.example.md
    prometheus_url: str
    alertmanager_url: str
    loki_url: str
    # name → "in-cluster" or https://host:6443; token and ca.crt for the others in <kube_secrets_dir>/<name>/
    clusters: dict[str, str]
    kube_secrets_dir: Path
    nodes: dict[str, str]  # name → user@host; empty disables node_command
    ssh_key_file: Path
    ssh_known_hosts: Path
    http_allowed_suffixes: tuple[str, ...]
    http_ca_file: Path | None
    max_steps: int
    history_turns: int

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            telegram_token=_secret("TELEGRAM_TOKEN_FILE", "/secrets/telegram/telegram-bot-token"),
            allowed_chats=frozenset(int(c) for c in os.environ["ALLOWED_CHAT_IDS"].split(",") if c.strip()),
            llm_api_key=_secret("LLM_API_KEY_FILE", "/secrets/llm/api-key"),
            llm_model=env("LLM_MODEL", "gpt-6-luna"),
            llm_base_url=env("LLM_BASE_URL") or None,
            llm_reasoning_effort=env("LLM_REASONING_EFFORT") or None,
            timezone=env("TZ", "UTC"),
            context=_optional_file("CONTEXT_FILE", "/config/context.md"),
            prometheus_url=env("PROMETHEUS_URL", "http://prometheus.monitoring:9090"),
            alertmanager_url=env("ALERTMANAGER_URL", "http://alertmanager.monitoring:9093"),
            loki_url=env("LOKI_URL", "http://loki.monitoring:3100"),
            clusters=_pairs(env("K8S_CLUSTERS", "production=in-cluster")),
            kube_secrets_dir=Path(env("KUBE_SECRETS_DIR", "/secrets/kube")),
            nodes=_pairs(env("NODES", "")),
            ssh_key_file=Path(env("SSH_KEY_FILE", "/secrets/ssh/id_ed25519")),
            ssh_known_hosts=Path(env("SSH_KNOWN_HOSTS", "/secrets/ssh/known_hosts")),
            http_allowed_suffixes=tuple(s.strip() for s in env("HTTP_ALLOWED_SUFFIXES", ".home.arpa").split(",") if s.strip()),
            http_ca_file=Path(env("HTTP_CA_FILE")) if env("HTTP_CA_FILE") else None,
            max_steps=int(env("MAX_STEPS", "12")),
            history_turns=int(env("HISTORY_TURNS", "6")),
        )
