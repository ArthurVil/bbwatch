"""Unit tests for the legacy status-file overlay controller (bbwatch/overlay.py)."""

import json
import logging
import time

import pytest

from bbwatch.overlay import ALERT_CRY, ALERT_ERROR, ALERT_NONE, OverlayController, get_alert_state


@pytest.fixture
def log_capture():
    """Capture bbwatch.overlay records with a plain handler.

    pytest's caplog fixture is unreliable in this environment (third-party
    pytest plugins interfere with the logging plugin), so attach our own —
    see tests/unit/test_latency.py for the same workaround.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("bbwatch.overlay")
    handler = _Collector(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def _write_status(path, **overrides):
    status = {
        "system_status": "ok",
        "alert_state": "idle",
        "alert_active": False,
        "timestamp": time.time(),
        "intensity": 0.0,
    }
    status.update(overrides)
    path.write_text(json.dumps(status))


class TestGetAlertState:
    def test_missing_file_is_error(self, tmp_path):
        assert get_alert_state(tmp_path / "missing.json") == ALERT_ERROR

    def test_fresh_status_is_none(self, tmp_path):
        status_file = tmp_path / "status.json"
        _write_status(status_file)
        assert get_alert_state(status_file, health_timeout_s=10.0) == ALERT_NONE

    def test_stale_status_is_error(self, tmp_path):
        status_file = tmp_path / "status.json"
        _write_status(status_file, timestamp=time.time() - 100.0)
        assert get_alert_state(status_file, health_timeout_s=3.0) == ALERT_ERROR

    def test_active_alert_is_cry(self, tmp_path):
        status_file = tmp_path / "status.json"
        _write_status(status_file, alert_active=True)
        assert get_alert_state(status_file, health_timeout_s=10.0) == ALERT_CRY

    def test_stale_check_does_not_log_every_call(self, tmp_path, log_capture):
        """get_alert_state itself must not spam WARNING per call.

        Regression: this used to log a WARNING on every call, and it's
        polled at 20Hz from the main loop — a permanently stale status
        file produced up to 20 duplicate log lines per second.
        """
        status_file = tmp_path / "status.json"
        _write_status(status_file, timestamp=time.time() - 100.0)

        for _ in range(20):
            get_alert_state(status_file, health_timeout_s=3.0)

        assert not any("stale" in r.getMessage().lower() for r in log_capture)


class TestOverlayControllerLogging:
    """Staleness must still be reported loudly — once per transition, not per poll."""

    def test_transition_into_error_logs_warning_once(self, tmp_path, log_capture):
        status_file = tmp_path / "status.json"
        _write_status(status_file, timestamp=time.time() - 100.0)
        controller = OverlayController(overlay_dir=tmp_path / "overlays", status_file=status_file, poll_interval_s=0.0)
        controller.setup()

        for _ in range(5):
            controller.update()

        warnings = [r for r in log_capture if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "error" in warnings[0].getMessage().lower()

    def test_recovery_then_re_staleness_logs_again(self, tmp_path, log_capture):
        status_file = tmp_path / "status.json"
        _write_status(status_file, timestamp=time.time() - 100.0)
        controller = OverlayController(overlay_dir=tmp_path / "overlays", status_file=status_file, poll_interval_s=0.0)
        controller.setup()

        controller.update()  # -> error (1st warning)
        _write_status(status_file)  # fresh
        controller.update()  # -> none (info)
        _write_status(status_file, timestamp=time.time() - 100.0)  # stale again
        controller.update()  # -> error (2nd warning)

        warnings = [r for r in log_capture if r.levelno == logging.WARNING]
        assert len(warnings) == 2
