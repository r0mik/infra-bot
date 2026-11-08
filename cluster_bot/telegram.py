"""Minimal Bot API client (long polling).

The token is part of every request URL, so nothing here may log or raise a URL:
httpx's own request logging is silenced in main, and errors are re-raised without it.
"""

import httpx

MAX_MESSAGE = 4000  # Telegram's limit is 4096


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self._base = f"https://api.telegram.org/bot{token}"
        self.http = client or httpx.Client(timeout=70)

    def _call(self, method: str, **params):
        try:
            body = self.http.post(f"{self._base}/{method}", json=params).json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('error_code')} {body.get('description')}")
        return body["result"]

    def updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        return self._call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])

    def typing(self, chat_id: int) -> None:
        self._call("sendChatAction", chat_id=chat_id, action="typing")

    def send(self, chat_id: int, text: str) -> None:
        for i in range(0, len(text), MAX_MESSAGE):
            self._call("sendMessage", chat_id=chat_id, text=text[i:i + MAX_MESSAGE])
