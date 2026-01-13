"""Motion detection using OpenCV frame differencing.

Implements a lightweight motion detector that calculates motion intensity
based on frame differences, suitable for triggering alerts and visualizing activity.
"""

import logging
import threading
import time
from collections import deque
from threading import Lock

import cv2
import numpy as np

LOGGER = logging.getLogger(__name__)


class MotionDetector:
    """Detects motion in video stream using frame differencing."""

    def __init__(
        self,
        device_index: int | str = 0,
        threshold: int = 25,
        blur_size: int = 21,
        history_len: int = 100,
        motion_threshold_percent: float = 1.0,
        dilation_iterations: int = 2,
        fps: int = 10,
    ) -> None:
        """Initialize motion detector.

        Args:
            device_index: Video device index or path (e.g., 0, "/dev/video0").
            threshold: Pixel difference threshold (0-255).
            blur_size: Gaussian blur kernel size (must be odd).
            history_len: Number of frames to keep in motion history.
            motion_threshold_percent: Motion percentage to trigger detection.
            dilation_iterations: Number of dilation iterations for morphology.
            fps: Target frame rate for motion detection.
        """
        self.device_index = device_index
        self.threshold = threshold
        self.blur_size = blur_size
        self.motion_threshold_percent = motion_threshold_percent
        self.dilation_iterations = dilation_iterations
        self.fps = fps
        self.frame_delay = 1.0 / fps  # Pre-calculate sleep time

        self._prev_gray: np.ndarray | None = None
        self._current_motion: float = 0.0
        self._motion_history: deque[float] = deque(maxlen=history_len)
        self._lock = Lock()

        # Determine capture backend
        if isinstance(device_index, str) and device_index.isdigit():
            self.device_index = int(device_index)

    def process_frame(self, frame: np.ndarray) -> tuple[float, bool]:
        """Process a single frame for motion.

        Args:
            frame: BGR image frame.

        Returns:
            Tuple of (motion_percentage, is_motion_detected).
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (self.blur_size, self.blur_size), 0)

        motion_percent = 0.0

        if self._prev_gray is not None:
            # Frame difference
            frame_diff = cv2.absdiff(self._prev_gray, gray)

            # Threshold
            _, thresh = cv2.threshold(frame_diff, self.threshold, 255, cv2.THRESH_BINARY)

            # Dilate to fill gaps
            # Use default 3x3 kernel explicitly for mypy compatibility
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
            thresh = cv2.dilate(thresh, kernel, iterations=self.dilation_iterations)

            # Calculate percentage
            motion_pixels = np.sum(thresh > 0)
            total_pixels = thresh.shape[0] * thresh.shape[1]
            motion_percent = float((motion_pixels / total_pixels) * 100)

        self._prev_gray = gray

        with self._lock:
            self._current_motion = motion_percent
            self._motion_history.append(motion_percent)

        return motion_percent, motion_percent > self.motion_threshold_percent

    def get_current_motion(self) -> float:
        """Get the most recent motion percentage."""
        with self._lock:
            return self._current_motion

    def get_history(self) -> list[float]:
        """Get motion history."""
        with self._lock:
            return list(self._motion_history)

    def start(self) -> None:
        """Start the motion detection thread."""
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Stop the motion detection thread."""
        self.running = False
        if hasattr(self, "thread"):
            self.thread.join(timeout=1.0)

    def _run(self) -> None:
        """Main capture loop."""
        LOGGER.info(f"Starting motion detection on device {self.device_index}")
        cap = cv2.VideoCapture(self.device_index)

        if not cap.isOpened():
            LOGGER.error(f"Failed to open video device {self.device_index}")
            return

        # Set low resolution for performance
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        while self.running:
            ret, frame = cap.read()
            if not ret:
                LOGGER.warning("Failed to read frame")
                time.sleep(1)
                continue

            self.process_frame(frame)
            time.sleep(self.frame_delay)  # Configurable FPS

        cap.release()
        LOGGER.info("Motion detection stopped")
