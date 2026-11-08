import logging
import threading
import time

import openai
from openai import OpenAI

from .agent import Agent
from .config import Settings
from .monitoring import Monitoring
from .telegram import Telegram, TelegramError

log = logging.getLogger("cluster_bot")

HISTORY_TURNS = 6  # question/answer pairs kept for follow-ups
HISTORY_TTL = 30 * 60  # a pause this long starts a fresh conversation

HELP = """\
Спросите о состоянии кластеров обычным текстом, например:
- всё ли в порядке?
- сколько свободно памяти и диска на staging?
- почему вчера вечером грелся production?
- какие поды перезапускались за сутки?

Команды:
/alerts — что горит сейчас (без модели, мгновенно)
/reset — забыть контекст разговора"""


class Bot:
    def __init__(self, telegram: Telegram, agent: Agent, monitoring: Monitoring, allowed_chats: frozenset[int]):
        self.telegram = telegram
        self.agent = agent
        self.monitoring = monitoring
        self.allowed_chats = allowed_chats
        self.history: dict[int, list[dict]] = {}
        self.last_seen: dict[int, float] = {}

    def handle(self, chat_id: int, text: str) -> str:
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/help"):
            return HELP
        if command == "/alerts":
            return self.monitoring.call("get_alerts", "{}")
        if command == "/reset":
            self.history.pop(chat_id, None)
            return "Контекст сброшен."

        if time.time() - self.last_seen.get(chat_id, 0) > HISTORY_TTL:
            self.history.pop(chat_id, None)
        self.last_seen[chat_id] = time.time()
        history = self.history.setdefault(chat_id, [])
        try:
            answer = self.agent.answer(history, text)
        except openai.APIError as e:
            log.error("LLM error: %s", e)
            return f"Модель недоступна: {type(e).__name__}. /alerts работает без неё."
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": answer}]
        del history[:-2 * HISTORY_TURNS]
        return answer

    def _keep_typing(self, chat_id: int, done: threading.Event) -> None:
        while not done.is_set():
            try:
                self.telegram.typing(chat_id)
            except TelegramError:
                pass
            done.wait(4)  # the indicator lasts ~5 s

    def process(self, update: dict) -> None:
        message = update.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text")
        if chat_id is None or not text:
            return
        if chat_id not in self.allowed_chats:
            log.warning("ignored message from chat %s", chat_id)
            return
        done = threading.Event()
        threading.Thread(target=self._keep_typing, args=(chat_id, done), daemon=True).start()
        try:
            reply = self.handle(chat_id, text)
        except Exception:
            log.exception("failed to answer")
            reply = "Не получилось ответить, подробности в логах пода cluster-bot."
        finally:
            done.set()
        self.telegram.send(chat_id, reply)

    def run(self) -> None:
        offset = None
        log.info("polling, allowed chats: %s", sorted(self.allowed_chats))
        while True:
            try:
                updates = self.telegram.updates(offset)
            except TelegramError as e:
                log.warning("getUpdates failed: %s", e)
                time.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1  # confirmed on the next poll, even if answering fails
                try:
                    self.process(update)
                except TelegramError as e:
                    log.error("sending reply failed: %s", e)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain the bot token
    settings = Settings.from_env()
    monitoring = Monitoring(settings.prometheus_url, settings.alertmanager_url, settings.timezone)
    llm = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url, timeout=120, max_retries=2)
    agent = Agent(llm, settings.llm_model, monitoring, settings.timezone, settings.llm_reasoning_effort,
                  settings.context)
    Bot(Telegram(settings.telegram_token), agent, monitoring, settings.allowed_chats).run()


if __name__ == "__main__":
    main()
