import logging
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from bbwatch.motion import MotionDetector


@pytest.fixture
def mock_cv2():
    with patch("bbwatch.motion.cv2") as mock:
        yield mock


@pytest.fixture
def log_capture():
    """Capture bbwatch.motion records with a plain handler.

    pytest's caplog fixture is unreliable in this environment (see
    tests/unit/test_latency.py for the same workaround).
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("bbwatch.motion")
    handler = _Collector(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)


@pytest.fixture
def motion_detector(mock_cv2):
    return MotionDetector(
        device_index=0,
        threshold=25,
        blur_size=21,
        history_len=10,
        motion_threshold_percent=1.0,
        dilation_iterations=2,
        fps=10,
    )


def test_initialization(motion_detector):
    assert motion_detector.device_index == 0
    assert motion_detector.threshold == 25
    assert len(motion_detector._motion_history) == 0
    assert motion_detector._current_motion == 0.0


class TestCropRoi:
    """zoom/offset determine the analysis ROI cropped from each frame."""

    def _frame(self, h: int = 100, w: int = 200) -> np.ndarray:
        return np.arange(h * w * 3, dtype=np.uint8).reshape(h, w, 3)

    def test_zoom_one_is_noop(self, motion_detector):
        frame = self._frame()
        cropped = motion_detector._crop_roi(frame)
        assert cropped is frame

    def test_zoom_two_centered_crop_is_half_size_and_centered(self, motion_detector):
        motion_detector.zoom = 2.0
        frame = self._frame(h=100, w=200)

        cropped = motion_detector._crop_roi(frame)

        assert cropped.shape[:2] == (50, 100)
        np.testing.assert_array_equal(cropped, frame[25:75, 50:150])

    def test_offset_shifts_crop_toward_requested_edge(self, motion_detector):
        motion_detector.zoom = 2.0
        motion_detector.offset_x = 1.0  # full right
        motion_detector.offset_y = -1.0  # full top
        frame = self._frame(h=100, w=200)

        cropped = motion_detector._crop_roi(frame)

        # max_x = 200-100=100, max_y = 100-50=50; offset=+1/-1 -> x=100, y=0
        np.testing.assert_array_equal(cropped, frame[0:50, 100:200])

    def test_offset_is_clamped_within_frame_bounds(self, motion_detector):
        motion_detector.zoom = 2.0
        motion_detector.offset_x = 5.0  # out of the [-1,1] contract; must still clamp safely
        frame = self._frame(h=100, w=200)

        cropped = motion_detector._crop_roi(frame)

        assert cropped.shape[:2] == (50, 100)
        np.testing.assert_array_equal(cropped, frame[25:75, 100:200])

    def test_process_frame_uses_cropped_dimensions(self, motion_detector, mock_cv2):
        motion_detector.zoom = 2.0
        mock_cv2.cvtColor.side_effect = lambda img, _: np.zeros(img.shape[:2], dtype=np.uint8)
        mock_cv2.GaussianBlur.side_effect = lambda img, *a, **k: img
        frame = self._frame(h=100, w=200)  # crop (100x50) stays below process_width=640 -> no resize

        motion_detector.process_frame(frame)

        assert motion_detector._prev_gray.shape == (50, 100)


class TestResizeForProcessing:
    """process_width bounds CPU cost of the (post-crop) analysis frame.

    Real cv2 (not mocked): this is a thin wrapper around cv2.resize and the
    interesting behavior is the actual output shape/no-op threshold.
    """

    def _frame(self, h: int, w: int) -> np.ndarray:
        return np.zeros((h, w, 3), dtype=np.uint8)

    def test_noop_when_within_budget(self):
        detector = MotionDetector(process_width=640)
        frame = self._frame(h=480, w=640)

        result = detector._resize_for_processing(frame)

        assert result is frame

    def test_never_upscales(self):
        detector = MotionDetector(process_width=1920)
        frame = self._frame(h=480, w=640)

        result = detector._resize_for_processing(frame)

        assert result is frame

    def test_downscales_preserving_aspect_ratio(self):
        detector = MotionDetector(process_width=640)
        frame = self._frame(h=1080, w=1920)  # 16:9

        result = detector._resize_for_processing(frame)

        assert result.shape[:2] == (360, 640)  # 1080 * 640/1920 = 360

    def test_crop_then_resize_preserves_native_detail_before_downscale(self):
        """The point of the whole pipeline: zoom crops native pixels, THEN
        the crop (not the full frame) is downscaled to the CPU budget —
        so a zoomed ROI keeps far more real detail than cropping an
        already-downscaled frame would.
        """
        detector = MotionDetector(zoom=2.0, process_width=640)
        native_1080p = self._frame(h=1080, w=1920)

        cropped = detector._crop_roi(native_1080p)
        assert cropped.shape[:2] == (540, 960)  # native crop, full detail

        final = detector._resize_for_processing(cropped)
        assert final.shape[:2] == (360, 640)  # 540 * 640/960 = 360


def test_initialization_string_index():
    detector = MotionDetector(device_index="1")
    assert detector.device_index == 1

    # Path should remain string
    detector_path = MotionDetector(device_index="/dev/video0")
    assert detector_path.device_index == "/dev/video0"


def test_process_frame_first_frame(motion_detector, mock_cv2):
    # Setup mock return for cvtColor and GaussianBlur
    mock_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    mock_gray = np.zeros((480, 640), dtype=np.uint8)

    mock_cv2.cvtColor.return_value = mock_gray
    mock_cv2.GaussianBlur.return_value = mock_gray

    motion, detected = motion_detector.process_frame(mock_frame)

    # First frame establishes background, so 0 motion
    assert motion == 0.0
    assert not detected
    assert motion_detector.get_current_motion() == 0.0
    assert len(motion_detector.get_history()) == 1


def test_process_frame_motion_detected(motion_detector, mock_cv2):
    # Setup - simulate a changed frame
    mock_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    mock_gray_1 = np.zeros((100, 100), dtype=np.uint8)
    mock_gray_2 = np.ones((100, 100), dtype=np.uint8) * 255  # Full change

    mock_cv2.cvtColor.side_effect = [mock_gray_1, mock_gray_2]
    mock_cv2.GaussianBlur.side_effect = [mock_gray_1, mock_gray_2]
    mock_cv2.absdiff.return_value = np.ones((100, 100), dtype=np.uint8) * 255
    mock_cv2.threshold.return_value = (0, np.ones((100, 100), dtype=np.uint8))
    mock_cv2.dilate.return_value = np.ones((100, 100), dtype=np.uint8)  # All pixels changed
    mock_cv2.countNonZero.side_effect = lambda arr: int(np.count_nonzero(arr))

    # First frame (baseline)
    motion_detector.process_frame(mock_frame)

    # Second frame (change)
    motion, detected = motion_detector.process_frame(mock_frame)

    assert motion == 100.0  # 100% changed
    assert detected
    assert motion_detector.get_current_motion() == 100.0


def test_process_frame_no_motion(motion_detector, mock_cv2):
    # Setup - simulate identical frames
    mock_frame = np.zeros((100, 100), dtype=np.uint8)
    mock_gray = np.zeros((100, 100), dtype=np.uint8)

    mock_cv2.cvtColor.return_value = mock_gray
    mock_cv2.GaussianBlur.return_value = mock_gray
    mock_cv2.absdiff.return_value = np.zeros((100, 100), dtype=np.uint8)
    mock_cv2.threshold.return_value = (0, np.zeros((100, 100), dtype=np.uint8))
    mock_cv2.dilate.return_value = np.zeros((100, 100), dtype=np.uint8)
    mock_cv2.countNonZero.side_effect = lambda arr: int(np.count_nonzero(arr))

    motion_detector.process_frame(mock_frame)
    motion, detected = motion_detector.process_frame(mock_frame)

    assert motion == 0.0
    assert not detected


class TestEqualizeLuminosity:
    """One-shot LUT calibration: built from the first frame only and frozen
    for the detector's lifetime — never recomputed, even when later frames
    have a completely different histogram.
    """

    def test_disabled_by_default_no_lut_built(self, motion_detector, mock_cv2):
        mock_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        mock_cv2.cvtColor.return_value = np.zeros((100, 100), dtype=np.uint8)
        mock_cv2.GaussianBlur.return_value = np.zeros((100, 100), dtype=np.uint8)

        motion_detector.process_frame(mock_frame)

        mock_cv2.LUT.assert_not_called()
        assert motion_detector._luminosity_lut is None

    def test_first_frame_calibrates_lut_and_applies_it(self, mock_cv2):
        detector = MotionDetector(equalize_luminosity=True)
        mock_frame = np.zeros((10, 10, 3), dtype=np.uint8)
        gray = np.full((10, 10), 50, dtype=np.uint8)
        mock_cv2.cvtColor.return_value = gray
        mock_cv2.GaussianBlur.side_effect = lambda img, *a, **k: img
        mock_cv2.LUT.side_effect = lambda src, lut: src

        detector.process_frame(mock_frame)

        assert detector._luminosity_lut is not None
        assert detector._luminosity_lut.shape == (256,)
        mock_cv2.LUT.assert_called_once()
        called_gray, called_lut = mock_cv2.LUT.call_args[0]
        np.testing.assert_array_equal(called_gray, gray)
        np.testing.assert_array_equal(called_lut, detector._luminosity_lut)

    def test_lut_is_not_recomputed_on_a_later_frame_with_different_brightness(self, mock_cv2):
        """The whole point of this design: a later frame with a totally
        different histogram must reuse the exact same frozen LUT, not get
        its own — that per-frame recomputation is what caused the reported
        noise-flicker bug in the two earlier (adaptive) implementations.
        """
        detector = MotionDetector(equalize_luminosity=True)
        gray_dark = np.full((10, 10), 20, dtype=np.uint8)
        gray_bright = np.full((10, 10), 200, dtype=np.uint8)
        mock_cv2.cvtColor.side_effect = [gray_dark, gray_bright]
        mock_cv2.GaussianBlur.side_effect = lambda img, *a, **k: img
        mock_cv2.LUT.side_effect = lambda src, lut: src
        # Minimal stubs so the second call's diffing branch doesn't choke on
        # unconfigured-MagicMock arithmetic (shape/dtype coercion downstream).
        mock_cv2.absdiff.return_value = np.zeros((10, 10), dtype=np.uint8)
        mock_cv2.threshold.return_value = (0, np.zeros((10, 10), dtype=np.uint8))
        mock_cv2.dilate.return_value = np.zeros((10, 10), dtype=np.uint8)
        mock_cv2.countNonZero.return_value = 0

        detector.process_frame(np.zeros((10, 10, 3), dtype=np.uint8))
        lut_after_first_frame = detector._luminosity_lut.copy()

        detector.process_frame(np.zeros((10, 10, 3), dtype=np.uint8))

        np.testing.assert_array_equal(detector._luminosity_lut, lut_after_first_frame)
        assert mock_cv2.LUT.call_count == 2  # applied on both frames...
        second_call_lut = mock_cv2.LUT.call_args_list[1][0][1]
        np.testing.assert_array_equal(second_call_lut, lut_after_first_frame)  # ...but never rebuilt


class TestBuildLuminosityLut:
    """Unit tests for the LUT-construction algorithm itself (real numpy,
    no cv2 involved) — the clip-and-redistribute histogram-equalization
    math that clahe_clip_limit actually controls.
    """

    def test_returns_256_entry_uint8_lut(self):
        gray = np.random.default_rng(0).integers(0, 256, (50, 50), dtype=np.uint8)
        lut = MotionDetector._build_luminosity_lut(gray, clip_limit=2.0)
        assert lut.shape == (256,)
        assert lut.dtype == np.uint8

    def test_all_zero_frame_returns_identity_lut(self):
        """The one genuinely degenerate case: every pixel at value 0 (so
        the CDF is already saturated at bin 0) AND the clip limit is high
        enough that no clipping/redistribution happens to spread the CDF
        elsewhere — cdf[-1] == cdf[0], nothing to stretch. Must fall back
        to identity instead of dividing by zero.

        Note: an all-*flat-but-nonzero* frame (e.g. every pixel at 100) is
        NOT degenerate — clip-and-redistribute still spreads the excess
        count across all 256 bins, producing a (nearly linear, not exactly
        identity) ramp rather than a flat CDF.
        """
        gray = np.zeros((5, 5), dtype=np.uint8)
        lut = MotionDetector._build_luminosity_lut(gray, clip_limit=1000.0)
        np.testing.assert_array_equal(lut, np.arange(256, dtype=np.uint8))

    def test_higher_clip_limit_stretches_more_aggressively(self):
        """A narrow input range with a low clip limit should be stretched
        less than the same input with a high (near-unclipped) limit —
        this is the mechanism clahe_clip_limit controls, and what bounds
        noise amplification in the frozen LUT (see MotionDetector's
        equalize_luminosity docstring).
        """
        gray = np.clip(40 + np.random.default_rng(1).normal(0, 2, (80, 80)), 0, 255).astype(np.uint8)

        lut_low_clip = MotionDetector._build_luminosity_lut(gray, clip_limit=2.0)
        lut_high_clip = MotionDetector._build_luminosity_lut(gray, clip_limit=1000.0)

        # Slope near the input band (~40) — the high-clip LUT should climb
        # much faster there since it stretches the narrow input range
        # closer to the full 0-255 output range.
        low_span = int(lut_low_clip[45]) - int(lut_low_clip[35])
        high_span = int(lut_high_clip[45]) - int(lut_high_clip[35])
        assert high_span > low_span


class TestFrozenLuminosityLutBehavior:
    """Real cv2 (not mocked): the two properties that matter for this
    feature — it fixes the reported bug, and it's honest about what it
    deliberately does not fix.
    """

    def _dim_noisy_frame(self, rng: np.random.Generator, shift: int = 0) -> np.ndarray:
        # A dim, low-contrast scene (base ~40) with independent per-pixel
        # sensor noise each call — simulates the reported "curtain drawn,
        # non-stop flicker" scenario: no real motion, just noise.
        h, w = 96, 128
        true_scene = 40 + 5 * np.sin(np.mgrid[0:h, 0:w][1] / 8.0)
        gray = np.clip(true_scene + shift + rng.normal(0, 2, (h, w)), 0, 255).astype(np.uint8)
        return np.stack([gray, gray, gray], axis=-1)

    def test_low_dynamic_range_calibration_frame_logs_warning(self, log_capture):
        """The real risk _build_luminosity_lut's own degenerate-input guard
        can't catch (it's unreachable at any clahe_clip_limit MotionConfig
        allows): a first frame with almost no brightness range — glare,
        mid-AE-convergence — produces a real, non-identity LUT that still
        silently degrades detection for the rest of the run. This is the
        one-shot design's actual failure mode, so it must be visible.
        """
        detector = MotionDetector(equalize_luminosity=True)
        flat_ish_frame = np.full((20, 20, 3), 100, dtype=np.uint8)
        flat_ish_frame[0, 0] = 105  # raw_range = 5: nonzero but very low

        detector.process_frame(flat_ish_frame)

        warnings = [r.getMessage() for r in log_capture if r.levelno == logging.WARNING]
        assert any("low brightness range" in m and "5/255" in m for m in warnings)

    def test_normal_calibration_frame_logs_info_not_warning(self, log_capture):
        """A representative, high-contrast first frame must not trip the
        low-range warning — raw_range here is ~160/255, far from the
        20/255 threshold.
        """
        detector = MotionDetector(equalize_luminosity=True)
        y, x = np.mgrid[0:96, 0:128]
        gray = np.clip(128 + 80 * np.sin(x / 5.0), 0, 255).astype(np.uint8)
        wide_range_frame = np.stack([gray, gray, gray], axis=-1)

        detector.process_frame(wide_range_frame)

        warnings = [r.getMessage() for r in log_capture if r.levelno == logging.WARNING]
        infos = [r.getMessage() for r in log_capture if r.levelno == logging.INFO]
        assert warnings == []
        assert any("Calibrated luminosity LUT" in m for m in infos)

    def test_sensor_noise_in_a_dim_room_no_longer_flickers(self):
        """Regression test for the reported bug: two independent noisy
        frames of the same static, dim scene must not read as motion.
        Measured 0% with these exact parameters (matches shipped
        config.yaml's threshold/blur_size).

        This proves clahe_clip_limit=2.0 keeps this specific i.i.d.-noise
        reproduction under threshold — it does NOT isolate freezing as the
        cause: recomputing the same clip_limit=2.0 LUT fresh every frame
        measured statistically indistinguishable results on this exact
        noise model (see config.py's equalize_luminosity comment for why
        freezing is kept anyway). See TestEqualizeLuminosity for the
        separate, structural proof that freezing actually happens.
        """
        rng = np.random.default_rng(42)
        detector = MotionDetector(
            threshold=10, blur_size=5, dilation_iterations=0, equalize_luminosity=True, clahe_clip_limit=2.0
        )

        detector.process_frame(self._dim_noisy_frame(rng))
        motion_percent, detected = detector.process_frame(self._dim_noisy_frame(rng))

        assert motion_percent < 5.0
        assert not detected

    def test_brightness_shift_after_calibration_is_not_corrected(self):
        """Accepted, deliberately-chosen limitation: the LUT is frozen from
        the FIRST frame only (per explicit design choice — periodic
        recalibration was considered and rejected). A real brightness
        shift arriving on a later frame is NOT calibrated away — this is
        the tradeoff, not a regression.
        """
        rng = np.random.default_rng(7)
        detector = MotionDetector(threshold=25, blur_size=3, dilation_iterations=0, equalize_luminosity=True)

        detector.process_frame(self._dim_noisy_frame(rng, shift=0))
        motion_percent, detected = detector.process_frame(self._dim_noisy_frame(rng, shift=120))

        assert motion_percent > 50.0
        assert detected


def test_capture_loop(motion_detector, mock_cv2):
    # Mock VideoCapture
    mock_cap = MagicMock()
    mock_cv2.VideoCapture.return_value = mock_cap
    mock_cap.isOpened.return_value = True
    mock_cap.set.return_value = True

    # Return a frame then False (EOF) to simulate stream end
    mock_frame = np.zeros((10, 10, 3), dtype=np.uint8)
    mock_cap.read.side_effect = [(True, mock_frame), RuntimeError("Stop loop")]

    # Allow logic to run
    motion_detector.running = True
    motion_detector._frame_lock = MagicMock()

    with pytest.raises(RuntimeError, match="Stop loop"):
        motion_detector._capture_loop()

    mock_cap.release.assert_called()  # finally block releases even on crash


def test_capture_loop_retries_when_device_fails_to_open(motion_detector, mock_cv2):
    """Regression: an open failure must retry, never kill the thread.

    The loop used to `return` when the device failed to open, leaving
    get_current_motion() silently serving stale values forever.
    """
    mock_cap = MagicMock()
    mock_cap.isOpened.return_value = False
    mock_cv2.VideoCapture.return_value = mock_cap

    motion_detector.running = True
    attempts = []

    def count_and_stop(timeout):
        attempts.append(timeout)
        if len(attempts) >= 3:
            motion_detector.running = False

    with patch.object(motion_detector._stop_event, "wait", side_effect=count_and_stop):
        motion_detector._capture_loop()  # returns cleanly instead of dying early

    assert mock_cv2.VideoCapture.call_count >= 3  # kept retrying
    assert mock_cap.release.call_count >= 3  # each failed open is released


def test_capture_loop_reopens_on_read_failure(motion_detector, mock_cv2):
    """A failed read must reopen the device, not spin on a dead handle."""
    dead_cap = MagicMock()
    dead_cap.isOpened.return_value = True
    dead_cap.read.return_value = (False, None)

    live_cap = MagicMock()
    live_cap.isOpened.return_value = True
    frame = np.zeros((10, 10, 3), dtype=np.uint8)

    def deliver_and_stop():
        motion_detector.running = False
        return (True, frame)

    live_cap.read.side_effect = lambda: deliver_and_stop()
    mock_cv2.VideoCapture.side_effect = [dead_cap, live_cap]

    motion_detector.running = True
    motion_detector._frame_lock = MagicMock()

    with patch.object(motion_detector._stop_event, "wait"):
        motion_detector._capture_loop()

    dead_cap.release.assert_called()  # dead handle dropped
    assert motion_detector._last_frame_ts is not None  # liveness updated


def test_process_loop_survives_process_frame_exception(motion_detector, mock_cv2):
    """A crash inside process_frame() (a bad frame, or any cv2 call
    including equalizeHist) must not silently kill this thread —
    is_healthy() checks process_thread.is_alive(), so a thread that died
    here would otherwise go undetected while motion data froze forever.
    """
    motion_detector._latest_frame = np.zeros((10, 10, 3), dtype=np.uint8)
    motion_detector._frame_lock = MagicMock()
    motion_detector.running = True

    calls = {"n": 0}

    def fake_process_frame(frame):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("corrupt frame")
        motion_detector.running = False  # stop after the second call
        return (0.0, False)

    with (
        patch.object(motion_detector, "process_frame", side_effect=fake_process_frame),
        patch("bbwatch.motion.time.sleep"),
    ):
        motion_detector._process_loop()  # must not raise

    assert calls["n"] == 2  # survived the exception and processed the next frame


def test_is_healthy_reflects_thread_and_frame_freshness(motion_detector):
    """is_healthy() is the main loop's signal that motion data is real."""
    # No capture thread yet
    assert motion_detector.is_healthy() is False

    motion_detector.capture_thread = MagicMock(is_alive=lambda: True)

    # Capture thread alive, but no process thread yet
    assert motion_detector.is_healthy() is False

    motion_detector.process_thread = MagicMock(is_alive=lambda: True)

    # Both threads alive but no frame ever captured
    assert motion_detector.is_healthy() is False

    # Fresh frame
    motion_detector._last_frame_ts = time.time()
    assert motion_detector.is_healthy() is True

    # Stale frame
    motion_detector._last_frame_ts = time.time() - 60.0
    assert motion_detector.is_healthy() is False

    # Dead capture thread with fresh frame
    motion_detector.capture_thread = MagicMock(is_alive=lambda: False)
    motion_detector._last_frame_ts = time.time()
    assert motion_detector.is_healthy() is False

    # Regression: a dead *processing* thread must be caught too — frames can
    # keep arriving (capture thread fine) while process_frame() has crashed
    # and stopped updating motion data entirely.
    motion_detector.capture_thread = MagicMock(is_alive=lambda: True)
    motion_detector.process_thread = MagicMock(is_alive=lambda: False)
    assert motion_detector.is_healthy() is False


class TestOpenCapture:
    """RTSP sources must bound network I/O; local devices open plainly."""

    def test_rtsp_url_uses_ffmpeg_backend_with_timeouts(self, mock_cv2):
        """Failure path: without open/read timeouts a hung RTSP stream
        blocks cap.read() indefinitely, outliving stop()'s join timeout.
        """
        detector = MotionDetector(device_index="rtsp://cam.local:8554/raw_video")

        detector._open_capture()

        mock_cv2.VideoCapture.assert_called_once_with(
            "rtsp://cam.local:8554/raw_video",
            mock_cv2.CAP_FFMPEG,
            [mock_cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000, mock_cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000],
        )

    def test_local_device_index_opens_without_extra_params(self, mock_cv2):
        detector = MotionDetector(device_index=0)

        detector._open_capture()

        mock_cv2.VideoCapture.assert_called_once_with(0)

    def test_local_device_path_opens_without_extra_params(self, mock_cv2):
        detector = MotionDetector(device_index="/dev/video0")

        detector._open_capture()

        mock_cv2.VideoCapture.assert_called_once_with("/dev/video0")


def test_start_stop(motion_detector, mock_cv2):
    # Mock threading
    with patch("threading.Thread") as mock_thread:
        mock_thread_inst = MagicMock()
        mock_thread.return_value = mock_thread_inst

        motion_detector.start()

        assert motion_detector.running is True
        # Should create 2 threads (capture + process)
        assert mock_thread.call_count == 2
        assert mock_thread_inst.start.call_count == 2

        motion_detector.stop()
        assert motion_detector.running is False
        assert mock_thread_inst.join.call_count == 2
