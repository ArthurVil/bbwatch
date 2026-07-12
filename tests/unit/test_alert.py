"""Unit tests for AlertManager state machine."""

import json
import time
from unittest.mock import patch

import pytest

from bbwatch.alert import AlertManager, AlertState
from bbwatch.config import AlertConfig
from bbwatch.recording import MockRecorder


class TestAlertManager:
    """Tests for AlertManager state logic."""

    @pytest.fixture
    def config(self, tmp_path):
        """Test alert configuration."""
        return AlertConfig(
            status_file=tmp_path / "status.json",
            trigger_high=0.1,
            trigger_low=0.05,
            cooldown_s=1.0,  # Short cooldown for testing
        )

    @pytest.fixture
    def recorder(self):
        """Shared MockRecorder for inspecting side effects."""
        return MockRecorder()

    @pytest.fixture
    def manager(self, config, recorder):
        """AlertManager instance with injected MockRecorder."""
        return AlertManager(config, recorder=recorder)

    def test_initial_state(self, manager):
        """Should start in IDLE state."""
        assert manager.current_state == AlertState.IDLE

    def test_trigger_logic(self, manager):
        """Should trigger when intensity exceeds high threshold."""
        # Below threshold -> IDLE
        status = manager.process_intensity(0.05)
        assert manager.current_state == AlertState.IDLE
        assert status.alert_active is False

        # Above threshold -> TRIGGERED
        status = manager.process_intensity(0.12)
        assert manager.current_state == AlertState.TRIGGERED
        assert status.alert_active is True
        assert status.intensity == 0.12

    def test_hysteresis_logic(self, manager):
        """Should stay triggered until intensity drops below low threshold."""
        # Trigger
        manager.process_intensity(0.12)
        assert manager.current_state == AlertState.TRIGGERED

        # Between thresholds -> Still TRIGGERED
        manager.process_intensity(0.08)
        assert manager.current_state == AlertState.TRIGGERED

        # Below low threshold -> COOLDOWN
        status = manager.process_intensity(0.04)
        assert manager.current_state == AlertState.COOLDOWN
        assert status.alert_active is False  # Alert clears visually in cooldown

    def test_cooldown_logic(self, manager):
        """Should return to IDLE after cooldown."""
        with patch("bbwatch.alert.time.time") as mock_time:
            mock_time.return_value = 0.0
            manager.process_intensity(0.12)  # Trigger
            manager.process_intensity(0.04)  # Cooldown
            assert manager.current_state == AlertState.COOLDOWN

            # Advance clock past cooldown_s=1.0
            mock_time.return_value = 2.0
            manager.process_intensity(0.02)
            assert manager.current_state == AlertState.IDLE

    def test_retrigger_during_cooldown(self, manager):
        """Should re-trigger immediately if high intensity occurs during cooldown."""
        # Enter cooldown
        manager.process_intensity(0.12)
        manager.process_intensity(0.04)
        assert manager.current_state == AlertState.COOLDOWN

        # High intensity -> TRIGGERED
        manager.process_intensity(0.15)
        assert manager.current_state == AlertState.TRIGGERED

    def test_status_file_written(self, manager, config):
        """Status file should be updated on every process call."""
        manager.process_intensity(0.12)

        assert config.status_file.exists()
        data = json.loads(config.status_file.read_text())
        assert data["alert_state"] == "triggered"
        assert data["intensity"] == 0.12

    def test_heartbeat_refreshes_status_without_changing_state(self, manager, config):
        """heartbeat() must update the timestamp but never alter alert state.

        Regression: status.json was only written from process_intensity(),
        so a deployment with no audio source (camera-only) never refreshed
        it and looked permanently unhealthy after health_timeout_s. The main
        loop calls heartbeat() independently of audio activity.
        """
        manager.process_intensity(0.02)  # establish IDLE baseline, intensity 0.02
        first = json.loads(config.status_file.read_text())

        with patch("bbwatch.alert.time.time", return_value=first["timestamp"] + 5.0):
            status = manager.heartbeat()

        assert status.alert_state == first["alert_state"]
        assert status.intensity == first["intensity"] == 0.02
        assert status.timestamp == first["timestamp"] + 5.0

        second = json.loads(config.status_file.read_text())
        assert second["timestamp"] == first["timestamp"] + 5.0
        assert manager.current_state == AlertState.IDLE  # unchanged

    def test_heartbeat_and_process_intensity_writes_do_not_corrupt_status_file(self, manager, config):
        """Concurrent writers (main loop heartbeat + watchdog observer thread)
        must not interleave and corrupt status.json.
        """
        import threading

        stop = threading.Event()
        errors = []

        def hammer_heartbeat() -> None:
            while not stop.is_set():
                try:
                    manager.heartbeat()
                except Exception as e:  # noqa: BLE001 - captured for assertion
                    errors.append(e)

        def hammer_process() -> None:
            while not stop.is_set():
                try:
                    manager.process_intensity(0.01)
                except Exception as e:  # noqa: BLE001 - captured for assertion
                    errors.append(e)

        threads = [threading.Thread(target=hammer_heartbeat), threading.Thread(target=hammer_process)]
        for t in threads:
            t.start()
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)

        assert errors == []
        # File must always be valid, complete JSON — never a torn write.
        data = json.loads(config.status_file.read_text())
        assert "timestamp" in data and "alert_state" in data

    def test_recording_starts_on_trigger(self, manager, recorder):
        """Recording should start when alert is triggered."""
        manager.process_intensity(0.12)

        assert len(recorder.start_recording_calls) == 1
        path, duration = recorder.start_recording_calls[0]
        assert path.suffix == ".mp4"
        assert duration == manager.config.record_clip_s

    def test_recording_stops_on_idle(self, manager, recorder):
        """Recording should stop when alert returns to idle."""
        with patch("bbwatch.alert.time.time") as mock_time:
            mock_time.return_value = 0.0
            manager.process_intensity(0.12)  # trigger
            manager.process_intensity(0.04)  # cooldown
            mock_time.return_value = 2.0
            manager.process_intensity(0.02)  # idle

        assert recorder.stop_recording_calls == 1

    def test_no_duplicate_recording_on_retrigger(self, manager, recorder):
        """Re-triggering while recording is active should not start a second clip."""
        manager.process_intensity(0.12)  # trigger -> recording starts
        manager.process_intensity(0.04)  # cooldown
        manager.process_intensity(0.15)  # retrigger

        assert len(recorder.start_recording_calls) == 1

    def test_screenshot_taken_when_configured(self, config, tmp_path):
        """Screenshot should be captured when screenshot_on_peak=True."""
        config.screenshot_on_peak = True
        recorder = MockRecorder()
        manager = AlertManager(config, recorder=recorder)

        manager.process_intensity(0.12)
        # Give the background thread a moment to run
        time.sleep(0.05)

        assert len(recorder.capture_frame_calls) == 1
        assert recorder.capture_frame_calls[0].suffix == ".jpg"

    def test_no_screenshot_when_disabled(self, config):
        """No screenshot when screenshot_on_peak=False."""
        config.screenshot_on_peak = False
        recorder = MockRecorder()
        manager = AlertManager(config, recorder=recorder)

        manager.process_intensity(0.12)
        time.sleep(0.05)

        assert len(recorder.capture_frame_calls) == 0
