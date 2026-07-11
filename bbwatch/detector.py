"""Cry detection using bandpass filter + RMS energy analysis.

This module implements a simple but effective baby cry detector
that uses DSP techniques rather than ML for the PoC phase.
"""

import logging
import time
from pathlib import Path
from typing import NamedTuple, cast

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt
from watchdog.events import FileSystemEvent, FileSystemEventHandler

from bbwatch.alert import AlertManager
from bbwatch.config import DetectionConfig
from bbwatch.latency import LatencyTracker, StageTimer
from bbwatch.overlay_generator import OverlayGenerator

LOGGER = logging.getLogger(__name__)


class DetectionResult(NamedTuple):
    """Result of cry detection analysis."""

    is_cry: bool
    filtered_rms: float
    active_ratio: float
    duration_s: float


def butter_bandpass(
    lowcut: float,
    highcut: float,
    fs: int,
    order: int = 4,
) -> np.ndarray:
    """Design a Butterworth bandpass filter.

    Args:
        lowcut: Low frequency cutoff in Hz.
        highcut: High frequency cutoff in Hz.
        fs: Sample rate in Hz.
        order: Filter order (higher = sharper cutoff, more latency).

    Returns:
        Second-order sections representation of the filter.

    Raises:
        ValueError: If frequencies are invalid for the sample rate.
    """
    nyq = 0.5 * fs

    if lowcut <= 0 or highcut <= 0:
        raise ValueError("Cutoff frequencies must be positive")
    if lowcut >= highcut:
        raise ValueError(f"lowcut ({lowcut}) must be less than highcut ({highcut})")
    if highcut >= nyq:
        raise ValueError(f"highcut ({highcut}) must be less than Nyquist ({nyq})")

    low = lowcut / nyq
    high = highcut / nyq

    sos = cast(np.ndarray, butter(order, [low, high], btype="band", output="sos"))
    return sos


def detect_cry(
    wav_path: str | Path,
    lowcut: float,
    highcut: float,
    rms_threshold: float,
    min_active_ratio: float,
    window_ms: float,
) -> DetectionResult:
    """Detect baby cry in audio segment.

    Uses a bandpass filter to isolate the frequency range typical of
    baby cries (250-800 Hz fundamental), then computes RMS energy in
    sliding windows to determine activity level.

    Args:
        wav_path: Path to .wav file to analyze.
        lowcut: Low frequency cutoff (Hz) for bandpass filter.
        highcut: High frequency cutoff (Hz) for bandpass filter.
        rms_threshold: RMS threshold for considering a window as "active".
        min_active_ratio: Minimum ratio of active windows to trigger alert.
        window_ms: Window size in milliseconds for RMS computation.

    Returns:
        DetectionResult with cry detection outcome and metrics.

    Raises:
        FileNotFoundError: If wav file doesn't exist.
        RuntimeError: If audio file cannot be read.
    """
    wav_path = Path(wav_path)

    if not wav_path.exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")

    try:
        audio, sr = sf.read(wav_path)
    except Exception as e:
        raise RuntimeError(f"Failed to read audio file: {e}") from e

    # Handle stereo by averaging channels
    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)

    duration_s = len(audio) / sr

    if len(audio) == 0:
        return DetectionResult(
            is_cry=False,
            filtered_rms=0.0,
            active_ratio=0.0,
            duration_s=0.0,
        )

    # Apply bandpass filter to isolate baby cry frequencies
    sos = butter_bandpass(lowcut, highcut, sr)
    filtered = sosfilt(sos, audio)

    # Compute RMS in sliding windows
    window_size = int(sr * window_ms / 1000)
    if window_size == 0:
        window_size = 1

    n_windows = len(filtered) // window_size
    if n_windows == 0:
        n_windows = 1
        window_size = len(filtered)

    # Simplified RMS calculation
    rms_values = []
    for i in range(n_windows):
        start = i * window_size
        end = min(start + window_size, len(filtered))
        window = filtered[start:end]
        if len(window) > 0:
            rms = np.sqrt(np.mean(window**2))
            rms_values.append(rms)

    # Calculate metrics
    filtered_rms = float(np.mean(rms_values)) if rms_values else 0.0
    active_frames = sum(1 for rms in rms_values if rms > rms_threshold)
    active_ratio = active_frames / len(rms_values) if rms_values else 0.0

    is_cry = active_ratio >= min_active_ratio

    if is_cry:
        LOGGER.debug(f"Cry detected: {wav_path.name} (rms={filtered_rms:.4f}, ratio={active_ratio:.2%})")

    return DetectionResult(
        is_cry=is_cry,
        filtered_rms=filtered_rms,
        active_ratio=active_ratio,
        duration_s=duration_s,
    )


def is_segment_empty(
    wav_path: str | Path,
    silence_threshold: float = 0.001,
) -> bool:
    """Check if an audio segment is essentially silent."""
    wav_path = Path(wav_path)

    if not wav_path.exists():
        return True

    try:
        audio, _ = sf.read(wav_path)
    except Exception:
        LOGGER.warning(f"Failed to read {wav_path} for silence check")
        return True

    if audio.ndim > 1:
        audio = np.mean(audio, axis=1)

    if len(audio) == 0:
        return True

    rms = np.sqrt(np.mean(audio**2))
    return bool(rms < silence_threshold)


class SegmentHandler(FileSystemEventHandler):
    """Watchdog handler for processing new audio segments.

    Monitors a directory for new .wav files and runs cry detection.
    Results are passed to the AlertManager.
    """

    def __init__(
        self,
        config: DetectionConfig,
        alert_manager: AlertManager,
        delete_empty: bool = True,
        overlay_generator: OverlayGenerator | None = None,
        latency_report_interval_s: float = 10.0,
    ) -> None:
        """Initialize the segment handler.

        Args:
            config: Detection configuration.
            alert_manager: Manager to handle alert state and effects.
            delete_empty: Whether to delete empty segments.
            overlay_generator: Generator to visualize audio levels.
            latency_report_interval_s: Seconds between latency summaries.
        """
        super().__init__()
        self.config = config
        self.alert_manager = alert_manager
        self.delete_empty = delete_empty
        self.overlay_generator = overlay_generator
        self._latency = LatencyTracker("audio", report_interval_s=latency_report_interval_s)

    def on_closed(self, event: "FileSystemEvent") -> None:
        """Handle file close events (finished writing)."""
        if event.is_directory:
            return

        src_path = event.src_path
        if isinstance(src_path, bytes):
            src_path = src_path.decode("utf-8")

        if not src_path.endswith(".wav"):
            return

        wav_path = Path(src_path)

        if not wav_path.exists():
            return

        capture_ts = time.time()

        # Stage timing: origin = segment file mtime (when FFmpeg finished
        # writing), so "pickup" measures the watchdog notification delay.
        try:
            origin_ts: float | None = wav_path.stat().st_mtime
        except OSError:
            origin_ts = None
        timer = StageTimer(origin_ts=origin_ts)

        # Check if empty (delete if configured)
        if is_segment_empty(wav_path, self.config.silence_threshold):
            timer.mark("empty_check")
            # Update heartbeat with 0 intensity (silence)
            self.alert_manager.process_intensity(0.0, capture_ts=capture_ts)
            timer.mark("alert")

            if self.delete_empty:
                try:
                    wav_path.unlink()
                    LOGGER.debug(f"Deleted empty segment: {wav_path.name}")
                except OSError as e:
                    LOGGER.warning(f"Failed to delete empty segment: {e}")
            self._latency.record(timer)
            return
        timer.mark("empty_check")

        # Run detection
        try:
            result = detect_cry(
                wav_path,
                lowcut=self.config.bandpass_low_hz,
                highcut=self.config.bandpass_high_hz,
                rms_threshold=self.config.rms_threshold,
                min_active_ratio=self.config.min_active_ratio,
                window_ms=self.config.window_ms,
            )
        except Exception as e:
            LOGGER.error(f"Detection failed for {wav_path.name}: {e}")
            return
        timer.mark("dsp")

        self.alert_manager.process_intensity(result.filtered_rms, capture_ts=capture_ts)
        timer.mark("alert")

        # Update overlay generator if present
        if self.overlay_generator:
            self.overlay_generator.update_state(
                motion_detected=False,  # We don't know motion here
                audio_alert=self.alert_manager.alert_active,
                motion_level=0.0,  # Handled by main loop
                audio_level=result.filtered_rms,
            )
            timer.mark("overlay_state")
        self._latency.record(timer)
