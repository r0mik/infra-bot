import os
from dataclasses import dataclass
from pathlib import Path


def _secret(file_env: str, default_file: str) -> str:
    """Secrets come from mounted files, never from plain env or the manifest."""
    return Path(os.environ.get(file_env, default_file)).read_text().strip()


def _optional_file(file_env: str, default_file: str) -> str:
    path = Path(os.environ.get(file_env, default_file))
    return path.read_text() if path.is_file() else ""


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_chats: frozenset[int]
    llm_api_key: str
    llm_model: str
    llm_base_url: str | None  # None = OpenAI; set for Ollama/Gemini OpenAI-compatible endpoints
    llm_reasoning_effort: str | None  # gpt-6-luna needs "none": no tools + reasoning in Chat Completions
    prometheus_url: str
    alertmanager_url: str
    timezone: str
    context: str  # description of this setup for the system prompt, see context.example.md

    @classmethod
    def from_env(cls) -> "Settings":
        chats = os.environ["ALLOWED_CHAT_IDS"]
        return cls(
            telegram_token=_secret("TELEGRAM_TOKEN_FILE", "/secrets/telegram/telegram-bot-token"),
            allowed_chats=frozenset(int(c) for c in chats.split(",") if c.strip()),
            llm_api_key=_secret("LLM_API_KEY_FILE", "/secrets/llm/api-key"),
            llm_model=os.environ.get("LLM_MODEL", "gpt-6-luna"),
            llm_base_url=os.environ.get("LLM_BASE_URL") or None,
            llm_reasoning_effort=os.environ.get("LLM_REASONING_EFFORT") or None,
            prometheus_url=os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"),
            alertmanager_url=os.environ.get("ALERTMANAGER_URL", "http://alertmanager:9093"),
            timezone=os.environ.get("TZ", "UTC"),
            context=_optional_file("CONTEXT_FILE", "/config/context.md"),
        )
