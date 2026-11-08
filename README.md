# infra-bot

Telegram bot that answers questions about a homelab ("всё ли в порядке?", "почему вчера грелся staging?")
from Prometheus and Alertmanager, using an LLM with read-only tools. It can share the bot token
Alertmanager uses for alerts: Alertmanager only sends, this one long-polls `getUpdates`. It answers only
`ALLOWED_CHAT_IDS`.

```
Telegram ─► infra-bot (pod next to Prometheus)
              ├─► LLM (OpenAI by default; any OpenAI-compatible endpoint via LLM_BASE_URL)
              └─► tools, read-only: Prometheus API (instant / range / metric names / targets / rules),
                                    Alertmanager API (firing alerts)
```

- Commands: `/alerts` (no model, instant), `/reset` (forget the conversation), `/help`.
- Keeps the last 6 question/answer pairs per chat for follow-ups, forgotten after 30 min of silence.
- Logs every tool call with its full result, so a wrong answer can be traced to the data the model saw.
- Logs token usage per answer (`answered in N rounds, tokens in=… out=…`). A typical question is
  ~3k in / 0.2k out tokens, well under $0.001 with gpt-6-luna.
- The bot token is in every Telegram URL: httpx request logging is off and Telegram errors are re-raised
  without the URL. Keep it that way.

## Describing your setup

The model only knows what the tools return, so tell it what your nodes, labels, jobs and useful metrics
are: write a short file like [context.example.md](context.example.md) and mount it at `CONTEXT_FILE`
(e.g. from a ConfigMap). It is added to the system prompt. Without it the bot still works, it just has to
discover more with `find_metrics` / `get_targets`.

## Configuration (env)

| Variable | Default |
|---|---|
| `ALLOWED_CHAT_IDS` | required, comma-separated Telegram chat ids |
| `LLM_MODEL` | `gpt-6-luna` |
| `LLM_BASE_URL` | unset = OpenAI. Ollama: `http://<host>:11434/v1`; Gemini: its OpenAI-compatible endpoint |
| `LLM_REASONING_EFFORT` | unset (not sent). Set `none` for gpt-6-luna: it rejects tools + reasoning in Chat Completions |
| `LLM_API_KEY_FILE` | `/secrets/llm/api-key` (Ollama ignores the key, but the file must exist) |
| `TELEGRAM_TOKEN_FILE` | `/secrets/telegram/telegram-bot-token` |
| `CONTEXT_FILE` | `/config/context.md`, optional |
| `PROMETHEUS_URL`, `ALERTMANAGER_URL` | `http://prometheus:9090`, `http://alertmanager:9093` |
| `TZ` | `UTC` |

Secrets are read from files (mounted Kubernetes Secrets), never from plain env.

## Develop

```bash
uv sync && uv run pytest -q
```

## Run in Kubernetes

```bash
docker buildx build --platform linux/amd64 -t <registry>/infra-bot:<tag> --push .
```

Run exactly one replica with `strategy: Recreate`: Telegram allows a single `getUpdates` poller per token.
`getUpdates failed: 409 Conflict` in the log means a second poller or a webhook uses the token.
The container runs as uid 10001 and works with a read-only root filesystem; mount the two secret files
and, optionally, the context file. Set a monthly budget limit in your LLM provider's console.
