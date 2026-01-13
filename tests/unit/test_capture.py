from unittest.mock import MagicMock, patch

import pytest

from bbwatch.capture import AudioCapture, SlidingWindowCapture


@pytest.fixture
def mock_subprocess():
    with patch("bbwatch.capture.subprocess.Popen") as mock:
        process_mock = MagicMock()
        process_mock.poll.return_value = None  # Running
        process_mock.returncode = None
        mock.return_value = process_mock
        yield mock


@pytest.fixture
def capture(tmp_path):
    return AudioCapture(
        device="hw:0,0",
        output_dir=tmp_path,
        segment_duration=1.0,
        sample_rate=16000,
        channels=1,
    )


def test_audio_capture_init(capture, tmp_path):
    assert capture.device == "hw:0,0"
    assert capture.output_dir == tmp_path
    assert capture._process is None


def test_audio_capture_build_command(capture):
    cmd = capture._build_command()
    assert cmd[0] == "ffmpeg"
    assert "-f" in cmd
    assert "alsa" in cmd
    assert capture.device in cmd
    assert str(capture.segment_duration) in cmd


def test_audio_capture_start(capture, mock_subprocess):
    capture.start()

    assert capture.is_running()
    mock_subprocess.assert_called_once()

    # Check that dir was created
    assert capture.output_dir.exists()

    capture.stop()
    assert not capture.is_running()  # Mock remains 'None' poll return, but stop sets process to None


def test_audio_capture_stop(capture, mock_subprocess):
    capture.start()
    capture.stop()

    process_mock = mock_subprocess.return_value
    process_mock.terminate.assert_called()
    process_mock.wait.assert_called()


def test_sliding_window_init(tmp_path):
    swc = SlidingWindowCapture(device="hw:0,0", output_dir=tmp_path, segment_duration=3.0, overlap=1.0)
    # stride = 2.0. 3.0 / 2.0 = 1.5 -> 1 proces? No, int(3/2) = 1?
    # logic: n_processes = int(segment_duration / stride)
    # 3.0 / (3.0-1.0) = 3/2 = 1.5 -> 1.
    # Wait, 3s duration, 1s overlap.
    # P1: 0-3. P2 needing to start at 2?
    # Logic in code: stride = 2.
    # Code says: n_processes = int(segment / stride)

    assert swc.n_processes == 1

    # Try different overlap to get multiple processes
    # Duration 3, Overlap 2 -> Stride 1.
    # n = 3 / 1 = 3.
    swc2 = SlidingWindowCapture(device="hw:0,0", output_dir=tmp_path, segment_duration=3.0, overlap=2.0)
    assert swc2.n_processes == 3


def test_sliding_window_start_stop(tmp_path, mock_subprocess):
    swc = SlidingWindowCapture(
        device="hw:0,0",
        output_dir=tmp_path,
        segment_duration=3.0,
        overlap=2.0,  # 3 processes
    )

    # We need to speed up sleep
    with patch("time.sleep"):
        swc.start()

    assert len(swc._captures) == 3
    assert mock_subprocess.call_count == 3

    swc.stop()
    assert len(swc._captures) == 0
