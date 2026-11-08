import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from . import tools
from .bot import Bot
from .config import Settings
from .graph import build_graph
from .prompts import SYSTEM_PROMPT
from .telegram import Telegram


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Request lines of the HTTP clients: Telegram's contain the bot token, OpenAI's are noise.
    for name in ("httpx", "httpx2", "httpcore", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    s = Settings.from_env()

    tool_list, monitoring = tools.build(s)
    logging.getLogger("ops_agent").info("tools: %s", ", ".join(t.name for t in tool_list))

    context = s.context.strip() + "\n" if s.context.strip() else ""

    def system_prompt() -> str:
        now = datetime.now(ZoneInfo(s.timezone)).strftime("%Y-%m-%d %H:%M (%A)")
        return SYSTEM_PROMPT.format(tz=s.timezone, now=now, clusters=", ".join(s.clusters) or "none",
                                    nodes=", ".join(s.nodes) or "none", context=context)

    # Only sent when set: other OpenAI-compatible servers may reject the parameter.
    extra = {"reasoning_effort": s.llm_reasoning_effort} if s.llm_reasoning_effort else {}
    llm = ChatOpenAI(model=s.llm_model, api_key=s.llm_api_key, base_url=s.llm_base_url, timeout=120,
                     max_retries=2, **extra)
    graph = build_graph(llm, tool_list, system_prompt, max_steps=s.max_steps, history_turns=s.history_turns,
                        checkpointer=InMemorySaver())
    Bot(Telegram(s.telegram_token), graph, monitoring.alerts, s.allowed_chats).run()


if __name__ == "__main__":
    main()
