"""Viewer connectivity watchdog.

Polls the go2rtc REST API and fires a notification when all viewers
disconnect from a stream (consumer count transitions from >0 to 0).
"""

import base64
import json
import logging
import threading
import urllib.request
from urllib.error import URLError

from bbwatch.config import WatchdogConfig
from bbwatch.notifier import Notifier

LOGGER = logging.getLogger(__name__)


class StreamWatchdog:
    """Background thread that alerts when stream viewers disconnect."""

    def __init__(self, config: WatchdogConfig, notifier: Notifier) -> None:
        """Initialize the watchdog.

        Args:
            config: Poll interval, target stream, and go2rtc API credentials.
            notifier: Sink for the disconnect alert (see `notifier.py`).
        """
        self._config = config
        self._notifier = notifier
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_count: int | None = None  # None = not yet sampled; avoids false alert on startup

    def start(self) -> None:
        """Start the background polling thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="stream-watchdog")
        self._thread.start()
        LOGGER.info(
            f"Stream watchdog started (stream={self._config.stream_name}, "
            f"interval={self._config.poll_interval_s}s, notifier={self._config.notifier.value})"
        )

    def stop(self) -> None:
        """Signal the polling thread to exit and join it (bounded wait)."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        """Poll loop: fires the notifier on a >0 -> 0 consumer-count transition.

        Poll errors (go2rtc unreachable, malformed response) are logged and
        retried on the next interval — they must never kill this thread,
        since a dead watchdog silently stops alerting on disconnects.
        """
        while not self._stop.wait(self._config.poll_interval_s):
            try:
                count = self._consumer_count()
                if self._last_count is not None and self._last_count > 0 and count == 0:
                    LOGGER.info(f"Viewer disconnected from '{self._config.stream_name}'")
                    self._notifier.send(self._config.alert_message)
                self._last_count = count
            except Exception as e:
                LOGGER.warning(f"Watchdog poll error: {e}")

    def _consumer_count(self) -> int:
        """Return the number of active consumers on the configured stream."""
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

        stream = data.get(self._config.stream_name, {})
        return len(stream.get("consumers", []))
