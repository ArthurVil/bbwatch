"""Alert system with hysteresis logic and action handling.

Manages the alert state machine to prevent rapid oscillations (hysteresis),
writes status for the overlay, and handles side effects like recording clips.
"""

import enum
import json
import logging
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime

from bbwatch.config import AlertConfig
from bbwatch.recording import FFmpegRecorder, Recorder

LOGGER = logging.getLogger(__name__)


def _timestamp_filename() -> str:
    """Generate timestamp string for filenames (YYYYMMDD_HHMMSS)."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


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

    def __init__(self, config: AlertConfig, recorder: Recorder | None = None):
        """Initialize alert manager.

        Args:
            config: Alert configuration.
            recorder: Video recorder; defaults to FFmpegRecorder using config.stream_url.
        """
        self.config = config
        self._state = AlertState.IDLE
        self._last_trigger_time = 0.0
        self._cooldown_start_time = 0.0
        self._current_intensity = 0.0
        self._last_latency_ms: float | None = None
        self._recorder: Recorder = recorder if recorder is not None else FFmpegRecorder(config.stream_url)

        # Pre-create output directories
        self.config.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.config.clips_dir.mkdir(parents=True, exist_ok=True)

    @property
    def current_intensity(self) -> float:
        """Get the most recent audio intensity."""
        return self._current_intensity

    @property
    def alert_active(self) -> bool:
        """Check if alert is currently triggered."""
        return self._state == AlertState.TRIGGERED

    @property
    def current_state(self) -> AlertState:
        """Get current alert state."""
        return self._state

    @property
    def last_latency_ms(self) -> float | None:
        """Milliseconds between segment close and this alert update. None until first segment."""
        return self._last_latency_ms

    def process_intensity(self, intensity: float, capture_ts: float | None = None) -> AlertStatus:
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
        self._current_intensity = intensity
        if capture_ts is not None:
            self._last_latency_ms = (now - capture_ts) * 1000.0

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

    def _handle_trigger(self, intensity: float) -> None:
        """Called when alert is triggered."""
        LOGGER.info("🚨 ALERT TRIGGERED - Starting recording/screenshot")

        if self.config.screenshot_on_peak:
            threading.Thread(target=self._take_screenshot, daemon=True).start()

        if not self._recorder.is_recording():
            output_path = self.config.clips_dir / f"{_timestamp_filename()}.mp4"
            try:
                self._recorder.start_recording(output_path, self.config.record_clip_s)
                LOGGER.info(f"Recording started: {output_path.name}")
            except RuntimeError:
                LOGGER.warning("Recording already in progress")
            except FileNotFoundError as e:
                LOGGER.error(f"Recording failed (ffmpeg not found): {e}")

    def _handle_cooldown(self) -> None:
        """Called when alert enters cooldown (intensity drops)."""
        LOGGER.info("Alert signal dropped - entering cooldown")

    def _handle_idle(self) -> None:
        """Called when alert clears completely."""
        LOGGER.info("Alert cleared - stopping recording")
        self._recorder.stop_recording()

    def _take_screenshot(self) -> None:
        output_path = self.config.screenshots_dir / f"{_timestamp_filename()}.jpg"
        try:
            self._recorder.capture_frame(output_path)
            LOGGER.info(f"Screenshot saved: {output_path.name}")
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as e:
            LOGGER.error(f"Screenshot failed: {type(e).__name__}: {e}")
