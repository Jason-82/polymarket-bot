"""Telegram alerts. Best effort, never blocks the loop, never raises."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from .log import get_logger

log = get_logger(__name__)


class Alerts:
    def __init__(self, bot_token: str = "", chat_id: str = "", dedupe_seconds: float = 60.0):
        self._token = bot_token
        self._chat = chat_id
        self._dedupe = dedupe_seconds
        self._last: dict[str, float] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self.sent = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return bool(self._token and self._chat)

    def fire(self, text: str, key: Optional[str] = None) -> None:
        """Schedule a message; identical keys are suppressed for dedupe_seconds."""
        if not self.enabled:
            return
        k = key or text
        now = time.time()
        if now - self._last.get(k, 0.0) < self._dedupe:
            return
        self._last[k] = now
        try:
            asyncio.get_running_loop().create_task(self.send(text))
        except RuntimeError:
            pass  # no loop (tests / shutdown)

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10.0)
        try:
            r = await self._client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat, "text": text[:4000], "disable_web_page_preview": True},
            )
            ok = r.status_code == 200
        except Exception as e:
            log.warning("alert_send_failed", error=str(e)[:200])
            ok = False
        self.sent += int(ok)
        self.failed += int(not ok)
        return ok

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
