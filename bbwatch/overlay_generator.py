"""Dynamic overlay generator.

Generates real-time video overlays with status information, timestamps,
and visualization plots, writing directly to a named pipe for FFmpeg.
"""

import logging
import os
import select
import threading
import time
from collections import deque
from pathlib import Path
from typing import NamedTuple

import cv2
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

LOGGER = logging.getLogger(__name__)


class OverlayState(NamedTuple):
    """Current state for overlay generation."""

    motion_detected: bool
    audio_alert: bool
    motion_level: float
    audio_level: float
    latency_ms: float | None = None


class OverlayGenerator:
    """Generates and writes overlay frames to a pipe."""

    # Layout Constraints
    WIDTH: int = 640
    HEIGHT: int = 480
    PLOT_WIDTH: int = 200
    PLOT_HEIGHT: int = 80
    MARGIN: int = 10

    # Colors (BGRA)
    COLOR_RED: tuple[int, int, int, int] = (0, 0, 255, 100)
    COLOR_YELLOW: tuple[int, int, int, int] = (0, 255, 255, 100)
    COLOR_PURPLE: tuple[int, int, int, int] = (255, 0, 128, 100)
    COLOR_TRANSPARENT: tuple[int, int, int, int] = (0, 0, 0, 0)
    COLOR_BG_PLOT: tuple[int, int, int, int] = (0, 0, 0, 100)
    COLOR_LINE_MOTION: tuple[int, int, int, int] = (0, 255, 0, 255)
    COLOR_LINE_AUDIO: tuple[int, int, int, int] = (255, 255, 0, 255)
    COLOR_TEXT_TIMESTAMP: tuple[int, int, int, int] = (255, 255, 255, 255)
    COLOR_TEXT_INFO: tuple[int, int, int, int] = (200, 200, 200, 255)

    def __init__(
        self,
        pipe_path: Path,
        width: int = 640,
        height: int = 480,
        fps: int = 5,
        history_len: int = 50,
    ) -> None:
        """Initialize overlay generator.

        Args:
            pipe_path: Path to the named pipe.
            width: Image width.
            height: Image height.
            fps: Target frames per second.
            history_len: Number of data points to keep for plotting.
        """
        self.pipe_path = Path(pipe_path)
        self.width = width
        self.height = height
        self.fps = fps
        self.history_len = history_len
        self.running = False

        # History for plotting
        self.motion_history = deque([0.0] * self.history_len, maxlen=self.history_len)
        self.audio_history = deque([0.0] * self.history_len, maxlen=self.history_len)

        # Current state
        self._state = OverlayState(False, False, 0.0, 0.0)
        self._lock = threading.Lock()

        # Plotting resources
        self.fig, self.ax = plt.subplots(figsize=(3, 1), dpi=100)
        self.canvas = FigureCanvas(self.fig)
        self._setup_plot()

    def _setup_plot(self) -> None:
        """Configure matplotlib figure."""
        self.fig.patch.set_alpha(0.5)  # Transparent figure background
        self.ax.patch.set_alpha(0.0)  # Transparent axis background
        self.ax.set_ylim(0, 100)
        self.ax.axis("off")  # Hide axes
        self.fig.tight_layout(pad=0)

    def update_state(
        self,
        motion_detected: bool,
        audio_alert: bool,
        motion_level: float,
        audio_level: float,
        latency_ms: float | None = None,
    ) -> None:
        """Update current system state thread-safely."""
        with self._lock:
            self._state = OverlayState(motion_detected, audio_alert, motion_level, audio_level, latency_ms)
            self.motion_history.append(motion_level)
            self.audio_history.append(min(audio_level * 1000, 100))

    def _get_status_color(self) -> tuple[int, int, int, int]:
        """Get background color based on state (BGRA)."""
        with self._lock:
            state = self._state

        # Red: Motion + Noise
        if state.motion_detected and state.audio_alert:
            return self.COLOR_RED

        # Yellow: Motion only
        if state.motion_detected:
            return self.COLOR_YELLOW

        # Purple: Noise only
        if state.audio_alert:
            return self.COLOR_PURPLE

        # Transparent: Normal
        return self.COLOR_TRANSPARENT

    def _generate_plot_image(self) -> np.ndarray:
        """Generate plot image using matplotlib."""
        # ... logic unchanged as this method is unused/deprecated in favor of cv2 ...
        self.ax.clear()
        self.ax.axis("off")
        self.ax.set_ylim(0, 100)
        # ... keeping implementation minimal or just returning empty if unused
        return np.zeros((self.height, self.width, 4), dtype=np.uint8)

    def _draw_plot_cv2(self, img: np.ndarray) -> None:
        """Draw plot using OpenCV (Faster/Simpler fallback)."""
        h, w = img.shape[:2]
        x_start = w - self.PLOT_WIDTH - self.MARGIN
        y_start = h - self.PLOT_HEIGHT - self.MARGIN

        # Background
        cv2.rectangle(
            img, (x_start, y_start), (x_start + self.PLOT_WIDTH, y_start + self.PLOT_HEIGHT), self.COLOR_BG_PLOT, -1
        )

        with self._lock:
            hist_m = list(self.motion_history)
            hist_a = list(self.audio_history)

        points_m = []
        points_a = []

        for i, val in enumerate(hist_m):
            x = x_start + int(i * self.PLOT_WIDTH / self.history_len)
            y = y_start + self.PLOT_HEIGHT - int((val / 100.0) * self.PLOT_HEIGHT)
            points_m.append((x, y))

        for i, val in enumerate(hist_a):
            x = x_start + int(i * self.PLOT_WIDTH / self.history_len)
            y = y_start + self.PLOT_HEIGHT - int((val / 100.0) * self.PLOT_HEIGHT)
            points_a.append((x, y))

        if len(points_m) > 1:
            cv2.polylines(img, [np.array(points_m)], False, self.COLOR_LINE_MOTION, 1)

        if len(points_a) > 1:
            cv2.polylines(img, [np.array(points_a)], False, self.COLOR_LINE_AUDIO, 1)

        cv2.putText(img, "Motion", (x_start, y_start - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, self.COLOR_LINE_MOTION, 1)
        cv2.putText(img, "Audio", (x_start + 60, y_start - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, self.COLOR_LINE_AUDIO, 1)

    def generate_frame(self) -> bytes:
        """Generate a single overlay frame."""
        # LOGGER.debug("Generating overlay frame")
        # Create transparent base
        img = np.zeros((self.height, self.width, 4), dtype=np.uint8)

        # Apply status color tint
        bg_color = self._get_status_color()
        if bg_color[3] > 0:
            # Create tint layer
            tint = np.full_like(img, bg_color)
            # Simple copy since base is empty
            img = tint

        # Add timestamp
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(img, ts, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, self.COLOR_TEXT_TIMESTAMP, 2)

        with self._lock:
            info = f"Motion: {self._state.motion_level:.1f}%  Audio: {self._state.audio_level:.3f}"
            latency_ms = self._state.latency_ms

        cv2.putText(img, info, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, self.COLOR_TEXT_INFO, 1)

        if latency_ms is not None:
            latency_text = f"Detect: {latency_ms:.0f}ms"
            cv2.putText(img, latency_text, (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.6, self.COLOR_TEXT_INFO, 1)

        # Draw plot (OpenCV is much faster/stable for this overlay use-case than converting mpl figures)
        self._draw_plot_cv2(img)

        # Convert BGRA (OpenCV) to RGBA (FFmpeg)
        img_rgba = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
        return img_rgba.tobytes()

    def _write_frame(self, fd: int, frame_data: bytes) -> None:
        """Write one complete frame to the FIFO, handling short writes.

        A frame (width*height*4 bytes) is far larger than the pipe buffer
        (64 KiB on Linux), so a single os.write() on the non-blocking fd is
        always partial. Once a frame is started it must be written to
        completion: FFmpeg's rawvideo demuxer has no framing markers, so a
        partial frame desyncs the stream permanently.

        Raises:
            BrokenPipeError: The reader disconnected mid-frame.
        """
        view = memoryview(frame_data)
        while view and self.running:
            try:
                written = os.write(fd, view)
                view = view[written:]
            except BlockingIOError:
                # Pipe buffer full — wait until the reader drains it.
                select.select([], [fd], [], 1.0)

    def run_loop(self) -> None:
        """Run the generation loop (blocking)."""
        LOGGER.info(f"Starting overlay loop writing to {self.pipe_path}")

        # Ensure pipe exists
        if not self.pipe_path.exists():
            try:
                if self.pipe_path.exists():
                    self.pipe_path.unlink()  # Cleanup if regular file
                os.mkfifo(str(self.pipe_path))
                self.pipe_path.chmod(0o666)
            except OSError as e:
                LOGGER.error(f"Failed to create pipe: {e}")
                # Don't crash, maybe it already exists or loop will retry

        self.running = True
        frame_interval = 1.0 / self.fps
        while self.running:
            try:
                LOGGER.info("Opening overlay pipe (waiting for reader)...")
                # O_WRONLY | O_NONBLOCK: open returns immediately if no reader yet,
                # raising OSError(ENXIO). We retry until go2rtc connects.
                fd = -1
                while self.running:
                    try:
                        fd = os.open(str(self.pipe_path), os.O_WRONLY | os.O_NONBLOCK)
                        break
                    except OSError:
                        time.sleep(0.5)

                if fd == -1:
                    continue

                LOGGER.info("Overlay pipe connected.")
                last_log = time.time()

                while self.running:
                    start_time = time.time()
                    try:
                        frame_data = self.generate_frame()

                        # Check writability before starting a frame (100ms timeout).
                        # select returns (rlist, wlist, xlist) — the fd is in the
                        # WRITE list. If the reader is not keeping up, drop the
                        # whole frame here; a frame must never be started and
                        # abandoned (rawvideo has no framing markers).
                        _, writable, _ = select.select([], [fd], [], 0.1)
                        if writable:
                            try:
                                self._write_frame(fd, frame_data)
                            except BrokenPipeError:
                                LOGGER.warning("Pipe broken (reader disconnected), reconnecting...")
                                break

                        if time.time() - last_log > 5.0:
                            with self._lock:
                                LOGGER.info(f"Overlay loop alive - state: motion={self._state.motion_level:.1f}")
                            last_log = time.time()

                    except Exception as inner_e:
                        LOGGER.error(f"Error generating overlay frame: {inner_e}")
                        time.sleep(0.5)

                    elapsed = time.time() - start_time
                    delay = max(0.0, frame_interval - elapsed)
                    time.sleep(delay)

                try:
                    os.close(fd)
                except OSError:
                    pass

            except Exception as e:
                LOGGER.error(f"Overlay loop error: {e}")
                time.sleep(2)

    def start(self) -> None:
        """Start the overlay thread."""
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop the loop."""
        self.running = False
        if hasattr(self, "thread"):
            self.thread.join(timeout=1.0)
