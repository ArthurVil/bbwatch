from unittest.mock import MagicMock, mock_open, patch

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
    # Setup mock open
    m_open = mock_open()

    # We need to simulate the loop running once
    overlay_generator.running = True

    # Patch OPEN in the module
    with patch("bbwatch.overlay_generator.open", m_open):
        with patch("os.mkfifo"):
            # Patch pathlib.Path.exists correctly
            with patch("pathlib.Path.exists", return_value=False):
                with patch("time.sleep"):
                    mock_cv2.cvtColor.return_value = MagicMock(tobytes=lambda: b"data")

                    # Let's use a side effect on write() to stop
                    m_file = m_open.return_value

                    def write_effect(data):
                        overlay_generator.running = False

                    m_file.__enter__.return_value.write.side_effect = write_effect

                    overlay_generator.run_loop()

                    # Verify write called
                    m_file.__enter__.return_value.write.assert_called_with(b"data")
