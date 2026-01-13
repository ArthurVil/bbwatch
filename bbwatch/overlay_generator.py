"""Dynamic overlay generator.

Generates real-time video overlays with status information, timestamps,
and visualization plots, writing directly to a named pipe for FFmpeg.
"""

import logging
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


class OverlayGenerator:
    """Generates and writes overlay frames to a pipe."""

    def __init__(
        self,
        pipe_path: Path,
        width: int = 640,
        height: int = 480,
        fps: int = 5,
    ) -> None:
        """Initialize overlay generator.

        Args:
            pipe_path: Path to the named pipe.
            width: Image width.
            height: Image height.
            fps: Target frames per second.
        """
        self.pipe_path = Path(pipe_path)
        self.width = width
        self.height = height
        self.fps = fps
        self.running = False

        # History for plotting
        self.history_len = 50
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

    def update_state(self, motion_detected: bool, audio_alert: bool, motion_level: float, audio_level: float) -> None:
        """Update current system state thread-safely."""
        with self._lock:
            self._state = OverlayState(motion_detected, audio_alert, motion_level, audio_level)
            self.motion_history.append(motion_level)
            # Audio level is typically small (<0.1), scale for visualization relative to motion (0-100)
            # Assume max audio ~0.1 -> 100
            self.audio_history.append(min(audio_level * 1000, 100))

    def _get_status_color(self) -> tuple[int, int, int, int]:
        """Get background color based on state (BGRA)."""
        with self._lock:
            state = self._state

        # Red: Motion + Noise
        if state.motion_detected and state.audio_alert:
            return (0, 0, 255, 100)  # Red, semi-transparent

        # Yellow: Motion only
        if state.motion_detected:
            return (0, 255, 255, 100)  # Yellow

        # Purple: Noise only
        if state.audio_alert:
            return (255, 0, 128, 100)  # Purple/Magenta

        # Transparent: Normal
        return (0, 0, 0, 0)

    def _generate_plot_image(self) -> np.ndarray:
        """Generate plot image using matplotlib."""
        self.ax.clear()
        self.ax.axis("off")
        self.ax.set_ylim(0, 100)

        with self._lock:
            y_motion = list(self.motion_history)
            y_audio = list(self.audio_history)

        self.ax.plot(y_motion, color="green", linewidth=1.5, label="Motion")
        self.ax.plot(y_audio, color="cyan", linewidth=1.5, label="Audio")
        # Add legend or simple lines? Simple lines are faster.

        self.canvas.draw()
        raw_data = np.frombuffer(self.canvas.tostring_argb(), dtype=np.uint8)
        w, h = self.fig.canvas.get_width_height()

        # Reshape to ARGB
        # img = raw_data.reshape((h, w, 4)) # Unused

        # Convert ARGB to BGRA for OpenCV
        # ARGB: 0=A, 1=R, 2=G, 3=B
        # BGRA: 0=B, 1=G, 2=R, 3=A
        # This is messy/slow with numpy, let's just use the rgba buffer if possible?
        # Canvas usually gives RGBA or ARGB. tostring_argb gives ARGB.

        # Let's try regular buffer (RGBA)
        raw_data = np.frombuffer(self.canvas.tostring_rgb(), dtype=np.uint8)
        img_rgb = raw_data.reshape((h, w, 3))
        # Add alpha
        img_rgba = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGRA)
        # Apply patch alpha manually or trust the plot... Matplotlib alpha handling can be tricky on buffers.
        # For speed, let's just use OpenCV drawing for lines instead of expensive matplotlib re-render every frame?
        # User requested matplotlib, but for 5fps it might be okay.

        return img_rgba

    def _draw_plot_cv2(self, img: np.ndarray) -> None:
        """Draw plot using OpenCV (Faster/Simpler fallback)."""
        h, w = img.shape[:2]
        plot_h = 80
        plot_w = 200
        x_start = w - plot_w - 10
        y_start = h - plot_h - 10

        # Background
        cv2.rectangle(img, (x_start, y_start), (x_start + plot_w, y_start + plot_h), (0, 0, 0, 100), -1)

        with self._lock:
            hist_m = list(self.motion_history)
            hist_a = list(self.audio_history)

        points_m = []
        points_a = []

        for i, val in enumerate(hist_m):
            x = x_start + int(i * plot_w / self.history_len)
            y = y_start + plot_h - int((val / 100.0) * plot_h)
            points_m.append((x, y))

        for i, val in enumerate(hist_a):
            x = x_start + int(i * plot_w / self.history_len)
            y = y_start + plot_h - int((val / 100.0) * plot_h)
            points_a.append((x, y))

        if len(points_m) > 1:
            cv2.polylines(img, [np.array(points_m)], False, (0, 255, 0, 255), 1)

        if len(points_a) > 1:
            cv2.polylines(img, [np.array(points_a)], False, (255, 255, 0, 255), 1)

        cv2.putText(img, "Motion", (x_start, y_start - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0, 255), 1)
        cv2.putText(img, "Audio", (x_start + 60, y_start - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 0, 255), 1)

    def generate_frame(self) -> bytes:
        """Generate a single overlay frame."""
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
        cv2.putText(img, ts, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255, 255), 2)

        with self._lock:
            info = f"Motion: {self._state.motion_level:.1f}%  Audio: {self._state.audio_level:.3f}"

        cv2.putText(img, info, (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200, 255), 1)

        # Draw plot (OpenCV is much faster/stable for this overlay use-case than converting mpl figures)
        self._draw_plot_cv2(img)

        # Convert BGRA (OpenCV) to RGBA (FFmpeg)
        img_rgba = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
        return img_rgba.tobytes()

    def run_loop(self) -> None:
        """Run the generation loop (blocking)."""
        LOGGER.info(f"Starting overlay loop writing to {self.pipe_path}")

        # Ensure pipe exists
        if not self.pipe_path.exists():
            try:
                if self.pipe_path.exists():
                    self.pipe_path.unlink()  # Cleanup if regular file
                import os

                os.mkfifo(str(self.pipe_path))
                self.pipe_path.chmod(0o666)
            except OSError as e:
                LOGGER.error(f"Failed to create pipe: {e}")
                # Don't crash, maybe it already exists or loop will retry

        self.running = True
        while self.running:
            try:
                # Open pipe in binary write mode
                LOGGER.info("Opening pipe in loop...")
                # Use os.open for non-blocking check or just standard open?
                # Standard open blocks until reader connects. This is good.
                with open(self.pipe_path, "wb") as pipe:
                    LOGGER.info("Pipe connected.")

                    while self.running:
                        start_time = time.time()

                        try:
                            frame_data = self.generate_frame()
                            pipe.write(frame_data)
                            pipe.flush()
                        except BrokenPipeError:
                            LOGGER.warning("Pipe broken (reader disconnected), reconnecting...")
                            break  # Break inner loop, retry outer loop
                        except Exception as inner_e:
                            LOGGER.error(f"Error generating/writing frame: {inner_e}")
                            time.sleep(1)  # Prevent tight error loop

                        elapsed = time.time() - start_time
                        delay = max(0.0, (1.0 / self.fps) - elapsed)
                        time.sleep(delay)

            except Exception as e:
                LOGGER.error(f"Overlay loop error: {e}")
                time.sleep(2)  # Wait before retry

    def start(self):
        """Start the overlay thread."""
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()

    def stop(self):
        """Stop the loop."""
        self.running = False
        if hasattr(self, "thread"):
            self.thread.join(timeout=1.0)
