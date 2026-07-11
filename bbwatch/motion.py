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

        # Liveness: timestamp of the last successfully captured frame.
        # None until the first frame arrives. Read by is_healthy().
        self._last_frame_ts: float | None = None
        self._stop_event = threading.Event()

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

    def last_frame_age_s(self) -> float | None:
        """Seconds since the last captured frame; None if no frame yet."""
        ts = self._last_frame_ts
        return None if ts is None else time.time() - ts

    def is_healthy(self, max_frame_age_s: float = 10.0) -> bool:
        """True when the capture thread is alive and frames are fresh.

        Polled by the main loop so a dead or stalled capture (device
        unplugged, RTSP hung) is surfaced instead of silently serving
        stale motion values forever.
        """
        thread = getattr(self, "capture_thread", None)
        if thread is None or not thread.is_alive():
            return False
        age = self.last_frame_age_s()
        return age is not None and age <= max_frame_age_s

    def start(self) -> None:
        """Start the motion detection threads."""
        self.running = True
        self._stop_event.clear()

        # Latest frame buffer (thread-safe)
        self._latest_frame: np.ndarray | None = None
        self._frame_lock = Lock()

        # Start capture thread (runs as fast as possible to drain buffer)
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture_thread.start()

        # Start processing thread (runs at target FPS)
        self.process_thread = threading.Thread(target=self._process_loop, daemon=True)
        self.process_thread.start()

    def stop(self) -> None:
        """Stop the motion detection threads."""
        self.running = False
        self._stop_event.set()
        if hasattr(self, "capture_thread"):
            self.capture_thread.join(timeout=1.0)
        if hasattr(self, "process_thread"):
            self.process_thread.join(timeout=1.0)

    # Delay between reconnection attempts when the device cannot be opened.
    RECONNECT_INTERVAL_S: float = 5.0

    def _open_capture(self) -> "cv2.VideoCapture":
        """Open the video device, bounding network I/O for stream URLs.

        Without open/read timeouts a hung RTSP stream blocks cap.read()
        indefinitely, outliving stop()'s join timeout.
        """
        if isinstance(self.device_index, str) and "://" in self.device_index:
            return cv2.VideoCapture(
                self.device_index,
                cv2.CAP_FFMPEG,
                [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
            )
        return cv2.VideoCapture(self.device_index)

    def _capture_loop(self) -> None:
        """Continuously grab frames, reconnecting when the device fails.

        This loop must never exit while running: a dead capture thread
        would leave get_current_motion() silently serving stale values.
        Open/read failures are logged and retried forever.
        """
        LOGGER.info(f"Starting capture loop on {self.device_index}")
        cap: cv2.VideoCapture | None = None

        try:
            while self.running:
                if cap is None:
                    cap = self._open_capture()
                    if not cap.isOpened():
                        LOGGER.error(
                            f"Failed to open video device {self.device_index}; "
                            f"retrying in {self.RECONNECT_INTERVAL_S}s"
                        )
                        cap.release()
                        cap = None
                        self._stop_event.wait(self.RECONNECT_INTERVAL_S)
                        continue

                    LOGGER.info(f"Video device {self.device_index} opened")
                    # Low resolution + minimal buffer for performance/freshness
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

                ret, frame = cap.read()
                if not ret:
                    LOGGER.warning("Failed to read frame; reopening video device")
                    cap.release()
                    cap = None
                    self._stop_event.wait(1.0)
                    continue

                # Store latest frame and refresh liveness timestamp
                self._last_frame_ts = time.time()
                with self._frame_lock:
                    self._latest_frame = frame

                # No sleep here! Consume frames as fast as possible.
        finally:
            if cap is not None:
                cap.release()
            LOGGER.info("Capture loop stopped")

    def _process_loop(self) -> None:
        """Process the latest available frame at target FPS."""
        LOGGER.info("Starting processing loop")

        while self.running:
            start_time = time.time()

            # Get latest frame
            frame_to_process = None
            with self._frame_lock:
                if self._latest_frame is not None:
                    frame_to_process = self._latest_frame.copy()

            if frame_to_process is not None:
                self.process_frame(frame_to_process)

            # Maintain target FPS
            elapsed = time.time() - start_time
            delay = max(0.0, self.frame_delay - elapsed)
            time.sleep(delay)

        LOGGER.info("Processing loop stopped")
