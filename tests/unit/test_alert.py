"""Unit tests for AlertManager state machine."""

import json
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

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

    def test_write_status_oserror_is_logged_not_raised(self, manager, config):
        """Failure path: a status.json write failure (disk full, permission
        denied) must be logged and swallowed, never propagate up and kill
        the calling thread (watchdog observer or main loop).
        """
        with patch("builtins.open", side_effect=OSError("disk full")):
            status = manager.process_intensity(0.12)  # must not raise

        assert status.alert_active is True  # state machine still advanced

    def test_trigger_handles_recorder_already_recording(self, config):
        """RuntimeError from start_recording (already recording) must be
        caught and logged, not propagate and break the alert state machine.
        """
        recorder = MagicMock()
        recorder.is_recording.return_value = False
        recorder.start_recording.side_effect = RuntimeError("Already recording")
        manager = AlertManager(config, recorder=recorder)

        status = manager.process_intensity(0.12)  # must not raise

        assert status.alert_active is True
        assert manager.current_state == AlertState.TRIGGERED

    def test_trigger_handles_ffmpeg_not_found(self, config):
        """FileNotFoundError from start_recording (ffmpeg missing) must be
        caught and logged, not crash the alert pipeline.
        """
        recorder = MagicMock()
        recorder.is_recording.return_value = False
        recorder.start_recording.side_effect = FileNotFoundError("ffmpeg")
        manager = AlertManager(config, recorder=recorder)

        status = manager.process_intensity(0.12)  # must not raise

        assert status.alert_active is True
        assert manager.current_state == AlertState.TRIGGERED

    def test_screenshot_failure_is_caught_in_background_thread(self, config):
        """Screenshot capture errors (timeout, non-zero exit, missing ffmpeg)
        must not crash the daemon thread they run on.
        """
        config.screenshot_on_peak = True
        recorder = MagicMock()
        recorder.is_recording.return_value = False
        recorder.capture_frame.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=10)
        manager = AlertManager(config, recorder=recorder)

        manager.process_intensity(0.12)
        time.sleep(0.05)  # let the background screenshot thread run and raise

        recorder.capture_frame.assert_called_once()

    def test_no_screenshot_when_disabled(self, config):
        """No screenshot when screenshot_on_peak=False."""
        config.screenshot_on_peak = False
        recorder = MockRecorder()
        manager = AlertManager(config, recorder=recorder)

        manager.process_intensity(0.12)
        time.sleep(0.05)

        assert len(recorder.capture_frame_calls) == 0


class TestMotionTrigger:
    """Motion as a second alert source alongside audio."""

    @pytest.fixture
    def config(self, tmp_path):
        return AlertConfig(
            status_file=tmp_path / "status.json",
            clips_dir=tmp_path / "clips",
            screenshots_dir=tmp_path / "screenshots",
            trigger_high=0.1,
            trigger_low=0.05,
            cooldown_s=1.0,
            motion_min_s=1.0,
            screenshot_on_peak=False,
        )

    @pytest.fixture
    def recorder(self):
        return MockRecorder()

    @pytest.fixture
    def manager(self, config, recorder):
        return AlertManager(config, recorder=recorder)

    def test_sustained_motion_triggers_and_records(self, manager, recorder):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_motion(True)
            assert manager.current_state == AlertState.IDLE  # not sustained yet
            t.return_value = 1.1
            status = manager.process_motion(True)

        assert manager.current_state == AlertState.TRIGGERED
        assert status.trigger_source == "motion"
        assert len(recorder.start_recording_calls) == 1

    def test_motion_blip_does_not_trigger(self, manager, recorder):
        """A single-frame flicker (lighting, IR switch) must not start a recording."""
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_motion(True)
            t.return_value = 0.5
            manager.process_motion(False)  # streak broken
            t.return_value = 0.9
            manager.process_motion(True)
            t.return_value = 1.5  # 0.6 s into the new streak
            manager.process_motion(True)

        assert manager.current_state == AlertState.IDLE
        assert recorder.start_recording_calls == []

    def test_motion_disabled_by_config(self, config, recorder):
        config.motion_triggers_alert = False
        manager = AlertManager(config, recorder=recorder)
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_motion(True)
            t.return_value = 5.0
            manager.process_motion(True)

        assert manager.current_state == AlertState.IDLE
        assert recorder.start_recording_calls == []

    def test_audio_and_motion_overlap_single_recording(self, manager, recorder):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_intensity(0.2)  # audio triggers
            manager.process_motion(True)
            t.return_value = 1.5
            status = manager.process_motion(True)

        assert status.trigger_source == "both"
        assert len(recorder.start_recording_calls) == 1

    def test_stays_triggered_while_either_input_hot(self, manager, recorder):
        """Audio going quiet must not end the alert while motion is still sustained."""
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_intensity(0.2)
            manager.process_motion(True)
            t.return_value = 1.5
            manager.process_motion(True)
            t.return_value = 2.0
            manager.process_intensity(0.01)  # audio quiet, motion still hot
            assert manager.current_state == AlertState.TRIGGERED
            assert manager.trigger_source == "motion"

            manager.process_motion(False)  # both quiet -> cooldown
            assert manager.current_state == AlertState.COOLDOWN
            t.return_value = 2.5
            manager.process_motion(False)
            assert manager.current_state == AlertState.COOLDOWN  # cooldown not elapsed
            t.return_value = 3.1
            manager.process_motion(False)

        assert manager.current_state == AlertState.IDLE
        assert recorder.stop_recording_calls == 1

    def test_motion_only_reaches_idle_without_audio(self, manager, recorder):
        """Camera-only deployments: motion calls alone must drive cooldown -> idle."""
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_motion(True)
            t.return_value = 1.0
            manager.process_motion(True)
            t.return_value = 1.2
            manager.process_motion(False)
            t.return_value = 2.5
            manager.process_motion(False)

        assert manager.current_state == AlertState.IDLE
        assert recorder.stop_recording_calls == 1

    def test_motion_does_not_light_cry_indicator(self, manager):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_motion(True)
            t.return_value = 1.0
            manager.process_motion(True)

        assert manager.alert_active is True
        assert manager.audio_active is False

    def test_trigger_source_written_to_status_file(self, manager, config):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_motion(True)
            t.return_value = 1.0
            manager.process_motion(True)

        data = json.loads(config.status_file.read_text())
        assert data["alert_state"] == "triggered"
        assert data["trigger_source"] == "motion"

    def test_steady_motion_does_not_rewrite_status_every_call(self, manager, config):
        """process_motion runs at ~20 Hz — only state changes may hit the disk."""
        with patch.object(manager, "_write_status") as write:
            for _ in range(50):
                manager.process_motion(False)
        write.assert_not_called()

    def test_concurrent_audio_and_motion_updates(self, manager, recorder):
        """Audio (watchdog thread) and motion (main loop) must not corrupt the state machine."""
        errors: list[Exception] = []

        def audio():
            try:
                for i in range(300):
                    manager.process_intensity(0.2 if i % 2 else 0.0)
            except Exception as e:  # pragma: no cover - surfaced via assertion
                errors.append(e)

        def motion():
            try:
                for i in range(300):
                    manager.process_motion(i % 3 == 0)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=audio), threading.Thread(target=motion)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert errors == []
        assert manager.current_state in set(AlertState)
        # Never two concurrent recordings: MockRecorder raises on a second start.
        assert len(recorder.start_recording_calls) >= 1
