from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from bbwatch.motion import MotionDetector


@pytest.fixture
def mock_cv2():
    with patch("bbwatch.motion.cv2") as mock:
        yield mock


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

    motion_detector.process_frame(mock_frame)
    motion, detected = motion_detector.process_frame(mock_frame)

    assert motion == 0.0
    assert not detected


def test_run_loop(motion_detector, mock_cv2):
    # Mock VideoCapture
    mock_cap = MagicMock()
    mock_cv2.VideoCapture.return_value = mock_cap
    mock_cap.isOpened.return_value = True
    mock_cap.set.return_value = True

    # Configure threshold return for the loop processing
    mock_cv2.threshold.return_value = (0, np.zeros((640, 480), dtype=np.uint8))
    mock_cv2.cvtColor.return_value = np.zeros((640, 480), dtype=np.uint8)
    mock_cv2.GaussianBlur.return_value = np.zeros((640, 480), dtype=np.uint8)
    mock_cv2.absdiff.return_value = np.zeros((640, 480), dtype=np.uint8)
    mock_cv2.dilate.return_value = np.zeros((640, 480), dtype=np.uint8)

    # Return a frame then False (EOF)
    mock_frame = np.zeros((10, 10, 3), dtype=np.uint8)
    mock_cap.read.side_effect = [(True, mock_frame), Exception("Stop loop")]

    # Allow logic to run
    motion_detector.running = True

    with pytest.raises(Exception, match="Stop loop"):
        # Set frame_delay to 0 to speed up
        motion_detector.frame_delay = 0
        motion_detector._run()

    mock_cap.release.assert_not_called()  # Crashed before release


def test_start_stop(motion_detector, mock_cv2):
    # Mock threading
    with patch("threading.Thread") as mock_thread:
        mock_thread_inst = MagicMock()
        mock_thread.return_value = mock_thread_inst

        motion_detector.start()

        assert motion_detector.running is True
        mock_thread.assert_called_once()
        mock_thread_inst.start.assert_called_once()

        motion_detector.stop()
        assert motion_detector.running is False
        mock_thread_inst.join.assert_called_once()
