"""The owner's channel: Telegram when it is configured, the log always.

Ported from the legacy ``TelegramNotifier`` (legacy/old modules/notifications/telegram_notifier.py)
and the run summary's Telegram packaging (``run_summary.to_telegram_html``): the Bot API's
``sendMessage`` in HTML mode, the text escaped, the headline bold, a long message split under
Telegram's cap. As in the legacy delivery, a send **never raises**: a failure is logged and
reported as ``False``, so the bot's work never waits on its messenger. The bot token is part of
the request path, so no URL or exception text that could carry it is ever logged.

Without ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` the :class:`LogNotifier` writes every
message to the log, which the unattended machine keeps.

SPDX-License-Identifier: AGPL-3.0-or-later
"""

import html
from typing import Protocol

import httpx
from pydantic import SecretStr

from ..config.schema_runtime import ReportingConfig
from ..logging_setup import get_logger

log = get_logger("reporting.notifier")

TELEGRAM_API = "https://api.telegram.org"
HTTP_OK = 200


class Notifier(Protocol):
    """Sends one message; ``True`` when it was delivered (or, for the log, written)."""

    async def send(self, subject: str, body: str) -> bool: ...


def to_telegram_html(subject: str, body: str, *, chunk: int) -> list[str]:
    """Escape for Telegram's HTML mode, bold the subject, and split under ``chunk`` characters
    at line ends; nothing is lost (legacy run_summary.to_telegram_html)."""
    text = f"<b>{html.escape(subject)}</b>\n\n{html.escape(body)}"
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > chunk:  # a single line beyond the cap is cut, not dropped
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:chunk])
            line = line[chunk:]
        if current and len(current) + len(line) + 1 > chunk:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks


class LogNotifier:
    """Every message to the log: the channel that works with nothing configured."""

    async def send(self, subject: str, body: str) -> bool:
        log.warning("owner message", extra={"subject": subject, "body": body})
        return True


class TelegramNotifier:
    """Messages through a Telegram bot to one chat, and to the log as well."""

    def __init__(
        self,
        reporting: ReportingConfig,
        *,
        token: SecretStr,
        chat_id: SecretStr,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        telegram = reporting.telegram
        self._chunk = telegram.chunk_characters
        self._timeout = telegram.timeout_seconds
        self._token = token
        self._chat_id = chat_id
        self._client = client
        self._log = LogNotifier()

    async def send(self, subject: str, body: str) -> bool:
        await self._log.send(subject, body)
        url = f"{TELEGRAM_API}/bot{self._token.get_secret_value()}/sendMessage"
        delivered = True
        client = self._client if self._client is not None else httpx.AsyncClient()
        try:
            for part in to_telegram_html(subject, body, chunk=self._chunk):
                try:
                    response = await client.post(
                        url,
                        json={
                            "chat_id": self._chat_id.get_secret_value(),
                            "text": part,
                            "parse_mode": "HTML",
                        },
                        timeout=self._timeout,
                    )
                except httpx.HTTPError as exc:
                    # The exception's text can carry the URL, and the URL the token.
                    log.warning("telegram send failed", extra={"error": type(exc).__name__})
                    return False
                if response.status_code != HTTP_OK:
                    log.warning(
                        "telegram refused the message", extra={"status": response.status_code}
                    )
                    delivered = False
        finally:
            if self._client is None:
                await client.aclose()
        return delivered
