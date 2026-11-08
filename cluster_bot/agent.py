import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import OpenAI

from .monitoring import TOOLS, Monitoring

log = logging.getLogger(__name__)

MAX_ROUNDS = 8

SYSTEM_PROMPT = """\
You are the on-call assistant of a small homelab. Its owner asks you in Telegram about the state of \
the servers and Kubernetes clusters. Answer only from data you fetched with the tools in this \
conversation; never guess a number. If the tools can't tell, say so.

{context}
How to answer:
- In the language of the question (usually Russian).
- Lead with the verdict (all fine / what is wrong), then the few numbers that support it, with units, \
rounded (GiB, %, °C). A few lines; more only if asked.
- Plain text only: no Markdown, no tables, no ** or #. Telegram shows them raw. Simple "- " lists are fine.
- Times in {tz}. Now is {now}.
"""


class Agent:
    def __init__(self, llm: OpenAI, model: str, monitoring: Monitoring, timezone: str = "UTC",
                 reasoning_effort: str | None = None, context: str = ""):
        self.llm = llm
        self.model = model
        self.monitoring = monitoring
        self.timezone = timezone
        # Describes this particular setup (nodes, labels, jobs, metrics); kept out of the code.
        self.context = context.strip() + "\n" if context.strip() else ""
        # Only sent when set: other OpenAI-compatible servers may reject the parameter.
        self.extra = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}

    def _system(self) -> dict:
        now = datetime.now(ZoneInfo(self.timezone)).strftime("%Y-%m-%d %H:%M (%A)")
        return {"role": "system", "content": SYSTEM_PROMPT.format(tz=self.timezone, now=now, context=self.context)}

    def answer(self, history: list[dict], question: str) -> str:
        """history holds earlier user/assistant text turns only, not tool calls."""
        messages = [self._system(), *history, {"role": "user", "content": question}]
        tokens_in = tokens_out = 0
        for round_ in range(MAX_ROUNDS + 1):
            last = round_ == MAX_ROUNDS
            response = self.llm.chat.completions.create(
                model=self.model, messages=messages, tools=TOOLS, tool_choice="none" if last else "auto",
                **self.extra,
            )
            if response.usage:
                tokens_in += response.usage.prompt_tokens
                tokens_out += response.usage.completion_tokens
            message = response.choices[0].message
            if not message.tool_calls or last:
                log.info("answered in %d rounds, tokens in=%d out=%d", round_ + 1, tokens_in, tokens_out)
                return (message.content or "").strip() or "Модель вернула пустой ответ."
            messages.append(message.model_dump(exclude_none=True))
            for call in message.tool_calls:
                result = self.monitoring.call(call.function.name, call.function.arguments)
                # Full result, so a wrong answer can be traced to what the model actually saw.
                log.info("tool %s(%s) ->\n%s", call.function.name, call.function.arguments, result)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        raise AssertionError("unreachable: the last round always returns")
