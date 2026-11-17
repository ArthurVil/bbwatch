"""Alert state machine with hysteresis.

Decides when an alert is on and writes status.json for the overlay. What an
alert *does* besides showing on the overlay (screenshots, clips, where they
go) is the job of ``bbwatch.actions.AlertActions``, configured at startup.
"""

import enum
import json
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

from bbwatch.actions import AlertActions
from bbwatch.config import AlertConfig
from bbwatch.recording import Recorder

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
    trigger_source: str | None = None  # "audio" | "motion" | "both" while an input is hot


class AlertManager:
    """Manages alert state transitions and side effects.

    Two inputs drive one IDLE/TRIGGERED/COOLDOWN state machine:

    - audio: ``process_intensity`` with trigger_high/trigger_low hysteresis
      (watchdog observer thread)
    - motion: ``process_motion``, hot while at least ``motion_min_frames``
      frames in the last ``motion_window_s`` exceeded the motion threshold
      (main loop thread)

    The alert is active while either input is hot; it enters cooldown when
    both are quiet and returns to idle after ``cooldown_s``. Transitions are
    passed to an ``AlertActions`` for the configured side effects.
    """

    def __init__(self, config: AlertConfig, recorder: Recorder | None = None, actions: AlertActions | None = None):
        """Initialize alert manager.

        Args:
            config: Alert configuration.
            recorder: Video recorder for the default AlertActions (tests inject a mock).
            actions: Alert side effects; defaults to AlertActions(config.actions).
        """
        self.config = config
        self._state = AlertState.IDLE
        self._last_trigger_time = 0.0
        self._cooldown_start_time = 0.0
        self._current_intensity = 0.0
        self._last_latency_ms: float | None = None
        self._audio_hot = False
        self._motion_hot = False
        self._motion_frame_times: deque[float] = deque()
        self._last_motion_sample: int | None = None
        self._actions = actions if actions is not None else AlertActions(config, recorder)
        # Serializes state-machine updates: audio arrives on the watchdog
        # observer thread, motion on the main loop thread.
        self._state_lock = threading.RLock()
        # Guards status.json writes: process_intensity() runs on the watchdog
        # observer thread, heartbeat() on the main loop thread. Both write to
        # the same fixed temp-file path — without a lock, concurrent writes
        # could interleave or race the rename.
        self._status_lock = threading.Lock()

    @property
    def current_intensity(self) -> float:
        """Get the most recent audio intensity."""
        return self._current_intensity

    @property
    def alert_active(self) -> bool:
        """Check if alert is currently triggered (by any source)."""
        return self._state == AlertState.TRIGGERED

    @property
    def audio_active(self) -> bool:
        """True while the audio input is above its hysteresis band (cry indicator)."""
        return self._audio_hot

    @property
    def current_state(self) -> AlertState:
        """Get current alert state."""
        return self._state

    @property
    def trigger_source(self) -> str | None:
        """Which inputs are currently hot: "audio", "motion", "both" or None."""
        motion = self._motion_hot and self.config.motion_triggers_alert
        if self._audio_hot and motion:
            return "both"
        if self._audio_hot:
            return "audio"
        if motion:
            return "motion"
        return None

    @property
    def recording_error(self) -> str | None:
        """Last clip recording failure, None if the most recent clip succeeded."""
        return self._actions.recording_error

    def shutdown(self) -> None:
        """Finalize any in-progress clip (called on monitor stop)."""
        with self._state_lock:
            self._actions.shutdown()

    @property
    def last_latency_ms(self) -> float | None:
        """Milliseconds between segment close and this alert update. None until first segment."""
        return self._last_latency_ms

    def process_intensity(self, intensity: float, capture_ts: float | None = None) -> AlertStatus:
        """Update state based on new intensity measurement.

        Implements hysteresis:
        - Audio becomes hot when intensity > trigger_high
        - Audio goes quiet when intensity < trigger_low

        Args:
            intensity: Current RMS intensity (0.0 - 1.0).

        Returns:
            Current status object.
        """
        now = time.time()
        with self._state_lock:
            self._current_intensity = intensity
            if capture_ts is not None:
                self._last_latency_ms = (now - capture_ts) * 1000.0

            if intensity > self.config.trigger_high:
                self._audio_hot = True
            elif intensity < self.config.trigger_low:
                self._audio_hot = False

            self._evaluate(now)
            status = self._current_status(now)
        self._write_status(status)
        return status

    def process_motion(self, motion_detected: bool, sample_id: int | None = None) -> AlertStatus:
        """Update state from the motion detector (called at main-loop rate).

        Motion is hot while at least ``motion_min_frames`` frames within the
        last ``motion_window_s`` were above the threshold. They need not be
        consecutive — real movement flickers frame to frame — but requiring
        a few filters a single-frame lighting jump. Only state changes are
        written to status.json here — the main loop heartbeat refreshes it
        every second anyway.

        Args:
            motion_detected: Current motion level is above the motion threshold.
            sample_id: Frame sequence number from the detector. The main loop
                polls faster than the camera frame rate; a repeated id is the
                same frame and is not counted again. None counts every call.

        Returns:
            Current status object.
        """
        now = time.time()
        with self._state_lock:
            new_frame = sample_id is None or sample_id != self._last_motion_sample
            self._last_motion_sample = sample_id
            if motion_detected and new_frame:
                self._motion_frame_times.append(now)
            while self._motion_frame_times and now - self._motion_frame_times[0] > self.config.motion_window_s:
                self._motion_frame_times.popleft()
            self._motion_hot = len(self._motion_frame_times) >= self.config.motion_min_frames

            changed = self._evaluate(now)
            status = self._current_status(now)
        if changed:
            self._write_status(status)
        return status

    def heartbeat(self) -> AlertStatus:
        """Refresh status.json's timestamp without changing alert state.

        status.json is the overlay/health-check's sole liveness signal
        (`bbwatch.overlay.get_alert_state`). It used to be written only from
        `process_intensity`, so a deployment with audio disabled (camera-only)
        never updated it — the system looked permanently "unhealthy" after
        `health_timeout_s` even though it was working correctly. The main
        loop calls this periodically so liveness reflects the process, not
        audio activity specifically.
        """
        with self._state_lock:
            status = self._current_status(time.time())
        self._write_status(status)
        return status

    def _current_status(self, now: float) -> AlertStatus:
        """Build the status snapshot written to status.json."""
        return AlertStatus(
            system_status="ok",
            alert_state=self._state.value,
            alert_active=(self._state == AlertState.TRIGGERED),
            timestamp=now,
            intensity=self._current_intensity,
            trigger_source=self.trigger_source,
        )

    def _evaluate(self, now: float) -> bool:
        """Advance the state machine from the current inputs. Caller holds _state_lock.

        Returns:
            True if the state changed.
        """
        before = self._state
        active = self.trigger_source is not None

        self._actions.tick(now)

        if self._state == AlertState.IDLE:
            if active:
                self._transition_to(AlertState.TRIGGERED, now)

        elif self._state == AlertState.TRIGGERED:
            if not active:
                self._transition_to(AlertState.COOLDOWN, now)
            else:
                # Still triggered, update timestamp (keep alive)
                self._last_trigger_time = now
                # e.g. a clip that hit record_max_s rolls over
                self._actions.on_active(now)

        elif self._state == AlertState.COOLDOWN:
            if active:
                # Re-trigger immediately
                self._transition_to(AlertState.TRIGGERED, now)
            elif now - self._cooldown_start_time > self.config.cooldown_s:
                self._transition_to(AlertState.IDLE, now)

        return self._state != before

    def _transition_to(self, new_state: AlertState, now: float) -> None:
        """Handle state transition side effects."""
        LOGGER.info(
            f"Alert State: {self._state.value} -> {new_state.value} "
            f"(source={self.trigger_source}, intensity={self._current_intensity:.3f})"
        )

        if new_state == AlertState.TRIGGERED:
            self._handle_trigger(now)
            self._last_trigger_time = now

        elif new_state == AlertState.COOLDOWN:
            self._handle_cooldown()
            self._cooldown_start_time = now

        elif new_state == AlertState.IDLE:
            self._handle_idle(now)

        self._state = new_state

    def _write_status(self, status: AlertStatus) -> None:
        """Write status to JSON file."""
        try:
            temp_file = self.config.status_file.with_suffix(".tmp")
            with self._status_lock:
                with open(temp_file, "w") as f:
                    json.dump(asdict(status), f)
                temp_file.replace(self.config.status_file)
        except OSError as e:
            LOGGER.error(f"Failed to write status file: {e}")

    def _handle_trigger(self, now: float) -> None:
        """Called when alert is triggered."""
        LOGGER.info(f"🚨 ALERT TRIGGERED (source={self.trigger_source})")
        self._actions.on_trigger(now)

    def _handle_cooldown(self) -> None:
        """Called when alert enters cooldown (all inputs quiet)."""
        LOGGER.info("Alert signal dropped - entering cooldown")

    def _handle_idle(self, now: float) -> None:
        """Called when alert clears completely."""
        LOGGER.info("Alert cleared")
        self._actions.on_clear(now)
