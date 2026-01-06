"""Alert dispatching to external services."""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import List, Optional
import httpx

from monitoring.logger import get_logger

logger = get_logger(__name__)


class AlertSeverity(str, Enum):
    """Alert severity levels."""
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class Alert:
    """Alert message."""
    severity: AlertSeverity
    title: str
    message: str
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()


class AlertManager:
    """
    Manages alert dispatching to external services.

    Supports:
    - Telegram
    - Slack
    - Console (always enabled)
    """

    def __init__(
        self,
        telegram_bot_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        slack_webhook_url: Optional[str] = None,
    ):
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.slack_webhook_url = slack_webhook_url
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "AlertManager":
        """Async context manager entry."""
        self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def send(self, alert: Alert) -> None:
        """Send an alert to all configured channels."""
        # Always log to console
        self._log_alert(alert)

        # Send to external services
        tasks = []

        if self.telegram_bot_token and self.telegram_chat_id:
            tasks.append(self._send_telegram(alert))

        if self.slack_webhook_url:
            tasks.append(self._send_slack(alert))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def send_info(self, title: str, message: str) -> None:
        """Send an info alert."""
        await self.send(Alert(AlertSeverity.INFO, title, message))

    async def send_warning(self, title: str, message: str) -> None:
        """Send a warning alert."""
        await self.send(Alert(AlertSeverity.WARNING, title, message))

    async def send_error(self, title: str, message: str) -> None:
        """Send an error alert."""
        await self.send(Alert(AlertSeverity.ERROR, title, message))

    async def send_critical(self, title: str, message: str) -> None:
        """Send a critical alert."""
        await self.send(Alert(AlertSeverity.CRITICAL, title, message))

    def _log_alert(self, alert: Alert) -> None:
        """Log alert to console."""
        log_fn = {
            AlertSeverity.INFO: logger.info,
            AlertSeverity.WARNING: logger.warning,
            AlertSeverity.ERROR: logger.error,
            AlertSeverity.CRITICAL: logger.critical,
        }.get(alert.severity, logger.info)

        log_fn(
            "alert_sent",
            severity=alert.severity.value,
            title=alert.title,
            message=alert.message,
        )

    async def _send_telegram(self, alert: Alert) -> None:
        """Send alert to Telegram."""
        if not self._client:
            return

        emoji = {
            AlertSeverity.INFO: "ℹ️",
            AlertSeverity.WARNING: "⚠️",
            AlertSeverity.ERROR: "❌",
            AlertSeverity.CRITICAL: "🚨",
        }.get(alert.severity, "📢")

        text = f"{emoji} *{alert.title}*\n\n{alert.message}\n\n_{alert.timestamp.isoformat()}_"

        try:
            url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
            response = await self._client.post(url, json={
                "chat_id": self.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
            })
            response.raise_for_status()
        except Exception as e:
            logger.error("telegram_alert_failed", error=str(e))

    async def _send_slack(self, alert: Alert) -> None:
        """Send alert to Slack."""
        if not self._client:
            return

        color = {
            AlertSeverity.INFO: "#36a64f",
            AlertSeverity.WARNING: "#ffcc00",
            AlertSeverity.ERROR: "#ff0000",
            AlertSeverity.CRITICAL: "#8b0000",
        }.get(alert.severity, "#cccccc")

        try:
            response = await self._client.post(self.slack_webhook_url, json={
                "attachments": [{
                    "color": color,
                    "title": f"[{alert.severity.value}] {alert.title}",
                    "text": alert.message,
                    "ts": int(alert.timestamp.timestamp()),
                }]
            })
            response.raise_for_status()
        except Exception as e:
            logger.error("slack_alert_failed", error=str(e))

    # Convenience methods for common alerts

    async def alert_kill_switch(self, reason: str) -> None:
        """Alert when kill switch is triggered."""
        await self.send_critical(
            "KILL SWITCH TRIGGERED",
            f"Trading has been halted.\n\nReason: {reason}"
        )

    async def alert_geoblock(self) -> None:
        """Alert when geoblocked."""
        await self.send_warning(
            "Geoblock Detected",
            "Trading is not available in your region. Bot running in READ_ONLY mode."
        )

    async def alert_large_loss(self, loss: float, threshold: float) -> None:
        """Alert on large loss."""
        await self.send_error(
            "Large Loss Alert",
            f"Loss of ${loss:.2f} exceeds threshold of ${threshold:.2f}"
        )

    async def alert_api_failure(self, api: str, error: str) -> None:
        """Alert on API failure."""
        await self.send_warning(
            f"{api} API Failure",
            f"Repeated failures detected: {error}"
        )
