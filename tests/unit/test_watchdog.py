"""Unit tests for StreamWatchdog: viewer disconnects and frozen-video detection."""

import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bbwatch.config import WatchdogConfig
from bbwatch.watchdog import StreamWatchdog, video_bytes


def snapshot(video: int | None, audio: int = 0, consumers: int = 1) -> dict[str, Any]:
    """A go2rtc /api/streams entry shaped like the real babycam one."""
    receivers = [{"codec": {"codec_name": "pcm_mulaw", "codec_type": "audio"}, "bytes": audio}]
    if video is not None:
        receivers.insert(0, {"codec": {"codec_name": "h264", "codec_type": "video"}, "bytes": video})
    return {
        "producers": [{"url": "exec:ffmpeg ...", "receivers": receivers}],
        "consumers": [{"user_agent": f"viewer{i}"} for i in range(consumers)],
    }


@pytest.fixture
def config() -> WatchdogConfig:
    return WatchdogConfig(enabled=True, video_stall_s=20.0)


@pytest.fixture
def notifier() -> MagicMock:
    return MagicMock()


@pytest.fixture
def watchdog(config: WatchdogConfig, notifier: MagicMock) -> StreamWatchdog:
    return StreamWatchdog(config, notifier)


class TestVideoBytes:
    def test_sums_video_receivers_only(self) -> None:
        assert video_bytes(snapshot(video=1000, audio=50)) == 1000

    def test_none_without_producer(self) -> None:
        assert video_bytes({"consumers": []}) is None

    def test_none_when_bytes_missing(self) -> None:
        stream = {"producers": [{"receivers": [{"codec": {"codec_type": "video"}}]}]}
        assert video_bytes(stream) is None


class TestVideoStall:
    def test_frozen_video_with_viewers_alerts_once(self, watchdog, notifier, config) -> None:
        """The 2026-10-06 failure: video counter frozen while audio and viewers continue."""
        watchdog.check(snapshot(video=5000, audio=10), now=0.0)
        watchdog.check(snapshot(video=5000, audio=20), now=10.0)
        assert not watchdog.video_stalled
        watchdog.check(snapshot(video=5000, audio=30), now=20.0)
        assert watchdog.video_stalled
        watchdog.check(snapshot(video=5000, audio=40), now=30.0)  # still frozen: no repeat

        notifier.send.assert_called_once_with(config.video_stall_message)

    def test_logs_error_on_stall(self, watchdog) -> None:
        with patch("bbwatch.watchdog.LOGGER") as log:
            watchdog.check(snapshot(video=5000), now=0.0)
            watchdog.check(snapshot(video=5000), now=25.0)
        assert "VIDEO STALLED" in log.error.call_args[0][0]

    def test_advancing_video_never_alerts(self, watchdog, notifier) -> None:
        for i in range(10):
            watchdog.check(snapshot(video=1000 * (i + 1)), now=i * 10.0)
        assert not watchdog.video_stalled
        notifier.send.assert_not_called()

    def test_recovery_notifies_and_clears(self, watchdog, notifier, config) -> None:
        watchdog.check(snapshot(video=5000), now=0.0)
        watchdog.check(snapshot(video=5000), now=20.0)
        watchdog.check(snapshot(video=9000), now=30.0)

        assert not watchdog.video_stalled
        assert notifier.send.call_args_list[-1].args == (config.video_recovered_message,)

    def test_counter_reset_counts_as_progress(self, watchdog, notifier) -> None:
        """go2rtc restart resets byte counters — a drop is a change, not a stall."""
        watchdog.check(snapshot(video=900_000), now=0.0)
        watchdog.check(snapshot(video=100), now=15.0)
        watchdog.check(snapshot(video=100), now=30.0)  # only 15 s since last progress
        assert not watchdog.video_stalled
        notifier.send.assert_not_called()

    def test_no_viewers_is_not_a_stall(self, watchdog, notifier) -> None:
        """On-demand stream idling with nobody watching must not alert."""
        watchdog.check(snapshot(video=5000, consumers=0), now=0.0)
        watchdog.check(snapshot(video=5000, consumers=0), now=100.0)
        assert not watchdog.video_stalled
        notifier.send.assert_not_called()

    def test_stall_timer_restarts_when_viewers_return(self, watchdog) -> None:
        watchdog.check(snapshot(video=5000), now=0.0)
        watchdog.check(snapshot(video=5000, consumers=0), now=10.0)
        watchdog.check(snapshot(video=5000), now=25.0)  # viewer back: timer starts now
        assert not watchdog.video_stalled
        watchdog.check(snapshot(video=5000), now=45.0)
        assert watchdog.video_stalled

    def test_no_producer_is_not_a_stall(self, watchdog, notifier) -> None:
        watchdog.check({"consumers": [{}]}, now=0.0)
        watchdog.check({"consumers": [{}]}, now=100.0)
        assert not watchdog.video_stalled


class TestViewerDisconnect:
    def test_disconnect_still_alerts(self, watchdog, notifier, config) -> None:
        watchdog.check(snapshot(video=1, consumers=2), now=0.0)
        watchdog.check(snapshot(video=2, consumers=0), now=10.0)
        notifier.send.assert_called_once_with(config.alert_message)

    def test_no_alert_on_first_sample(self, watchdog, notifier) -> None:
        watchdog.check(snapshot(video=1, consumers=0), now=0.0)
        notifier.send.assert_not_called()


class TestPollLoop:
    def test_poll_errors_do_not_kill_thread(self, config, notifier) -> None:
        """Failure path: go2rtc unreachable must be logged and retried, never end the thread."""
        config.poll_interval_s = 1.0
        wd = StreamWatchdog(config, notifier)
        calls = threading.Event()
        attempts = []

        def failing_fetch() -> dict[str, Any]:
            attempts.append(1)
            if len(attempts) >= 2:
                calls.set()
            raise RuntimeError("go2rtc unreachable")

        with patch.object(wd, "_fetch_stream", side_effect=failing_fetch):
            wd.start()
            try:
                assert calls.wait(timeout=5)
                assert wd._thread is not None and wd._thread.is_alive()
            finally:
                wd.stop()
