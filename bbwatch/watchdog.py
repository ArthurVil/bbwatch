"""Stream watchdog.

Polls the go2rtc REST API and fires a notification when:

- all viewers disconnect from a stream (consumer count transitions from >0 to 0)
- the stream's video freezes while it is being watched: the producer's video
  byte counter stops advancing even though consumers are connected. This
  happens when bbwatch restarts without go2rtc — go2rtc's compositing ffmpeg
  hits EOF on the overlay FIFO and never resumes, so the live view and alert
  clips silently stop getting new frames while audio keeps flowing.
"""

import base64
import json
import logging
import threading
import time
import urllib.request
from typing import Any
from urllib.error import URLError

from bbwatch.config import WatchdogConfig
from bbwatch.notifier import Notifier

LOGGER = logging.getLogger(__name__)


def video_bytes(stream: dict[str, Any]) -> int | None:
    """Total video bytes received by a go2rtc stream's producers.

    Returns:
        Byte count, or None when the stream has no producer reporting a
        video receiver (e.g. an on-demand stream nobody is watching).
    """
    total: int | None = None
    for producer in stream.get("producers") or []:
        for receiver in producer.get("receivers") or []:
            codec = receiver.get("codec") or {}
            if codec.get("codec_type") == "video" and isinstance(receiver.get("bytes"), int):
                total = (total or 0) + receiver["bytes"]
    return total


class StreamWatchdog:
    """Background thread that alerts on viewer disconnects and frozen video."""

    def __init__(self, config: WatchdogConfig, notifier: Notifier) -> None:
        """Initialize the watchdog.

        Args:
            config: Poll interval, target stream, and go2rtc API credentials.
            notifier: Sink for the alerts (see `notifier.py`).
        """
        self._config = config
        self._notifier = notifier
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_count: int | None = None  # None = not yet sampled; avoids false alert on startup
        self._last_video_bytes: int | None = None
        self._video_progress_at: float | None = None  # last time video bytes advanced
        self._video_stalled = False

    @property
    def video_stalled(self) -> bool:
        """True while the watched stream's video is frozen."""
        return self._video_stalled

    def start(self) -> None:
        """Start the background polling thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="stream-watchdog")
        self._thread.start()
        LOGGER.info(
            f"Stream watchdog started (stream={self._config.stream_name}, "
            f"interval={self._config.poll_interval_s}s, video_stall={self._config.video_stall_s}s, "
            f"notifier={self._config.notifier.value})"
        )

    def stop(self) -> None:
        """Signal the polling thread to exit and join it (bounded wait)."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        """Poll loop.

        Poll errors (go2rtc unreachable, malformed response) are logged and
        retried on the next interval — they must never kill this thread,
        since a dead watchdog silently stops alerting.
        """
        while not self._stop.wait(self._config.poll_interval_s):
            try:
                self.check(self._fetch_stream(), time.monotonic())
            except Exception as e:
                LOGGER.warning(f"Watchdog poll error: {e}")

    def check(self, stream: dict[str, Any], now: float) -> None:
        """Evaluate one go2rtc stream snapshot (the poll body, separated for testing)."""
        count = len(stream.get("consumers") or [])
        if self._last_count is not None and self._last_count > 0 and count == 0:
            LOGGER.info(f"Viewer disconnected from '{self._config.stream_name}'")
            self._notifier.send(self._config.alert_message)
        self._last_count = count

        self._check_video(video_bytes(stream), count, now)

    def _check_video(self, current: int | None, consumers: int, now: float) -> None:
        """Track video progress; alert once on a stall and once on recovery."""
        if current is None or consumers == 0:
            # Nothing flowing because nobody is watching (on-demand stream):
            # not a stall. Restart tracking when it comes back.
            self._last_video_bytes = None
            self._video_progress_at = None
            return

        if current != self._last_video_bytes:
            # Any change counts as progress (a go2rtc restart resets the counter)
            self._last_video_bytes = current
            self._video_progress_at = now
            if self._video_stalled:
                self._video_stalled = False
                LOGGER.info(f"Video on '{self._config.stream_name}' is flowing again")
                self._notifier.send(self._config.video_recovered_message)
            return

        if self._video_progress_at is None:
            self._video_progress_at = now
        frozen_for = now - self._video_progress_at
        if not self._video_stalled and frozen_for >= self._config.video_stall_s:
            self._video_stalled = True
            LOGGER.error(
                f"VIDEO STALLED on '{self._config.stream_name}': no new video for {frozen_for:.0f} s "
                f"with {consumers} viewer(s) connected — live view and alert clips are frozen. "
                "Restart go2rtc (sudo systemctl restart go2rtc, or make restart)."
            )
            self._notifier.send(self._config.video_stall_message)

    def _fetch_stream(self) -> dict[str, Any]:
        """Return the go2rtc API entry for the configured stream ({} if absent)."""
        url = f"{self._config.go2rtc_api_url}/api/streams"
        req = urllib.request.Request(url)

        if self._config.go2rtc_username:
            credentials = base64.b64encode(
                f"{self._config.go2rtc_username}:{self._config.go2rtc_password}".encode()
            ).decode()
            req.add_header("Authorization", f"Basic {credentials}")

        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
        except URLError as e:
            raise RuntimeError(f"go2rtc unreachable at {url}: {e}") from e

        stream = data.get(self._config.stream_name) or {}
        if not isinstance(stream, dict):
            raise RuntimeError(f"unexpected go2rtc response for '{self._config.stream_name}'")
        return stream
