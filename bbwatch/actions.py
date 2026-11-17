"""Alert actions: the side effects of an alert, chosen once at startup.

``AlertManager`` only decides *when* an alert is on; ``AlertActions`` decides
*what happens* then, from ``alerts.actions`` in config.yaml:

- ``screenshot``: one JPEG when the alert triggers
- ``clip``: record from trigger until the alert clears, at least
  ``record_min_s`` and capped at ``record_max_s`` per file (rolls over)
- ``destination``: where both go — ``local`` (data/clips, data/screenshots),
  ``cloud`` (data/outbox, emptied to Google Drive by the host upload timer
  in deploy/) or ``both`` (local, plus a hard link in the outbox)

The live overlay shows the alert in every configuration. With neither
screenshot nor clip enabled, no recorder is created and FFmpeg never runs.
bbwatch itself never uploads: the outbox is just a directory.
"""

import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from bbwatch.config import AlertConfig, AlertDestination
from bbwatch.recording import FFmpegRecorder, Recorder, recover_partial_clips

LOGGER = logging.getLogger(__name__)

# Minimum gap between clip start attempts. Rollover is re-checked on every
# state update (~20 Hz from motion); without this a dead stream, where
# ffmpeg exits immediately, would respawn it on every call.
RECORD_RETRY_S = 5.0

CLIPS = "clips"
SCREENSHOTS = "screenshots"


def _dated_path(base_dir: Path, suffix: str) -> Path:
    """Output path grouped by day: ``base_dir/YYYY-MM-DD/HHMMSS<suffix>``."""
    now = datetime.now()
    day_dir = base_dir / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    return day_dir / f"{now.strftime('%H%M%S')}{suffix}"


class AlertActions:
    """Runs the configured alert side effects. Not thread-safe on its own:
    ``AlertManager`` calls it under its state lock.

    Times (``now``) come from the caller so the whole alert pipeline shares
    one clock.
    """

    def __init__(self, config: AlertConfig, recorder: Recorder | None = None):
        """Initialize from config.

        Args:
            config: Alert configuration (``config.actions`` picks the actions).
            recorder: Video recorder; defaults to an FFmpegRecorder on
                config.stream_url, created only if an action needs one.
        """
        self.config = config
        self.actions = config.actions
        destination = self.actions.destination
        self._clips_dir = config.outbox_dir / CLIPS if destination == AlertDestination.CLOUD else config.clips_dir
        self._screenshots_dir = (
            config.outbox_dir / SCREENSHOTS if destination == AlertDestination.CLOUD else config.screenshots_dir
        )
        self._recorder: Recorder | None = None
        if self.actions.clip or self.actions.screenshot:
            self._recorder = recorder if recorder is not None else FFmpegRecorder(config.stream_url)

        self._last_record_attempt = float("-inf")
        self._clip_started_at = float("-inf")
        # Set when the alert clears before record_min_s: stop the clip then
        self._stop_due_at: float | None = None

        if self.actions.clip:
            # Clips interrupted by a crash or restart, in either location
            for clips_dir in {self._clips_dir, config.clips_dir, config.outbox_dir / CLIPS}:
                recover_partial_clips(clips_dir)

        LOGGER.info(f"Alert actions: {self.describe()}")

    def describe(self) -> str:
        """One-line summary of the active actions, for the startup log."""
        enabled = [name for name, on in ((SCREENSHOTS, self.actions.screenshot), (CLIPS, self.actions.clip)) if on]
        if not enabled:
            return "overlay only (no screenshots, no clips)"
        return f"overlay + {' + '.join(enabled)} -> {self.actions.destination.value}"

    @property
    def recording_error(self) -> str | None:
        """Last clip recording failure, None if the most recent clip succeeded (or clips are off)."""
        if not self.actions.clip or self._recorder is None:
            return None
        return self._recorder.last_error()

    # --- events from the alert state machine ---------------------------------

    def on_trigger(self, now: float) -> None:
        """The alert just turned on (from idle or cooldown)."""
        if self.actions.screenshot:
            threading.Thread(target=self._take_screenshot, name="AlertScreenshot", daemon=True).start()
        if self.actions.clip:
            # Re-triggered before a short clip's minimum length ran out: keep it going
            self._stop_due_at = None
            self._ensure_recording(now)

    def on_active(self, now: float) -> None:
        """The alert is still on: roll over a clip that hit record_max_s (or failed)."""
        if self.actions.clip:
            self._ensure_recording(now)

    def on_clear(self, now: float) -> None:
        """The alert cleared completely (cooldown over)."""
        if not self.actions.clip or self._recorder is None:
            return
        stop_at = self._clip_started_at + self.config.record_min_s
        if self._recorder.is_recording() and now < stop_at:
            LOGGER.info(f"Alert cleared - recording continues {stop_at - now:.0f} s more (record_min_s)")
            self._stop_due_at = stop_at
            return
        LOGGER.info("Alert cleared - stopping recording")
        self._recorder.stop_recording()

    def tick(self, now: float) -> None:
        """Called on every state update: stops a short clip once record_min_s is reached."""
        if self._stop_due_at is not None and now >= self._stop_due_at and self._recorder is not None:
            self._stop_due_at = None
            LOGGER.info("Minimum clip length reached - stopping recording")
            self._recorder.stop_recording()

    def shutdown(self) -> None:
        """Finalize any in-progress clip (called on monitor stop)."""
        self._stop_due_at = None
        if self._recorder is not None:
            self._recorder.stop_recording()

    # --- actions ---------------------------------------------------------------

    def _ensure_recording(self, now: float) -> None:
        """Start a clip unless one is running or the last attempt was too recent."""
        assert self._recorder is not None
        if self._recorder.is_recording() or now - self._last_record_attempt < RECORD_RETRY_S:
            return
        self._last_record_attempt = now
        output_path = _dated_path(self._clips_dir, ".mp4")
        on_saved = self._link_clip_to_outbox if self.actions.destination == AlertDestination.BOTH else None
        try:
            self._recorder.start_recording(output_path, self.config.record_max_s, on_saved=on_saved)
            self._clip_started_at = now
            LOGGER.info(f"Recording started: {output_path}")
        except RuntimeError:
            LOGGER.warning("Recording already in progress")
        except FileNotFoundError as e:
            LOGGER.error(f"Recording failed (ffmpeg not found): {e}")

    def _take_screenshot(self) -> None:
        assert self._recorder is not None
        output_path = _dated_path(self._screenshots_dir, ".jpg")
        try:
            self._recorder.capture_frame(output_path)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as e:
            LOGGER.error(f"Screenshot failed: {type(e).__name__}: {e}")
            return
        LOGGER.info(f"Screenshot saved: {output_path}")
        if self.actions.destination == AlertDestination.BOTH:
            self._link_to_outbox(output_path, self.config.screenshots_dir, SCREENSHOTS)

    def _link_clip_to_outbox(self, clip: Path) -> None:
        self._link_to_outbox(clip, self.config.clips_dir, CLIPS)

    def _link_to_outbox(self, path: Path, local_root: Path, kind: str) -> None:
        """Queue a finished local file for upload, keeping the local copy.

        A hard link costs no space and the uploader deleting it leaves the
        local file intact. Falls back to a copy across filesystems.
        """
        target = self.config.outbox_dir / kind / path.relative_to(local_root)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(path, target)
            except OSError:
                shutil.copy2(path, target)
        except OSError as e:
            LOGGER.error(f"Could not queue {path.name} for upload: {e}")
            return
        LOGGER.debug(f"Queued for upload: {target}")
