"""Unit tests for FFmpegRecorder clip lifecycle.

FFmpeg itself is replaced by a tiny Python script (via build_command) so the
real subprocess, reaper thread, signal handling and file finalization run.
"""

import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from bbwatch import recording
from bbwatch.recording import FFmpegRecorder, part_path_for, recover_partial_clips

# Fake-ffmpeg behaviours: argv[1] is the .part output path.
WRITE_AND_EXIT_0 = "import sys; open(sys.argv[1], 'wb').write(b'x' * 64)"
FAIL_NO_OUTPUT = (
    "import sys; sys.stderr.write('Could not find tag for codec pcm_mulaw in stream #1'); sys.exit(1)"
)
WRITE_THEN_FAIL = "import sys; open(sys.argv[1], 'wb').write(b'x' * 64); sys.stderr.write('connection reset'); sys.exit(1)"
WRITE_UNTIL_SIGINT = (
    "import signal, sys, time\n"
    "f = open(sys.argv[1], 'wb'); f.write(b'x' * 64); f.flush()\n"
    "signal.signal(signal.SIGINT, lambda *a: sys.exit(255))\n"
    "time.sleep(30)\n"
)
IGNORE_SIGINT = (
    "import signal, sys, time\n"
    "open(sys.argv[1], 'wb').write(b'x' * 64)\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "time.sleep(30)\n"
)


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _fake_ffmpeg(script: str) -> Callable[[FFmpegRecorder, Path, float], list[str]]:
    def build(self: FFmpegRecorder, part_path: Path, max_duration_s: float) -> list[str]:
        return [sys.executable, "-c", script, str(part_path)]

    return build


@pytest.fixture
def clip_path(tmp_path: Path) -> Path:
    return tmp_path / "2026-10-06" / "101500.mp4"


class TestBuildCommand:
    def test_transcodes_audio_and_copies_video(self, tmp_path: Path) -> None:
        """pcm_mulaw from go2rtc can't go in MP4 — audio must be AAC, video stream-copied."""
        cmd = FFmpegRecorder("rtsp://host/babycam").build_command(tmp_path / ".a.mp4.part", 300)

        assert cmd[cmd.index("-c:v") + 1] == "copy"
        assert cmd[cmd.index("-c:a") + 1] == "aac"
        assert "-c" not in cmd  # the old blanket stream copy
        assert cmd[cmd.index("-t") + 1] == "300"
        assert "frag_keyframe" in cmd[cmd.index("-movflags") + 1]
        assert cmd[cmd.index("-f") + 1] == "mp4"  # .part extension can't imply the muxer
        assert cmd[-1].endswith(".part")
        assert "-nostdin" in cmd


class TestClipLifecycle:
    def test_success_renames_part_to_final(self, clip_path: Path) -> None:
        rec = FFmpegRecorder("rtsp://x")
        with patch.object(FFmpegRecorder, "build_command", _fake_ffmpeg(WRITE_AND_EXIT_0)):
            rec.start_recording(clip_path, 10)
        assert _wait_for(clip_path.exists)
        assert not part_path_for(clip_path).exists()
        assert rec.last_error() is None
        assert not rec.is_recording()

    def test_failure_without_output_is_reported(self, clip_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Failure path: FFmpeg exiting non-zero must log ERROR with its stderr, never fail silently."""
        rec = FFmpegRecorder("rtsp://x")
        with caplog.at_level(logging.ERROR), patch.object(
            FFmpegRecorder, "build_command", _fake_ffmpeg(FAIL_NO_OUTPUT)
        ):
            rec.start_recording(clip_path, 10)
            assert _wait_for(lambda: rec.last_error() is not None)

        assert "pcm_mulaw" in (rec.last_error() or "")
        assert "exit 1" in (rec.last_error() or "")
        assert any("Clip recording FAILED" in r.message for r in caplog.records)
        assert not clip_path.exists()
        assert not part_path_for(clip_path).exists()

    def test_failure_mid_clip_keeps_partial_footage(self, clip_path: Path) -> None:
        """Failure path: stream drops mid-clip — keep what was recorded, still report the error."""
        rec = FFmpegRecorder("rtsp://x")
        with patch.object(FFmpegRecorder, "build_command", _fake_ffmpeg(WRITE_THEN_FAIL)):
            rec.start_recording(clip_path, 10)
        assert _wait_for(lambda: rec.last_error() is not None)
        assert clip_path.exists()
        assert "connection reset" in (rec.last_error() or "")

    def test_success_clears_previous_error(self, tmp_path: Path) -> None:
        rec = FFmpegRecorder("rtsp://x")
        with patch.object(FFmpegRecorder, "build_command", _fake_ffmpeg(FAIL_NO_OUTPUT)):
            rec.start_recording(tmp_path / "a.mp4", 10)
        assert _wait_for(lambda: rec.last_error() is not None)
        with patch.object(FFmpegRecorder, "build_command", _fake_ffmpeg(WRITE_AND_EXIT_0)):
            rec.start_recording(tmp_path / "b.mp4", 10)
        assert _wait_for(lambda: (tmp_path / "b.mp4").exists())
        assert _wait_for(lambda: rec.last_error() is None)

    def test_stop_sends_sigint_and_finalizes(self, clip_path: Path) -> None:
        """FFmpeg's exit 255 after a requested SIGINT is a clean stop, not a failure."""
        rec = FFmpegRecorder("rtsp://x")
        with patch.object(FFmpegRecorder, "build_command", _fake_ffmpeg(WRITE_UNTIL_SIGINT)):
            rec.start_recording(clip_path, 10)
        assert _wait_for(lambda: part_path_for(clip_path).exists())
        assert rec.is_recording()

        start = time.monotonic()
        rec.stop_recording()
        assert time.monotonic() - start < 0.5  # non-blocking for the alert hot path
        assert not rec.is_recording()

        assert _wait_for(clip_path.exists)
        assert rec.last_error() is None

    def test_stop_escalates_to_kill(self, clip_path: Path) -> None:
        """Failure path: FFmpeg ignoring SIGINT must be killed after the grace period."""
        rec = FFmpegRecorder("rtsp://x")
        with patch.object(recording, "STOP_GRACE_S", 0.2), patch.object(
            FFmpegRecorder, "build_command", _fake_ffmpeg(IGNORE_SIGINT)
        ):
            rec.start_recording(clip_path, 10)
            assert _wait_for(lambda: part_path_for(clip_path).exists())
            time.sleep(0.2)  # let the child install its SIGINT handler
            rec.stop_recording()
            assert _wait_for(clip_path.exists)

    def test_start_while_recording_raises(self, tmp_path: Path) -> None:
        rec = FFmpegRecorder("rtsp://x")
        with patch.object(FFmpegRecorder, "build_command", _fake_ffmpeg(WRITE_UNTIL_SIGINT)):
            rec.start_recording(tmp_path / "a.mp4", 10)
            try:
                with pytest.raises(RuntimeError):
                    rec.start_recording(tmp_path / "b.mp4", 10)
            finally:
                rec.stop_recording()

    def test_name_collision_gets_suffix(self, clip_path: Path) -> None:
        clip_path.parent.mkdir(parents=True)
        clip_path.write_bytes(b"existing")
        rec = FFmpegRecorder("rtsp://x")
        with patch.object(FFmpegRecorder, "build_command", _fake_ffmpeg(WRITE_AND_EXIT_0)):
            rec.start_recording(clip_path, 10)
        suffixed = clip_path.with_name("101500_1.mp4")
        assert _wait_for(suffixed.exists)
        assert clip_path.read_bytes() == b"existing"


class TestRecoverPartialClips:
    def test_promotes_nonempty_and_drops_empty(self, tmp_path: Path) -> None:
        day = tmp_path / "2026-10-06"
        day.mkdir()
        (day / ".101500.mp4.part").write_bytes(b"x" * 10)
        (day / ".101600.mp4.part").write_bytes(b"")

        recovered = recover_partial_clips(tmp_path)

        assert recovered == [day / "101500.mp4"]
        assert (day / "101500.mp4").exists()
        assert list(day.glob("*.part")) == []

    def test_missing_dir_is_noop(self, tmp_path: Path) -> None:
        assert recover_partial_clips(tmp_path / "nope") == []
