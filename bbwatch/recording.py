"""Video recording abstractions.

Provides a Protocol for capturing frames and recording clips, with an
FFmpegRecorder implementation that reads from an RTSP stream.
"""

import logging
import subprocess
from pathlib import Path
from typing import Protocol

LOGGER = logging.getLogger(__name__)


class Recorder(Protocol):
    """Protocol for capturing frames and recording video clips."""

    def capture_frame(self, output_path: Path) -> None:
        """Capture a single frame to JPEG.

        Raises:
            subprocess.TimeoutExpired: Capture took too long.
            subprocess.CalledProcessError: FFmpeg returned non-zero exit.
            FileNotFoundError: FFmpeg not installed.
        """
        ...

    def start_recording(self, output_path: Path, duration_sec: float) -> None:
        """Start a background clip recording.

        Raises:
            RuntimeError: Already recording.
            FileNotFoundError: FFmpeg not installed.
        """
        ...

    def is_recording(self) -> bool:
        """Return True if a clip recording is in progress."""
        ...

    def stop_recording(self) -> None:
        """Stop any in-progress recording gracefully."""
        ...


class FFmpegRecorder:
    """Records video from an RTSP stream via FFmpeg subprocesses."""

    def __init__(self, stream_url: str) -> None:
        self.stream_url = stream_url
        self._process: subprocess.Popen | None = None

    def capture_frame(self, output_path: Path) -> None:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", self.stream_url,
                "-vframes", "1",
                "-q:v", "5",
                str(output_path),
            ],
            timeout=10,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def start_recording(self, output_path: Path, duration_sec: float) -> None:
        if self._process is not None:
            raise RuntimeError("Already recording")

        self._process = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-rtsp_transport", "tcp",
                "-i", self.stream_url,
                "-t", str(int(duration_sec)),
                "-c", "copy",
                str(output_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def is_recording(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop_recording(self) -> None:
        if self._process is None:
            return

        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            LOGGER.warning("Recording process did not terminate, killing")
            self._process.kill()
            self._process.wait()
        finally:
            self._process = None


class MockRecorder:
    """No-op recorder for testing without FFmpeg or a live stream."""

    def __init__(self) -> None:
        self._recording = False
        self.capture_frame_calls: list[Path] = []
        self.start_recording_calls: list[tuple[Path, float]] = []
        self.stop_recording_calls: int = 0

    def capture_frame(self, output_path: Path) -> None:
        self.capture_frame_calls.append(output_path)

    def start_recording(self, output_path: Path, duration_sec: float) -> None:
        if self._recording:
            raise RuntimeError("Already recording")
        self._recording = True
        self.start_recording_calls.append((output_path, duration_sec))

    def is_recording(self) -> bool:
        return self._recording

    def stop_recording(self) -> None:
        self._recording = False
        self.stop_recording_calls += 1
