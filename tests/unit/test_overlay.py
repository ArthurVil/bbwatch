import logging
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
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
def log_capture():
    """Capture bbwatch.overlay_generator records with a plain handler.

    pytest's caplog fixture is unreliable in this environment (see
    tests/unit/test_latency.py for the same workaround).
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("bbwatch.overlay_generator")
    handler = _Collector(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)


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
    """generate_frame returns a raw RGBA array — no BGRA conversion pass.

    Regression: colors are defined in RGBA directly (see COLOR_* constants)
    specifically so no cv2.cvtColor call over the full canvas is needed;
    this test would catch that call being reintroduced only indirectly
    (via the returned type), so it's paired with an explicit assertion
    that cv2.cvtColor is never called.
    """
    frame = overlay_generator.generate_frame()

    assert isinstance(frame, np.ndarray)
    assert frame.shape == (480, 640, 4)
    assert frame.dtype == np.uint8
    mock_cv2.cvtColor.assert_not_called()
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


class TestWriteFrame:
    """_write_frame must push a whole frame through the FIFO, or fail loudly."""

    def test_retries_on_short_write_until_complete(self, overlay_generator):
        """A frame is far larger than the pipe buffer, so a single os.write()
        is always partial; _write_frame must keep writing the remainder.
        """
        frame = np.zeros((2, 2, 4), dtype=np.uint8)  # 16 bytes total
        total = frame.nbytes
        written_chunks = []

        def fake_write(fd, buf):
            n = min(3, len(buf))  # force multiple short writes
            written_chunks.append(bytes(buf[:n]))
            return n

        overlay_generator.running = True
        with patch("os.write", side_effect=fake_write):
            overlay_generator._write_frame(99, frame)

        assert sum(len(c) for c in written_chunks) == total

    def test_waits_on_blocking_io_error_then_completes(self, overlay_generator):
        """A full pipe buffer raises BlockingIOError; the writer must wait
        for the reader to drain it (via select) rather than dropping data
        or busy-looping, then resume writing the same frame.
        """
        frame = np.zeros((2, 2, 4), dtype=np.uint8)
        calls = {"n": 0}

        def fake_write(fd, buf):
            calls["n"] += 1
            if calls["n"] == 1:
                raise BlockingIOError()
            return len(buf)

        overlay_generator.running = True
        with (
            patch("os.write", side_effect=fake_write),
            patch("select.select", return_value=([], [99], [])) as mock_select,
        ):
            overlay_generator._write_frame(99, frame)

        mock_select.assert_called_once()
        assert calls["n"] == 2  # first blocked, second completed the frame

    def test_broken_pipe_propagates_when_reader_disconnects_mid_frame(self, overlay_generator):
        """A reader disconnecting mid-frame must raise BrokenPipeError up to
        run_loop, which reconnects — a partially-written frame must never
        be silently abandoned (rawvideo has no framing markers to resync).
        """
        frame = np.zeros((2, 2, 4), dtype=np.uint8)
        overlay_generator.running = True

        with patch("os.write", side_effect=BrokenPipeError()):
            with pytest.raises(BrokenPipeError):
                overlay_generator._write_frame(99, frame)

    def test_stops_early_when_running_flag_cleared(self, overlay_generator):
        """stop() sets running=False; an in-progress write loop must not
        keep pushing bytes after that.
        """
        frame = np.zeros((4, 4, 4), dtype=np.uint8)
        calls = {"n": 0}

        def fake_write(fd, buf):
            calls["n"] += 1
            overlay_generator.running = False  # simulate stop() mid-write
            return 1

        overlay_generator.running = True
        with patch("os.write", side_effect=fake_write):
            overlay_generator._write_frame(99, frame)

        assert calls["n"] == 1  # loop exited instead of continuing to write


def test_run_loop_reconnects_after_broken_pipe(overlay_generator, mock_cv2):
    """Failure path: a reader disconnecting mid-frame must not kill run_loop
    — it should log, close the fd, and loop back to reopen the pipe.
    """
    fake_fds = iter([42, 43])
    opened = []

    def fake_open(path, flags):
        fd = next(fake_fds)
        opened.append(fd)
        return fd

    write_calls = {"n": 0}

    def fake_write_frame(fd, frame_data):
        write_calls["n"] += 1
        if write_calls["n"] == 1:
            raise BrokenPipeError()
        overlay_generator.running = False  # stop after the reconnect succeeds

    with (
        patch("os.open", side_effect=fake_open),
        patch("os.close"),
        patch("os.mkfifo"),
        patch("pathlib.Path.exists", return_value=False),
        patch("select.select", return_value=([], [1], [])),
        patch("time.sleep"),
        patch.object(overlay_generator, "_write_frame", side_effect=fake_write_frame),
    ):
        overlay_generator.run_loop()

    assert opened == [42, 43]  # reconnected after the broken pipe
    assert write_calls["n"] == 2


def test_run_loop_survives_pipe_creation_failure(overlay_generator, mock_cv2):
    """Failure path: mkfifo failing (e.g. permission denied, disk full) must
    be logged, not crash the overlay thread — run_loop still attempts to
    open the pipe (which will keep retrying) instead of raising.
    """
    with (
        patch("os.mkfifo", side_effect=OSError("no permission")),
        patch("pathlib.Path.exists", return_value=False),
        patch("os.open", side_effect=OSError("still no pipe")),
        patch("time.sleep", side_effect=lambda *_: setattr(overlay_generator, "running", False)),
    ):
        overlay_generator.run_loop()  # must not raise

    assert overlay_generator.running is False


def test_run_loop_writes_to_pipe(overlay_generator, mock_cv2):
    """run_loop writes frame bytes when the fd is in select's WRITE list.

    Regression guard: select returns (rlist, wlist, xlist) and the fd is
    registered in wlist only — the mock must reflect that, otherwise a
    wrong-list unpack in run_loop passes undetected.
    """
    fake_fd = 42

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
    fd, data = written[0]
    assert fd == fake_fd
    # cv2 drawing calls are mocked (no-op), so the canvas stays a blank,
    # zeroed 640x480 RGBA frame — this pins the byte count _write_frame
    # must push through the FIFO regardless of drawn content.
    assert len(data) == 640 * 480 * 4
    assert data == b"\x00" * (640 * 480 * 4)


class TestFrameDropPolicy:
    """drop_stale_frames selects between bounded-latency-with-loss (default)
    and no-loss-but-unbounded-latency delivery — see OverlayGenerator.__init__.
    """

    def test_drop_mode_skips_frame_when_pipe_not_ready(self, overlay_generator, mock_cv2):
        """Default (drop_stale_frames=True): a not-writable pipe means the
        frame is dropped outright, never handed to os.write.
        """
        assert overlay_generator.drop_stale_frames is True
        write_calls = {"n": 0}

        def fake_os_write(fd, data):
            write_calls["n"] += 1
            return len(data)

        with (
            patch("os.open", return_value=42),
            patch("os.write", side_effect=fake_os_write),
            patch("os.close"),
            patch("os.mkfifo"),
            patch("pathlib.Path.exists", return_value=False),
            patch("select.select", return_value=([], [], [])),  # never writable
            patch("time.sleep", side_effect=lambda *_: setattr(overlay_generator, "running", False)),
        ):
            overlay_generator.run_loop()

        assert write_calls["n"] == 0

    def test_no_loss_mode_bypasses_readiness_gate(self, mock_pipe_path, mock_cv2):
        """drop_stale_frames=False must deliver the frame even when select
        would have reported the pipe as not writable — proving the readiness
        gate is bypassed, not just relaxed.
        """
        gen = OverlayGenerator(
            pipe_path=mock_pipe_path,
            width=640,
            height=480,
            fps=10,
            history_len=20,
            drop_stale_frames=False,
        )
        written = []

        def fake_os_write(fd, data):
            written.append(bytes(data))
            gen.running = False  # stop after the one frame we're checking
            return len(data)

        with (
            patch("os.open", return_value=42),
            patch("os.write", side_effect=fake_os_write),
            patch("os.close"),
            patch("os.mkfifo"),
            patch("pathlib.Path.exists", return_value=False),
            patch("select.select", return_value=([], [], [])),  # would say "not writable" if checked
            patch("time.sleep"),
        ):
            gen.run_loop()

        assert len(written) == 1


class TestStop:
    """stop() must surface it loudly if the thread outlives its join timeout.

    Most reachable with drop_stale_frames=False: _write_frame can be blocked
    on backpressure for every frame, not just ones that passed a readiness
    check, so a stalled reader can make join(timeout=1.0) time out.
    """

    def test_logs_error_when_thread_survives_join(self, overlay_generator, log_capture):
        overlay_generator.thread = MagicMock()
        overlay_generator.thread.is_alive.return_value = True

        overlay_generator.stop()

        overlay_generator.thread.join.assert_called_once_with(timeout=1.0)
        errors = [r.getMessage() for r in log_capture if r.levelno == logging.ERROR]
        assert any("did not stop within 1s" in m for m in errors)

    def test_no_error_logged_when_thread_stops_in_time(self, overlay_generator, log_capture):
        overlay_generator.thread = MagicMock()
        overlay_generator.thread.is_alive.return_value = False

        overlay_generator.stop()

        errors = [r.getMessage() for r in log_capture if r.levelno == logging.ERROR]
        assert errors == []


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
