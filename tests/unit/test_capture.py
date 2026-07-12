import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from bbwatch.capture import AudioCapture, SlidingWindowCapture


@pytest.fixture
def mock_subprocess():
    with patch("bbwatch.capture.subprocess.Popen") as mock:
        process_mock = MagicMock()
        process_mock.poll.return_value = None  # Running
        process_mock.returncode = None
        process_mock.stderr.readline.return_value = b""  # EOF for drain thread
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


def test_stderr_is_drained_so_chatty_ffmpeg_cannot_wedge(capture):
    """Regression: a process writing more stderr than the 64KiB pipe buffer
    must run to completion (an undrained pipe blocks its writes forever,
    leaving a wedged process that still looks alive), and the retained tail
    must be available and bounded for the death report.
    """
    chatty_cmd = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('E' * 200_000 + '\\nlast-line\\n')",
    ]
    with patch.object(capture, "_build_command", return_value=chatty_cmd):
        capture._start_process()

    # Without the drain thread this wait would time out: the child blocks
    # writing stderr once the pipe fills and never exits.
    capture._process.wait(timeout=5.0)

    # Give the drain thread a moment to consume the remainder after exit.
    import time

    deadline = time.time() + 2.0
    while time.time() < deadline and "last-line" not in capture._stderr_snapshot():
        time.sleep(0.05)

    snapshot = capture._stderr_snapshot()
    assert "last-line" in snapshot
    assert len(capture._stderr_tail) <= capture._stderr_tail.maxlen


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


def test_start_raises_runtime_error_when_ffmpeg_missing(capture):
    """Regression/failure-path: a missing ffmpeg binary must surface as a
    clear RuntimeError, not an unhandled FileNotFoundError deep in Popen.
    """
    with patch("bbwatch.capture.subprocess.Popen", side_effect=FileNotFoundError("no ffmpeg")):
        with pytest.raises(RuntimeError, match="FFmpeg not found"):
            capture.start()


def test_stop_kills_process_when_terminate_times_out(capture, mock_subprocess):
    """If FFmpeg ignores SIGTERM, stop() must escalate to kill() rather than
    hang forever waiting for a process that will never exit.
    """
    capture.start()
    process_mock = mock_subprocess.return_value
    process_mock.wait.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5.0)

    capture.stop()

    process_mock.terminate.assert_called_once()
    process_mock.kill.assert_called_once()


def test_monitor_loop_restarts_ffmpeg_after_it_dies(capture, mock_subprocess):
    """Failure path: FFmpeg exiting non-zero mid-run must trigger an
    automatic restart via the real _monitor_loop, not silently stop
    producing segments forever.

    The process dies after the first health check; the loop must call
    _start_process() again and then stop retrying once the stop event is
    set (simulating a healthy restart followed by shutdown).
    """
    process_mock = mock_subprocess.return_value
    process_mock.poll.return_value = 1  # dead from the start
    process_mock.returncode = 1

    capture._stop_event.clear()
    capture._process = process_mock
    capture._max_restarts = 10

    calls = {"n": 0}
    real_sleep_calls = []

    def fake_sleep(seconds):
        real_sleep_calls.append(seconds)
        calls["n"] += 1
        if calls["n"] >= 2:  # let exactly one restart happen, then stop
            capture._stop_event.set()

    with patch("bbwatch.capture.time.sleep", side_effect=fake_sleep):
        capture._monitor_loop()

    assert capture._restart_count == 1
    assert mock_subprocess.call_count == 1  # one restart via _start_process (no initial start() call here)


def test_monitor_loop_thread_restarts_and_gives_up_after_max_restarts(capture, mock_subprocess):
    """End-to-end via the real monitor thread: death -> restart, repeated
    until _max_restarts is exceeded, at which point the loop gives up
    instead of restart-looping forever.
    """
    process_mock = mock_subprocess.return_value
    process_mock.poll.return_value = 1  # "already dead" from the first check
    process_mock.returncode = 1

    capture._max_restarts = 2
    capture._stop_event.clear()
    capture._process = process_mock

    with patch("bbwatch.capture.time.sleep"):
        capture._monitor_loop()  # runs to completion once max_restarts exceeded

    assert capture._restart_count == 2
    # initial _process assigned manually + 2 restarts via _start_process
    assert mock_subprocess.call_count == 2


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
