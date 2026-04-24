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

    def __init__(self, config: AlertConfig):
        """Initialize alert manager.

        Args:
            config: Alert configuration.
        """
        self.config = config
        self._state = AlertState.IDLE
        self._last_trigger_time = 0.0
        self._cooldown_start_time = 0.0
        self._current_intensity = 0.0
        self._recording_process: subprocess.Popen | None = None
        self._recording_lock = threading.Lock()

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
        self._current_intensity = intensity

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
            threading.Thread(target=self._capture_screenshot, daemon=True).start()
        self._start_recording()

    def _handle_cooldown(self) -> None:
        """Called when alert enters cooldown (intensity drops)."""
        LOGGER.info("Alert signal dropped - entering cooldown")

    def _handle_idle(self) -> None:
        """Called when alert clears completely."""
        LOGGER.info("Alert cleared - saving recording")
        self._stop_recording()

    def _capture_screenshot(self) -> None:
        """Capture screenshot from video stream via FFmpeg."""
        output_path = self.config.screenshots_dir / f"{_timestamp_filename()}.jpg"

        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-rtsp_transport",
                    "tcp",
                    "-i",
                    self.config.stream_url,
                    "-vframes",
                    "1",
                    "-q:v",
                    "5",
                    str(output_path),
                ],
                timeout=10,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            LOGGER.info(f"Screenshot saved: {output_path}")
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as e:
            LOGGER.error(f"Failed to capture screenshot: {e}")

    def _start_recording(self) -> None:
        """Start video recording via FFmpeg in background thread."""
        with self._recording_lock:
            if self._recording_process is not None:
                LOGGER.warning("Recording already in progress, ignoring start request")
                return

            output_path = self.config.clips_dir / f"{_timestamp_filename()}.mp4"

            try:
                self._recording_process = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-y",
                        "-rtsp_transport",
                        "tcp",
                        "-i",
                        self.config.stream_url,
                        "-t",
                        str(int(self.config.record_clip_s)),
                        "-c",
                        "copy",
                        str(output_path),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                LOGGER.info(f"Recording started: {output_path}")
            except FileNotFoundError as e:
                LOGGER.error(f"Failed to start recording (ffmpeg not found): {e}")
                self._recording_process = None

    def _stop_recording(self) -> None:
        """Stop video recording gracefully."""
        with self._recording_lock:
            if self._recording_process is None:
                return

            try:
                self._recording_process.terminate()
                self._recording_process.wait(timeout=5)
                LOGGER.info("Recording stopped")
            except subprocess.TimeoutExpired:
                LOGGER.warning("Recording process did not terminate in time, killing")
                self._recording_process.kill()
                self._recording_process.wait()
            finally:
                self._recording_process = None
