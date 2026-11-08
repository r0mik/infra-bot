"""Telegram front end: commands, per-chat threads, a live progress message while the agent works."""

import logging
import threading
import time

import openai
from langchain_core.messages import AIMessage, HumanMessage

from .telegram import Telegram, TelegramError

log = logging.getLogger(__name__)

THREAD_TTL = 30 * 60  # a pause this long starts a fresh conversation
RECURSION_LIMIT = 60  # LangGraph super-steps; the agent's own step budget ends a question far earlier

HELP = """\
Спросите о кластерах и нодах обычным текстом, например:
- всё ли в порядке?
- почему не открывается gitea?
- почему рестартится под в namespace shop?
- что с диском на staging и кто его занял?
- что было в журнале k3s на production за последний час?

Я только смотрю (Kubernetes, Prometheus, Loki, Alertmanager, команды на нодах) и ничего не меняю.
Если нужно что-то исправить, предложу команды, которые выполните вы.

Команды:
/alerts — что горит сейчас (без модели, мгновенно)
/trace — какие инструменты я вызвал для последнего ответа
/reset — забыть контекст разговора"""


def _short(args: dict) -> str:
    text = ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, "", False))
    return text if len(text) <= 90 else text[:87] + "…"


class Bot:
    def __init__(self, telegram: Telegram, graph, alerts, allowed_chats: frozenset[int]):
        self.telegram = telegram
        self.graph = graph
        self.alerts = alerts  # () -> str, for /alerts without the model
        self.allowed_chats = allowed_chats
        self.generation: dict[int, int] = {}
        self.last_seen: dict[int, float] = {}
        self.traces: dict[int, list[str]] = {}

    def _thread(self, chat_id: int, fresh: bool = False) -> str:
        if fresh or time.time() - self.last_seen.get(chat_id, 0) > THREAD_TTL:
            old = self.generation.get(chat_id)
            self.generation[chat_id] = (old or 0) + 1
            if old is not None and self.graph.checkpointer is not None:
                self.graph.checkpointer.delete_thread(f"{chat_id}:{old}")
        self.last_seen[chat_id] = time.time()
        return f"{chat_id}:{self.generation[chat_id]}"

    def ask(self, chat_id: int, question: str, progress=None) -> str:
        config = {"configurable": {"thread_id": self._thread(chat_id)}, "recursion_limit": RECURSION_LIMIT}
        log.info("question from %s: %s", chat_id, question[:500])
        started = time.monotonic()
        trace: list[str] = []
        final = None
        tokens_in = tokens_out = 0
        for update in self.graph.stream({"messages": [HumanMessage(question)], "steps": 0}, config,
                                        stream_mode="updates"):
            for node, delta in update.items():
                for m in (delta or {}).get("messages", []):
                    if isinstance(m, AIMessage):
                        usage = m.usage_metadata or {}
                        tokens_in += usage.get("input_tokens", 0)
                        tokens_out += usage.get("output_tokens", 0)
                    if isinstance(m, AIMessage) and m.tool_calls:
                        calls = [f"{c['name']}({_short(c['args'])})" for c in m.tool_calls]
                        trace += calls
                        if progress:
                            progress(trace)
                    elif isinstance(m, AIMessage) and node == "agent":
                        final = m
        self.traces[chat_id] = trace
        answer = (final.content if final else "") or ""
        if isinstance(answer, list):  # content blocks
            answer = "".join(b.get("text", "") for b in answer if isinstance(b, dict))
        answer = answer.strip() or "Модель вернула пустой ответ."
        log.info("answered in %.1fs with %d tool calls, tokens in=%d out=%d:\n%s", time.monotonic() - started,
                 len(trace), tokens_in, tokens_out, answer[:1000])
        return answer

    def handle(self, chat_id: int, text: str, progress=None) -> str:
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if command in ("/start", "/help"):
            return HELP
        if command == "/alerts":
            return self.alerts()
        if command == "/reset":
            self._thread(chat_id, fresh=True)
            return "Контекст сброшен."
        if command == "/trace":
            trace = self.traces.get(chat_id)
            return "Вызовы для последнего ответа:\n" + "\n".join(f"- {t}" for t in trace) if trace else \
                "Для последнего ответа инструменты не вызывались."
        try:
            return self.ask(chat_id, text, progress)
        except openai.APIError as e:
            log.error("LLM error: %s", e)
            return f"Модель недоступна: {type(e).__name__}. /alerts работает без неё."

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
        status_id: list[int] = []

        def keep_typing():
            while not done.is_set():
                try:
                    self.telegram.typing(chat_id)
                except TelegramError:
                    pass
                done.wait(4)  # the indicator lasts ~5 s

        def progress(trace: list[str]):
            text_ = "Смотрю:\n" + "\n".join(f"- {t}" for t in trace[-12:])
            try:
                if status_id:
                    self.telegram.edit(chat_id, status_id[0], text_)
                else:
                    status_id.append(self.telegram.send(chat_id, text_))
            except TelegramError as e:
                log.debug("progress update failed: %s", e)

        threading.Thread(target=keep_typing, daemon=True).start()
        try:
            reply = self.handle(chat_id, text, progress)
        except Exception:
            log.exception("failed to answer")
            reply = "Не получилось ответить, подробности в логах пода ops-agent."
        finally:
            done.set()
        if status_id:  # the progress list served its purpose; /trace keeps it
            try:
                self.telegram.delete(chat_id, status_id[0])
            except TelegramError:
                pass
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
