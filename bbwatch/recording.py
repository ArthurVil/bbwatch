"""Video recording abstractions.

Provides a Protocol for capturing frames and recording clips, with an
FFmpegRecorder implementation that reads from an RTSP stream.

Clips are written to a hidden ``.<name>.part`` file next to their final
path and renamed into place only once FFmpeg exits, so anything consuming
the clips directory (e.g. the Google Drive uploader in ``deploy/``) never
sees a half-written file. The container is fragmented MP4, so even a
``.part`` left behind by a crash is playable; ``recover_partial_clips``
promotes those on startup.
"""

import logging
import signal
import subprocess
import threading
from pathlib import Path
from typing import Protocol

LOGGER = logging.getLogger(__name__)

PART_SUFFIX = ".part"

# How long a stopped FFmpeg gets to flush and exit after SIGINT before it is killed.
STOP_GRACE_S = 5.0

# Keep only the tail of FFmpeg's stderr for error reports.
STDERR_TAIL_CHARS = 2000


def part_path_for(output_path: Path) -> Path:
    """Hidden in-progress path for a clip: ``dir/.name.mp4.part``."""
    return output_path.with_name(f".{output_path.name}{PART_SUFFIX}")


def _final_path_for(part_path: Path) -> Path:
    """Inverse of part_path_for."""
    return part_path.with_name(part_path.name[1 : -len(PART_SUFFIX)])


def _unique_path(path: Path) -> Path:
    """Return path, or path with a numeric suffix if it already exists."""
    candidate = path
    n = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        n += 1
    return candidate


def _finalize_part(part_path: Path) -> Path | None:
    """Rename a .part clip to its final name. Empty/missing parts are dropped.

    Returns:
        The final path, or None if there was nothing worth keeping.
    """
    try:
        if part_path.stat().st_size == 0:
            part_path.unlink()
            return None
    except FileNotFoundError:
        return None
    final = _unique_path(_final_path_for(part_path))
    part_path.replace(final)
    return final


def recover_partial_clips(clips_dir: Path) -> list[Path]:
    """Promote ``.part`` clips left behind by a crash or restart.

    Fragmented MP4 stays playable up to the last flushed fragment, so the
    footage is kept rather than discarded.

    Returns:
        Final paths of the recovered clips.
    """
    recovered: list[Path] = []
    if not clips_dir.exists():
        return recovered
    for part in sorted(clips_dir.rglob(f".*{PART_SUFFIX}")):
        try:
            final = _finalize_part(part)
        except OSError as e:
            LOGGER.error(f"Could not recover partial clip {part}: {e}")
            continue
        if final is not None:
            LOGGER.warning(f"Recovered interrupted clip: {final}")
            recovered.append(final)
    return recovered


class Recorder(Protocol):
    """Protocol for capturing frames and recording video clips."""

    def capture_frame(self, output_path: Path) -> None:
        """Capture a single frame to JPEG.

        Raises:
            subprocess.TimeoutExpired: Capture took too long.
            subprocess.CalledProcessError: FFmpeg returned non-zero exit.
            FileNotFoundError: FFmpeg not installed.
        """
        ...

    def start_recording(self, output_path: Path, duration_sec: float) -> None:
        """Start a background clip recording.

        Raises:
            RuntimeError: Already recording.
            FileNotFoundError: FFmpeg not installed.
        """
        ...

    def is_recording(self) -> bool:
        """Return True if a clip recording is in progress."""
        ...

    def stop_recording(self) -> None:
        """Stop any in-progress recording gracefully (non-blocking)."""
        ...

    def last_error(self) -> str | None:
        """Description of the most recent failed recording, None if the last one succeeded."""
        ...


class _Recording:
    """One FFmpeg clip process plus the reaper thread that finalizes it."""

    def __init__(self, process: subprocess.Popen[bytes], part_path: Path) -> None:
        self.process = process
        self.part_path = part_path
        self.stop_requested = False


class FFmpegRecorder:
    """Records video from an RTSP stream via FFmpeg subprocesses."""

    def __init__(self, stream_url: str) -> None:
        self.stream_url = stream_url
        self._current: _Recording | None = None
        self._lock = threading.Lock()
        self._last_error: str | None = None

    def capture_frame(self, output_path: Path) -> None:
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y", "-nostdin",
                "-rtsp_transport", "tcp",
                "-i", self.stream_url,
                "-vframes", "1",
                "-q:v", "5",
                str(output_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, "ffmpeg")

    def build_command(self, part_path: Path, duration_sec: float) -> list[str]:
        """FFmpeg command for one clip.

        Video is stream-copied (no CPU cost). Audio is transcoded to AAC:
        the go2rtc stream carries pcm_mulaw, which the MP4 muxer rejects,
        so a plain ``-c copy`` fails before writing a single frame.
        Fragmented MP4 keeps the file playable if FFmpeg is killed.
        """
        return [
            "ffmpeg", "-y", "-nostdin",
            "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", self.stream_url,
            "-t", str(int(duration_sec)),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "64k",
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4",
            str(part_path),
        ]

    def start_recording(self, output_path: Path, duration_sec: float) -> None:
        with self._lock:
            if self._current is not None and self._current.process.poll() is None:
                raise RuntimeError("Already recording")

            output_path.parent.mkdir(parents=True, exist_ok=True)
            part_path = part_path_for(output_path)
            process = subprocess.Popen(
                self.build_command(part_path, duration_sec),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            recording = _Recording(process, part_path)
            self._current = recording

        threading.Thread(
            target=self._reap,
            args=(recording,),
            name=f"ClipReaper-{output_path.name}",
            daemon=True,
        ).start()

    def _reap(self, recording: _Recording) -> None:
        """Wait for FFmpeg to exit, then finalize the clip and record the outcome."""
        try:
            _, stderr_bytes = recording.process.communicate()
        except Exception as e:  # reaper must never die silently
            LOGGER.error(f"Clip reaper failed waiting for ffmpeg: {type(e).__name__}: {e}")
            return

        returncode = recording.process.returncode
        stderr = (stderr_bytes or b"").decode(errors="replace").strip()[-STDERR_TAIL_CHARS:]
        # A clean SIGINT stop makes FFmpeg exit 255 — that is success, not failure.
        failed = returncode != 0 and not recording.stop_requested

        try:
            final = _finalize_part(recording.part_path)
        except OSError as e:
            final = None
            failed = True
            stderr = f"{stderr}\nfinalize failed: {e}".strip()

        if failed:
            self._last_error = f"ffmpeg exit {returncode}: {stderr or '(no stderr)'}"
            kept = f", partial clip kept: {final.name}" if final else ", no footage written"
            LOGGER.error(f"Clip recording FAILED (exit {returncode}){kept}\n{stderr}")
        elif final is None:
            self._last_error = "ffmpeg produced an empty clip"
            LOGGER.error(f"Clip recording produced no data ({recording.part_path.name})")
        else:
            self._last_error = None
            LOGGER.info(f"Clip saved: {final}")

    def is_recording(self) -> bool:
        with self._lock:
            return self._current is not None and self._current.process.poll() is None

    def stop_recording(self) -> None:
        """Ask FFmpeg to finish the clip (SIGINT), escalating to kill after STOP_GRACE_S.

        Non-blocking: called from the alert state machine's hot path.
        """
        with self._lock:
            recording = self._current
            self._current = None
        if recording is None or recording.process.poll() is not None:
            return

        recording.stop_requested = True
        recording.process.send_signal(signal.SIGINT)

        def _kill_if_alive() -> None:
            if recording.process.poll() is None:
                LOGGER.warning("Recording process did not stop after SIGINT, killing")
                recording.process.kill()

        timer = threading.Timer(STOP_GRACE_S, _kill_if_alive)
        timer.daemon = True
        timer.start()

    def last_error(self) -> str | None:
        return self._last_error


class MockRecorder:
    """No-op recorder for testing without FFmpeg or a live stream."""

    def __init__(self) -> None:
        self._recording = False
        self.capture_frame_calls: list[Path] = []
        self.start_recording_calls: list[tuple[Path, float]] = []
        self.stop_recording_calls: int = 0
        self.error: str | None = None

    def capture_frame(self, output_path: Path) -> None:
        self.capture_frame_calls.append(output_path)

    def start_recording(self, output_path: Path, duration_sec: float) -> None:
        if self._recording:
            raise RuntimeError("Already recording")
        self._recording = True
        self.start_recording_calls.append((output_path, duration_sec))

    def is_recording(self) -> bool:
        return self._recording

    def finish(self) -> None:
        """Simulate the clip reaching its duration cap and FFmpeg exiting on its own."""
        self._recording = False

    def stop_recording(self) -> None:
        self._recording = False
        self.stop_recording_calls += 1

    def last_error(self) -> str | None:
        return self.error
