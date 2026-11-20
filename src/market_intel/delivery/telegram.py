"""Send text to a Telegram chat through the Bot API.

The bot token and chat id come from ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` in
``.env`` (never committed). The digest is plain text laid out in columns, so each message
is wrapped in ``<pre>`` (HTML parse mode) to keep a monospace font, and long digests are
split on line boundaries to stay under Telegram's 4096-character message limit.
"""

from __future__ import annotations

import html
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096
# Room for the <pre></pre> wrapper and a "(1/3)" part marker.
_CHUNK_BUDGET = MAX_MESSAGE_CHARS - 64


class TelegramError(RuntimeError):
    pass


class MissingTelegramConfigError(TelegramError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id: str

    @classmethod
    def from_env(cls) -> TelegramConfig:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        missing = [
            name
            for name, value in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat_id))
            if not value
        ]
        if missing:
            raise MissingTelegramConfigError(
                f"Set {' and '.join(missing)} in .env to send the digest to Telegram "
                "(or use --dry-run to print it). See README, 'Telegram setup'."
            )
        return cls(token=token, chat_id=chat_id)


def split_text(text: str, limit: int = _CHUNK_BUDGET) -> list[str]:
    """Split on line boundaries so every chunk, once HTML-escaped, fits in ``limit``.

    A single line longer than the limit (rare: a very long URL) is hard-cut.
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        cost = len(html.escape(line, quote=False)) + 1
        while cost > limit:  # hard-cut an oversized line
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            cut = limit - 1
            while len(html.escape(line[:cut], quote=False)) > limit - 1:
                cut -= 64
            chunks.append(line[:cut])
            line = line[cut:]
            cost = len(html.escape(line, quote=False)) + 1
        if size + cost > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += cost
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def format_messages(text: str) -> list[str]:
    """Escape for HTML parse mode, wrap in <pre>, and number the parts if there are several."""
    chunks = split_text(text)
    n = len(chunks)
    out = []
    for i, chunk in enumerate(chunks, 1):
        body = f"<pre>{html.escape(chunk, quote=False)}</pre>"
        out.append(f"({i}/{n})\n{body}" if n > 1 else body)
    return out


class TelegramClient:
    def __init__(
        self,
        cfg: TelegramConfig,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        max_retries: int = 3,
        sleep=time.sleep,
    ) -> None:
        self.cfg = cfg
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep

    def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        url = f"{API_BASE}/bot{self.cfg.token}/{method}"
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.post(url, json=payload or {}, timeout=self.timeout)
            except requests.RequestException as exc:
                # Don't log the exception text: it can include the URL, which holds the token.
                if attempt < self.max_retries:
                    log.warning(
                        "Telegram %s network error (%s), retrying", method, type(exc).__name__
                    )
                    self._sleep(2**attempt)
                    continue
                raise TelegramError(f"Telegram {method} failed: {type(exc).__name__}") from None
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code == 429 and attempt < self.max_retries:
                wait = float(data.get("parameters", {}).get("retry_after", 2**attempt))
                log.warning("Telegram rate limit, waiting %.0fs", wait)
                self._sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < self.max_retries:
                self._sleep(2**attempt)
                continue
            if not data.get("ok"):
                desc = data.get("description") or f"HTTP {resp.status_code}"
                raise TelegramError(f"Telegram {method} failed: {desc}")
            return data.get("result")
        raise TelegramError(f"Telegram {method} failed after {self.max_retries} retries")

    def send_message(self, html_text: str) -> None:
        self._call(
            "sendMessage",
            {
                "chat_id": self.cfg.chat_id,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    def send_text(self, text: str) -> int:
        """Send plain text as one or more monospace messages. Returns the message count."""
        messages = format_messages(text)
        for i, msg in enumerate(messages):
            if i:
                self._sleep(1)  # stay well under Telegram's per-chat rate limit
            self.send_message(msg)
        return len(messages)

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def recent_chats(self) -> list[dict[str, Any]]:
        """Chats that recently messaged the bot, newest last (used to find the chat id)."""
        chats: dict[int, dict[str, Any]] = {}
        for update in self._call("getUpdates") or []:
            msg = update.get("message") or update.get("channel_post") or {}
            chat = msg.get("chat")
            if chat and "id" in chat:
                chats.pop(chat["id"], None)
                chats[chat["id"]] = chat
        return list(chats.values())
