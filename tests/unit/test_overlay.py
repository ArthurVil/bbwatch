import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from bbwatch.overlay_generator import OverlayGenerator


@pytest.fixture
def mock_cv2():
    with patch("bbwatch.overlay_generator.cv2") as mock:
        yield mock


@pytest.fixture
def mock_pipe_path(tmp_path):
    return tmp_path / "test.pipe"


@pytest.fixture
def overlay_generator(mock_pipe_path, mock_cv2):
    return OverlayGenerator(
        pipe_path=mock_pipe_path,
        width=640,
        height=480,
        fps=10,
        history_len=20,
    )


def test_initialization(overlay_generator):
    assert overlay_generator.width == 640
    assert overlay_generator.fps == 10
    assert len(overlay_generator.motion_history) == 20
    assert overlay_generator._state.motion_detected is False


def test_update_state(overlay_generator):
    overlay_generator.update_state(
        motion_detected=True,
        audio_alert=False,
        motion_level=50.0,
        audio_level=0.05,
    )

    assert overlay_generator._state.motion_detected is True
    assert overlay_generator._state.motion_level == 50.0
    # Deque is maxlen=20 and initialized with 0s.
    # append adds to right.
    assert len(overlay_generator.motion_history) == 20
    assert overlay_generator.motion_history[-1] == 50.0

    # Check audio scaling (0.05 * 1000 = 50)
    assert overlay_generator.audio_history[-1] == 50.0


def test_generate_frame_calls_cv2(overlay_generator, mock_cv2):
    # Setup mock return for cvtColor
    mock_cv2.cvtColor.return_value = MagicMock(tobytes=lambda: b"fake_bytes")

    frame_bytes = overlay_generator.generate_frame()

    assert frame_bytes == b"fake_bytes"
    mock_cv2.putText.assert_called()  # Check that text is drawn (timestamp, info)

    # Check that polylines are called (plot)
    # They might not be called if history is empty or 1 point
    # Add data to history
    overlay_generator.motion_history.extend([10, 20])
    overlay_generator.generate_frame()
    # cv2.polylines should be called for plot
    # Not strictly asserting call count as it depends on internal plot logic


def test_status_color_logic(overlay_generator):
    # Red: Motion + Audio
    overlay_generator.update_state(True, True, 0, 0)
    assert overlay_generator._get_status_color() == overlay_generator.COLOR_RED

    # Yellow: Motion only
    overlay_generator.update_state(True, False, 0, 0)
    assert overlay_generator._get_status_color() == overlay_generator.COLOR_YELLOW

    # Purple: Audio only
    overlay_generator.update_state(False, True, 0, 0)
    assert overlay_generator._get_status_color() == overlay_generator.COLOR_PURPLE

    # Transparent: None
    overlay_generator.update_state(False, False, 0, 0)
    assert overlay_generator._get_status_color() == overlay_generator.COLOR_TRANSPARENT


def test_run_loop_writes_to_pipe(overlay_generator, mock_cv2):
    """run_loop writes frame bytes when the fd is in select's WRITE list.

    Regression guard: select returns (rlist, wlist, xlist) and the fd is
    registered in wlist only — the mock must reflect that, otherwise a
    wrong-list unpack in run_loop passes undetected.
    """
    fake_fd = 42
    mock_cv2.cvtColor.return_value = MagicMock(tobytes=lambda: b"data")

    written = []

    def fake_os_write(fd, data):
        written.append((fd, bytes(data)))
        overlay_generator.running = False  # stop after first write
        return len(data)

    with (
        patch("os.open", return_value=fake_fd),
        patch("os.write", side_effect=fake_os_write),
        patch("os.close"),
        patch("os.mkfifo"),
        patch("pathlib.Path.exists", return_value=False),
        patch("select.select", return_value=([], [fake_fd], [])),
        patch("time.sleep"),
    ):
        overlay_generator.run_loop()

    assert len(written) == 1
    assert written[0] == (fake_fd, b"data")


@pytest.mark.slow
def test_run_loop_writes_full_frames_to_real_fifo(tmp_path):
    """End-to-end regression: a reader on a real FIFO receives complete frames.

    Catches both halves of the broken write path: the fd being taken from
    select's read list (nothing ever written) and unhandled short writes
    (a 1.2MB frame can never fit the 64KiB pipe buffer in one os.write).
    """
    pipe_path = tmp_path / "overlay.pipe"
    gen = OverlayGenerator(pipe_path=pipe_path, width=640, height=480, fps=30, history_len=20)
    frame_size = 640 * 480 * 4

    received = bytearray()
    done = threading.Event()

    def reader():
        with open(pipe_path, "rb") as f:
            while len(received) < 2 * frame_size:
                chunk = f.read(2 * frame_size - len(received))
                if not chunk:
                    break
                received.extend(chunk)
        done.set()

    gen.start()
    try:
        # run_loop creates the FIFO; wait for it before opening the reader.
        deadline = time.time() + 5.0
        while time.time() < deadline and not pipe_path.exists():
            time.sleep(0.05)
        assert pipe_path.exists(), "run_loop never created the FIFO"

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        assert done.wait(timeout=10.0), f"received only {len(received)} of {2 * frame_size} bytes"
        assert len(received) == 2 * frame_size
    finally:
        gen.running = False
        gen.thread.join(timeout=3.0)
