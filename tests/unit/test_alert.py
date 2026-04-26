"""Unit tests for AlertManager state machine."""

import json
import time

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
        # Enter cooldown
        manager.process_intensity(0.12)  # Trigger
        manager.process_intensity(0.04)  # Cooldown
        assert manager.current_state == AlertState.COOLDOWN

        # Wait for cooldown
        time.sleep(1.1)

        # Process low intensity -> IDLE
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

    def test_recording_starts_on_trigger(self, manager, recorder):
        """Recording should start when alert is triggered."""
        manager.process_intensity(0.12)

        assert len(recorder.start_recording_calls) == 1
        path, duration = recorder.start_recording_calls[0]
        assert path.suffix == ".mp4"
        assert duration == manager.config.record_clip_s

    def test_recording_stops_on_idle(self, manager, recorder):
        """Recording should stop when alert returns to idle."""
        manager.process_intensity(0.12)  # trigger
        manager.process_intensity(0.04)  # cooldown
        time.sleep(1.1)
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
