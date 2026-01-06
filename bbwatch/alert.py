"""Alert system with hysteresis logic and action handling.

Manages the alert state machine to prevent rapid oscillations (hysteresis),
writes status for the overlay, and handles side effects like recording clips.
"""

import enum
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from bbwatch.config import AlertConfig

LOGGER = logging.getLogger(__name__)


class AlertState(str, enum.Enum):
    """System alert states."""

    IDLE = "idle"
    TRIGGERED = "triggered"
    COOLDOWN = "cooldown"


@dataclass
class AlertStatus:
    """Status structure written to JSON."""

    system_status: str
    alert_state: str  # AlertState value
    alert_active: bool  # True if TRIGGERED (compatibility)
    timestamp: float
    intensity: float


class AlertManager:
    """Manages alert state transitions and side effects."""

    def __init__(self, config: AlertConfig):
        """Initialize alert manager.

        Args:
            config: Alert configuration.
        """
        self.config = config
        self._state = AlertState.IDLE
        self._last_trigger_time = 0.0
        self._cooldown_start_time = 0.0

    @property
    def current_state(self) -> AlertState:
        """Get current alert state."""
        return self._state

    def process_intensity(self, intensity: float) -> AlertStatus:
        """Update state based on new intensity measurement.

        Implements hysteresis:
        - Triggers when intensity > trigger_high
        - Clears when intensity < trigger_low

        Args:
            intensity: Current RMS intensity (0.0 - 1.0).

        Returns:
            Current status object.
        """
        now = time.time()
        previous_state = self._state

        if self._state == AlertState.IDLE:
            if intensity > self.config.trigger_high:
                self._transition_to(AlertState.TRIGGERED, now, intensity)

        elif self._state == AlertState.TRIGGERED:
            if intensity < self.config.trigger_low:
                self._transition_to(AlertState.COOLDOWN, now, intensity)
            else:
                # Still triggered, update timestamp (keep alive)
                self._last_trigger_time = now

        elif self._state == AlertState.COOLDOWN:
            if intensity > self.config.trigger_high:
                # Re-trigger immediately
                self._transition_to(AlertState.TRIGGERED, now, intensity)
            elif now - self._cooldown_start_time > self.config.cooldown_s:
                self._transition_to(AlertState.IDLE, now, intensity)

        # Write status file for overlay controller
        status = AlertStatus(
            system_status="ok",
            alert_state=self._state.value,
            alert_active=(self._state == AlertState.TRIGGERED),
            timestamp=now,
            intensity=intensity,
        )
        self._write_status(status)

        return status

    def _transition_to(self, new_state: AlertState, now: float, intensity: float) -> None:
        """Handle state transition side effects."""
        LOGGER.info(f"Alert State: {self._state.value} -> {new_state.value} (intensity={intensity:.3f})")

        if new_state == AlertState.TRIGGERED:
            self._handle_trigger(intensity)
            self._last_trigger_time = now

        elif new_state == AlertState.COOLDOWN:
            self._handle_cooldown()
            self._cooldown_start_time = now

        elif new_state == AlertState.IDLE:
            self._handle_idle()

        self._state = new_state

    def _write_status(self, status: AlertStatus) -> None:
        """Write status to JSON file."""
        try:
            temp_file = self.config.status_file.with_suffix(".tmp")
            with open(temp_file, "w") as f:
                json.dump(asdict(status), f)
            temp_file.replace(self.config.status_file)
        except OSError as e:
            LOGGER.error(f"Failed to write status file: {e}")

    # Side effects (Recording / Screenshots)
    # These are placeholders for now to be filled in with actual FFmpeg calls

    def _handle_trigger(self, intensity: float) -> None:
        """Called when alert is triggered."""
        LOGGER.info("🚨 ALERT TRIGGERED - Starting recording/screenshot")
        if self.config.screenshot_on_peak:
            self._capture_screenshot()
        self._start_recording()

    def _handle_cooldown(self) -> None:
        """Called when alert enters cooldown (intensity drops)."""
        LOGGER.info("Alert signal dropped - entering cooldown")

    def _handle_idle(self) -> None:
        """Called when alert clears completely."""
        LOGGER.info("Alert cleared - saving recording")
        self._stop_recording()

    def _capture_screenshot(self) -> None:
        """Capture screenshot from video stream (Stub)."""
        # TODO: Implement ffmpeg screenshot capture
        # ffmpeg -y -i rtsp://... -vframes 1 ...
        LOGGER.debug(f"Would capture screenshot to {self.config.screenshots_dir}")

    def _start_recording(self) -> None:
        """Start video recording (Stub)."""
        # TODO: Implement ffmpeg recording
        LOGGER.debug(f"Would start recording to {self.config.clips_dir}")

    def _stop_recording(self) -> None:
        """Stop video recording (Stub)."""
        LOGGER.debug("Would stop recording")
