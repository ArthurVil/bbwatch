"""Audio capture using FFmpeg with sliding window segmentation.

Manages multiple FFmpeg processes to create overlapping audio segments
for continuous analysis with minimal latency.
"""

import logging
import subprocess
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class AudioCapture:
    """Captures audio from ALSA device using FFmpeg.

    Creates .wav segments at regular intervals for processing
    by the cry detector.
    """

    def __init__(
        self,
        device: str,
        output_dir: Path,
        segment_duration: float = 3.0,
        sample_rate: int = 16000,
        channels: int = 1,
        prefix: str = "seg",
    ) -> None:
        """Initialize audio capture.

        Args:
            device: ALSA device identifier (e.g., 'hw:1,0').
            output_dir: Directory to write .wav segments.
            segment_duration: Duration of each segment in seconds.
            sample_rate: Audio sample rate in Hz.
            channels: Number of audio channels (1=mono, 2=stereo).
            prefix: Filename prefix for segments.
        """
        self.device = device
        self.output_dir = Path(output_dir)
        self.segment_duration = segment_duration
        self.sample_rate = sample_rate
        self.channels = channels
        self.prefix = prefix

        self._process: subprocess.Popen | None = None
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._restart_count = 0
        self._max_restarts = 10

    def _build_command(self) -> list[str]:
        """Build the FFmpeg command for audio capture.

        Returns:
            Command as list of strings.
        """
        output_pattern = str(self.output_dir / f"{self.prefix}_%05d.wav")

        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            # Input: ALSA device
            "-f",
            "alsa",
            "-i",
            self.device,
            # Audio settings
            "-ac",
            str(self.channels),
            "-ar",
            str(self.sample_rate),
            # Output: segmented WAV files
            "-f",
            "segment",
            "-segment_time",
            str(self.segment_duration),
            "-segment_format",
            "wav",
            "-y",  # Overwrite output files
            output_pattern,
        ]

    def start(self) -> None:
        """Start audio capture.

        Raises:
            RuntimeError: If capture is already running.
        """
        if self.is_running():
            raise RuntimeError("Audio capture already running")

        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._stop_event.clear()
        self._restart_count = 0
        self._start_process()

        # Start monitor thread
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="AudioCaptureMonitor",
            daemon=True,
        )
        self._monitor_thread.start()

        LOGGER.info(f"Audio capture started: device={self.device}, segment={self.segment_duration}s")

    def _start_process(self) -> None:
        """Start the FFmpeg process."""
        cmd = self._build_command()
        LOGGER.debug(f"Starting FFmpeg: {' '.join(cmd)}")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise RuntimeError("FFmpeg not found - please install ffmpeg") from e

    def stop(self) -> None:
        """Stop audio capture gracefully."""
        self._stop_event.set()

        if self._process is not None:
            LOGGER.debug("Terminating FFmpeg process")
            self._process.terminate()
            try:
                self._process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                LOGGER.warning("FFmpeg did not terminate, killing")
                self._process.kill()
            self._process = None

        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2.0)
            self._monitor_thread = None

        LOGGER.info("Audio capture stopped")

    def is_running(self) -> bool:
        """Check if capture is running.

        Returns:
            True if FFmpeg process is alive.
        """
        return self._process is not None and self._process.poll() is None

    def _monitor_loop(self) -> None:
        """Monitor FFmpeg process and restart if needed."""
        while not self._stop_event.is_set():
            time.sleep(1.0)

            if self._stop_event.is_set():
                break

            if not self.is_running():
                # Process died unexpectedly
                exit_code = self._process.returncode if self._process else -1
                stderr = ""
                if self._process and self._process.stderr:
                    stderr = self._process.stderr.read().decode()

                LOGGER.error(f"FFmpeg process died (exit={exit_code}): {stderr[:200]}")

                if self._restart_count < self._max_restarts:
                    self._restart_count += 1
                    LOGGER.warning(f"Restarting FFmpeg (attempt {self._restart_count})")
                    time.sleep(1.0)  # Brief delay before restart
                    try:
                        self._start_process()
                    except RuntimeError as e:
                        LOGGER.error(f"Failed to restart FFmpeg: {e}")
                else:
                    LOGGER.critical(f"Max restart attempts ({self._max_restarts}) exceeded")
                    break

    def __enter__(self) -> "AudioCapture":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """Context manager exit."""
        self.stop()


class SlidingWindowCapture:
    """Manages multiple AudioCapture instances for sliding window.

    Creates overlapping segments by running offset capture processes.
    For example, with 3s segments and 1s overlap:
    - Process A: segments at t=0, t=3, t=6...
    - Process B: segments at t=1, t=4, t=7...
    - Process C: segments at t=2, t=5, t=8...

    This gives us a new segment every 1 second.
    """

    def __init__(
        self,
        device: str,
        output_dir: Path,
        segment_duration: float = 3.0,
        overlap: float = 1.0,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        """Initialize sliding window capture.

        Args:
            device: ALSA device identifier.
            output_dir: Directory for .wav segments.
            segment_duration: Duration of each segment.
            overlap: Overlap between segments (determines new segment rate).
            sample_rate: Audio sample rate.
            channels: Number of channels.
        """
        self.device = device
        self.output_dir = Path(output_dir)
        self.segment_duration = segment_duration
        self.overlap = overlap
        self.sample_rate = sample_rate
        self.channels = channels

        # Calculate number of processes needed
        # stride = segment_duration - overlap
        # e.g., 3s segments with 2s overlap = 1s stride = 3 processes
        stride = segment_duration - overlap
        if stride <= 0:
            raise ValueError("overlap must be less than segment_duration")

        self.n_processes = int(segment_duration / stride)
        self._captures: list[AudioCapture] = []
        self._started = False

        LOGGER.info(f"SlidingWindowCapture: {self.n_processes} processes, new segment every {stride}s")

    def start(self) -> None:
        """Start all capture processes with staggered timing."""
        if self._started:
            raise RuntimeError("Already started")

        self.output_dir.mkdir(parents=True, exist_ok=True)

        stride = self.segment_duration - self.overlap

        for i in range(self.n_processes):
            capture = AudioCapture(
                device=self.device,
                output_dir=self.output_dir,
                segment_duration=self.segment_duration,
                sample_rate=self.sample_rate,
                channels=self.channels,
                prefix=f"seg_{chr(ord('a') + i)}",  # seg_a, seg_b, seg_c...
            )

            if i > 0:
                # Stagger start times
                time.sleep(stride)

            capture.start()
            self._captures.append(capture)

        self._started = True
        LOGGER.info(f"Started {len(self._captures)} capture processes")

    def stop(self) -> None:
        """Stop all capture processes."""
        for capture in self._captures:
            try:
                capture.stop()
            except Exception as e:
                LOGGER.error(f"Error stopping capture: {e}")

        self._captures.clear()
        self._started = False
        LOGGER.info("All capture processes stopped")

    def is_running(self) -> bool:
        """Check if at least one capture is running."""
        return any(c.is_running() for c in self._captures)

    def __enter__(self) -> "SlidingWindowCapture":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """Context manager exit."""
        self.stop()
