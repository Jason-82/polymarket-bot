"""Kill switch implementation."""

import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from monitoring.logger import get_logger

logger = get_logger(__name__)


class KillSwitchReason(str, Enum):
    """Reasons for kill switch activation."""
    MANUAL_FILE = "manual_file"
    MANUAL_ENV = "manual_env"
    MANUAL_TRIGGER = "manual_trigger"
    MAX_DAILY_LOSS = "max_daily_loss"
    MAX_DRAWDOWN = "max_drawdown"
    ERROR_RATE = "error_rate"
    WS_DISCONNECTED = "ws_disconnected"
    GEOBLOCK = "geoblock"
    API_FAILURE = "api_failure"


@dataclass
class KillSwitchState:
    """State of the kill switch."""
    active: bool
    reason: Optional[KillSwitchReason] = None
    triggered_at: Optional[datetime] = None
    details: Optional[str] = None


class KillSwitch:
    """
    Kill switch for emergency trading halt.

    Checks multiple trigger sources:
    - Manual file trigger (create KILL_SWITCH file)
    - Environment variable (KILL_SWITCH=1)
    - Programmatic trigger from risk manager

    When triggered:
    - Cancels all open orders
    - Halts trading loop
    - Sends alerts
    """

    # File to check for manual kill switch
    KILL_FILE_NAME = "KILL_SWITCH"

    def __init__(self, base_path: Path = Path(".")):
        self.base_path = base_path
        self._state = KillSwitchState(active=False)
        self._on_trigger_callbacks = []

    @property
    def is_active(self) -> bool:
        """Check if kill switch is active."""
        return self._state.active

    @property
    def state(self) -> KillSwitchState:
        """Get current kill switch state."""
        return self._state

    def check(self) -> KillSwitchState:
        """
        Check all kill switch trigger sources.

        Returns current state after checking.
        """
        # Already active - return current state
        if self._state.active:
            return self._state

        # Check manual file trigger
        kill_file = self.base_path / self.KILL_FILE_NAME
        if kill_file.exists():
            self.trigger(KillSwitchReason.MANUAL_FILE, "Kill file detected")
            return self._state

        # Check environment variable
        if os.getenv("KILL_SWITCH", "").lower() in ("1", "true", "yes"):
            self.trigger(KillSwitchReason.MANUAL_ENV, "KILL_SWITCH env var set")
            return self._state

        return self._state

    def trigger(
        self,
        reason: KillSwitchReason,
        details: Optional[str] = None,
    ) -> None:
        """
        Trigger the kill switch.

        Args:
            reason: Why the kill switch was triggered
            details: Additional details
        """
        if self._state.active:
            logger.warning("kill_switch_already_active")
            return

        self._state = KillSwitchState(
            active=True,
            reason=reason,
            triggered_at=datetime.utcnow(),
            details=details,
        )

        logger.critical(
            "kill_switch_triggered",
            reason=reason.value,
            details=details,
        )

        # Call registered callbacks
        for callback in self._on_trigger_callbacks:
            try:
                callback(self._state)
            except Exception as e:
                logger.error("kill_switch_callback_error", error=str(e))

    def reset(self) -> None:
        """
        Reset the kill switch.

        WARNING: This allows trading to resume. Only do this after
        investigating why the kill switch was triggered.
        """
        if not self._state.active:
            return

        logger.warning(
            "kill_switch_reset",
            was_reason=self._state.reason.value if self._state.reason else None,
        )

        # Remove kill file if it exists
        kill_file = self.base_path / self.KILL_FILE_NAME
        if kill_file.exists():
            kill_file.unlink()
            logger.info("kill_switch_file_removed")

        self._state = KillSwitchState(active=False)

    def on_trigger(self, callback) -> None:
        """Register a callback to be called when kill switch is triggered."""
        self._on_trigger_callbacks.append(callback)

    def create_kill_file(self) -> None:
        """Create the kill switch file (for testing or manual trigger)."""
        kill_file = self.base_path / self.KILL_FILE_NAME
        kill_file.touch()
        logger.info("kill_switch_file_created", path=str(kill_file))
