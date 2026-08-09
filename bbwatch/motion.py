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

from bbwatch.latency import LatencyTracker, StageTimer

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
        zoom: float = 1.0,
        offset_x: float = 0.0,
        offset_y: float = 0.0,
        process_width: int = 640,
        latency_report_interval_s: float = 10.0,
        equalize_luminosity: bool = False,
        clahe_clip_limit: float = 2.0,
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
            zoom: Crop factor for the analysis ROI (1.0 = full frame, 2.0 =
                center half width/height), applied to the native-resolution
                frame. Same absolute movement covers a larger fraction of a
                smaller ROI, raising effective sensitivity — and cropping
                before any downscale preserves the sensor detail zoom exists
                to exploit, rather than cropping an already-degraded frame.
            offset_x: Horizontal crop offset, -1 (left) .. 1 (right), 0 = centered.
            offset_y: Vertical crop offset, -1 (top) .. 1 (bottom), 0 = centered.
            process_width: Max width of the analysis frame *after* cropping —
                bounds per-frame CPU cost independent of zoom/source
                resolution. The crop is downscaled to this width only if
                larger; never upscaled.
            latency_report_interval_s: Seconds between latency summaries.
            equalize_luminosity: Normalize brightness before differencing
                using a brightness-equalization LUT calibrated ONCE from
                the first frame and frozen for the rest of the process's
                life (never recomputed) — see _build_luminosity_lut.

                Earlier revisions of this feature recomputed the
                equalization adaptively on every frame (first CLAHE, then
                global histogram equalization); the second was deployed and
                reported live as near-constant false-motion flicker after
                dimming a room. The PROVEN cause and fix is
                clahe_clip_limit, documented on that parameter below —
                measure it there before assuming this paragraph is what's
                protecting you. Freezing is additional, unproven-by-test
                insurance: it removes any dependence on two real frames'
                histograms staying statistically similar to each other
                (true for simple i.i.d. per-pixel noise, tested here, but
                not necessarily true for compression artifacts, correlated
                sensor read noise, or continuous micro-exposure drift,
                none of which this synthetic test models). The tradeoff
                freezing does definitely cost: it will NOT track a real
                lighting change after that first frame — the room getting
                brighter or darker later reads exactly as it would with
                equalization off, until the process restarts. Off by
                default — changes detection sensitivity.
            clahe_clip_limit: Bounds how steeply the frozen LUT can stretch
                the calibration frame's brightness range (only used when
                equalize_luminosity is True) — this is the mechanism
                actually verified to control noise amplification: a high
                limit amplifies noise on every frame it's applied to
                regardless of freezing (measured: unclipped stretching of a
                narrow, noisy, low-contrast frame reproduces the same ~45%
                false motion whether recomputed every frame or frozen —
                freezing alone, without a low clip limit, does not fix the
                reported bug). 2.0 (OpenCV's typical CLAHE default)
                measured 0% false motion on a synthetic dim/noisy-room
                reproduction of the reported bug, both frozen and
                recomputed per frame at that same limit.
        """
        self.device_index = device_index
        self.threshold = threshold
        self.blur_size = blur_size
        self.motion_threshold_percent = motion_threshold_percent
        self.dilation_iterations = dilation_iterations
        self.fps = fps
        self.zoom = zoom
        self.offset_x = offset_x
        self.offset_y = offset_y
        self.process_width = process_width
        self.frame_delay = 1.0 / fps  # Pre-calculate sleep time
        self.equalize_luminosity = equalize_luminosity
        self.clahe_clip_limit = clahe_clip_limit
        # Calibrated from the first frame and frozen — see equalize_luminosity
        # docstring. None until that first frame arrives.
        self._luminosity_lut: np.ndarray | None = None

        self._prev_gray: np.ndarray | None = None
        self._current_motion: float = 0.0
        self._motion_history: deque[float] = deque(maxlen=history_len)
        self._lock = Lock()
        # Fixed 3x3 dilate kernel — built once instead of every process_frame() call.
        self._dilate_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))

        # cv2 defaults to one worker thread per core for its internal ops
        # (cvtColor/GaussianBlur/absdiff/threshold/dilate). Measured on an
        # x86 dev box (proxy for the Pi): on the ~640px-wide frames this
        # pipeline actually processes, per-frame cost with the default
        # thread pool was ~0.23ms vs ~0.21ms single-threaded — the
        # dispatch/join overhead of spreading sub-millisecond work across
        # cores costs more than it saves, and the extra worker threads
        # compete with the main loop, OverlayGenerator, and capture threads
        # for the Pi's 4 cores. This setting is process-global (OpenCV has
        # one thread pool per process), so it also applies to
        # OverlayGenerator's cv2 drawing calls.
        cv2.setNumThreads(1)

        # Liveness: timestamp of the last successfully captured frame.
        # None until the first frame arrives. Read by is_healthy().
        self._last_frame_ts: float | None = None
        self._stop_event = threading.Event()
        self._latency = LatencyTracker("motion", report_interval_s=latency_report_interval_s)

        # Determine capture backend
        if isinstance(device_index, str) and device_index.isdigit():
            self.device_index = int(device_index)

    def _crop_roi(self, frame: np.ndarray) -> np.ndarray:
        """Crop the analysis region according to zoom/offset config.

        zoom=1.0 is a no-op (returns frame unchanged). Cropping also
        shrinks the array processed by cvtColor/blur/diff, lowering
        per-frame compute proportionally to the zoom factor.
        """
        if self.zoom <= 1.0:
            return frame

        h, w = frame.shape[:2]
        crop_w = max(1, int(w / self.zoom))
        crop_h = max(1, int(h / self.zoom))
        max_x = w - crop_w
        max_y = h - crop_h

        # offset in [-1, 1] maps linearly across the available margin,
        # centered (offset=0) in the middle of the frame.
        x = int((max_x / 2) * (1 + self.offset_x))
        y = int((max_y / 2) * (1 + self.offset_y))
        x = max(0, min(x, max_x))
        y = max(0, min(y, max_y))

        return frame[y : y + crop_h, x : x + crop_w]

    def _resize_for_processing(self, frame: np.ndarray) -> np.ndarray:
        """Downscale to process_width if the (post-crop) frame is larger.

        Never upscales. INTER_AREA is the correct choice for shrinking —
        it averages source pixels instead of sampling/interpolating.
        """
        h, w = frame.shape[:2]
        if w <= self.process_width:
            return frame
        new_h = max(1, int(h * self.process_width / w))
        return cv2.resize(frame, (self.process_width, new_h), interpolation=cv2.INTER_AREA)

    @staticmethod
    def _build_luminosity_lut(gray: np.ndarray, clip_limit: float) -> np.ndarray:
        """Build a 256-entry brightness-equalization LUT from one frame.

        The standard clip-and-redistribute histogram-equalization algorithm
        (CLAHE with a single global tile, i.e. tileGridSize=(1, 1)):
        clipping the histogram before taking its CDF bounds how steeply any
        input range gets stretched, which is what bounds sensor-noise
        amplification (see equalize_luminosity's docstring). Verified
        empirically within 2 output levels of
        cv2.createCLAHE(tileGridSize=(1, 1)).apply() on the same input —
        reimplemented directly rather than calling that (undocumented,
        black-box) API because the caller needs a reusable LUT object to
        freeze and replay via cv2.LUT(), not a one-off transformed image.
        """
        hist, _ = np.histogram(gray, bins=256, range=(0, 256))
        clip_value = max(1, int(clip_limit * gray.size / 256))
        excess = np.clip(hist.astype(np.int64) - clip_value, 0, None).sum()
        hist_clipped = np.minimum(hist, clip_value).astype(np.float64)
        hist_clipped += excess / 256.0
        cdf = np.cumsum(hist_clipped)
        span = cdf[-1] - cdf[0]
        if span <= 0:
            # Degenerate calibration frame (perfectly uniform, e.g. a blank
            # test frame): no dynamic range to stretch. Identity avoids a
            # divide-by-zero and a meaningless mapping rather than crashing.
            #
            # This is defensive, not a real-world safety net: with
            # clip_value = clip_limit * size / 256, span > 0 whenever
            # clip_value < size, which holds for any clip_limit
            # MotionConfig currently allows (le=40.0 => would need size
            # <= ~10 px to reach this branch on a real frame) — a
            # low-but-nonzero-range first frame (glare, mid-AE-convergence)
            # produces a real, non-identity LUT instead, and is handled by
            # process_frame's raw_range check, not here.
            return np.arange(256, dtype=np.uint8)
        lut: np.ndarray = ((cdf - cdf[0]) / span * 255.0).astype(np.uint8)
        return lut

    def process_frame(self, frame: np.ndarray) -> tuple[float, bool]:
        """Process a single frame for motion.

        Args:
            frame: BGR image frame.

        Returns:
            Tuple of (motion_percentage, is_motion_detected).
        """
        frame = self._crop_roi(frame)
        frame = self._resize_for_processing(frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if self.equalize_luminosity:
            if self._luminosity_lut is None:
                # One-shot calibration: computed from this first frame only
                # and never touched again — see __init__'s docstring.
                self._luminosity_lut = self._build_luminosity_lut(gray, self.clahe_clip_limit)
                # _build_luminosity_lut's own degenerate-input guard (a
                # perfectly uniform frame) is effectively unreachable at any
                # clahe_clip_limit MotionConfig allows (le=40.0) — the real
                # risk is a *low* dynamic-range calibration frame (glare,
                # mid-AE-convergence, a scene that's merely mostly flat),
                # which produces a real, non-identity LUT that still
                # degrades detection quality silently for the rest of this
                # run, since calibration never repeats. Surface it here
                # instead, on a directly-interpretable measurement (the raw
                # value range) rather than the internal CDF math.
                raw_range = int(gray.max()) - int(gray.min())
                if raw_range < 20:
                    LOGGER.warning(
                        f"Luminosity calibration frame has a very low brightness range "
                        f"({raw_range}/255) — detection may be degraded for the rest of this run "
                        "(calibration is one-shot and will not be retried); consider restarting "
                        "once the camera has a representative view of the scene."
                    )
                else:
                    LOGGER.info(f"Calibrated luminosity LUT from the first frame (range={raw_range}/255, one-shot)")
            gray = cv2.LUT(gray, self._luminosity_lut)
        gray = cv2.GaussianBlur(gray, (self.blur_size, self.blur_size), 0)

        motion_percent = 0.0

        if self._prev_gray is not None:
            # Frame difference
            frame_diff = cv2.absdiff(self._prev_gray, gray)

            # Threshold
            _, thresh = cv2.threshold(frame_diff, self.threshold, 255, cv2.THRESH_BINARY)

            # Dilate to fill gaps
            thresh = cv2.dilate(thresh, self._dilate_kernel, iterations=self.dilation_iterations)

            # Calculate percentage. cv2.countNonZero avoids allocating a
            # full-frame boolean array just to sum it (np.sum(thresh > 0)).
            motion_pixels = cv2.countNonZero(thresh)
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
        """True when the capture and processing threads are alive and frames are fresh.

        Polled by the main loop so a dead or stalled capture (device
        unplugged, RTSP hung) — or a dead processing thread, which
        previously went undetected here even though frames kept arriving —
        is surfaced instead of silently serving stale motion values forever.
        """
        capture_thread = getattr(self, "capture_thread", None)
        if capture_thread is None or not capture_thread.is_alive():
            return False
        process_thread = getattr(self, "process_thread", None)
        if process_thread is None or not process_thread.is_alive():
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
                    # Decode at native resolution — do NOT downscale here.
                    # zoom crops the ROI from the full-resolution frame in
                    # process_frame(); downscaling before crop would throw
                    # away exactly the detail zoom is meant to preserve.
                    # process_width (applied post-crop) is what bounds
                    # per-frame CPU cost instead.
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
                timer = StageTimer()
                frame_ts = self._last_frame_ts
                if frame_ts is not None:
                    # Age of the frame when processing begins (capture → process)
                    timer.stages["frame_age"] = (time.time() - frame_ts) * 1000.0
                try:
                    self.process_frame(frame_to_process)
                except Exception:
                    # A single bad frame must not silently kill this thread:
                    # is_healthy() also checks process_thread.is_alive() now,
                    # but an uncaught exception here would still take motion
                    # detection down invisibly until the next poll — log loud
                    # and keep going, matching OverlayGenerator's per-frame
                    # resilience pattern.
                    LOGGER.exception("Motion processing failed on this frame; continuing")
                else:
                    timer.mark("process")
                    self._latency.record(timer)

            # Maintain target FPS
            elapsed = time.time() - start_time
            delay = max(0.0, self.frame_delay - elapsed)
            time.sleep(delay)

        LOGGER.info("Processing loop stopped")
