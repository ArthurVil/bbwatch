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
        assert duration == manager.config.record_max_s

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
    """Motion as a second alert source: a few frames above threshold within a window."""

    @pytest.fixture
    def config(self, tmp_path):
        return AlertConfig(
            status_file=tmp_path / "status.json",
            clips_dir=tmp_path / "clips",
            screenshots_dir=tmp_path / "screenshots",
            trigger_high=0.1,
            trigger_low=0.05,
            cooldown_s=1.0,
            motion_min_frames=3,
            motion_window_s=1.0,
            screenshot_on_peak=False,
        )

    @pytest.fixture
    def recorder(self):
        return MockRecorder()

    @pytest.fixture
    def manager(self, config, recorder):
        return AlertManager(config, recorder=recorder)

    @staticmethod
    def feed(manager, t, frames, start_seq=0, dt=0.1):
        """Feed one call per frame: frames is a sequence of motion_detected bools."""
        seq = start_seq
        for detected in frames:
            seq += 1
            manager.process_motion(detected, sample_id=seq)
            t.return_value += dt
        return seq

    def test_few_frames_above_threshold_trigger_and_record(self, manager, recorder):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            self.feed(manager, t, [True, True])
            assert manager.current_state == AlertState.IDLE  # 2 < motion_min_frames
            status = manager.process_motion(True, sample_id=3)

        assert manager.current_state == AlertState.TRIGGERED
        assert status.trigger_source == "motion"
        assert len(recorder.start_recording_calls) == 1

    def test_non_consecutive_frames_still_trigger(self, manager):
        """Real movement flickers frame to frame — gaps inside the window must not reset the count."""
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            self.feed(manager, t, [True, False, True, False, False, True])

        assert manager.current_state == AlertState.TRIGGERED

    def test_single_frame_spike_does_not_trigger(self, manager, recorder):
        """A one-frame lighting/exposure jump must not start a recording."""
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            self.feed(manager, t, [False, True] + [False] * 20)

        assert manager.current_state == AlertState.IDLE
        assert recorder.start_recording_calls == []

    def test_frames_spread_beyond_window_do_not_trigger(self, manager):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            self.feed(manager, t, [True, False, False, False, False, False, True], dt=0.3)

        assert manager.current_state == AlertState.IDLE  # 2 frames per 1 s window at most

    def test_repeated_poll_of_same_frame_counts_once(self, manager):
        """The main loop polls at 20 Hz but frames arrive at ~10 fps: re-reads must not inflate the count."""
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            for _ in range(10):
                manager.process_motion(True, sample_id=7)
                t.return_value += 0.05

        assert manager.current_state == AlertState.IDLE

    def test_motion_disabled_by_config(self, config, recorder):
        config.motion_triggers_alert = False
        manager = AlertManager(config, recorder=recorder)
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            self.feed(manager, t, [True] * 10)

        assert manager.current_state == AlertState.IDLE
        assert recorder.start_recording_calls == []

    def test_audio_and_motion_overlap_single_recording(self, manager, recorder):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_intensity(0.2)  # audio triggers
            self.feed(manager, t, [True] * 3)
            status = manager.process_motion(True, sample_id=4)

        assert status.trigger_source == "both"
        assert len(recorder.start_recording_calls) == 1

    def test_stays_triggered_while_either_input_hot(self, manager, recorder):
        """Audio going quiet must not end the alert while motion frames keep coming."""
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_intensity(0.2)
            seq = self.feed(manager, t, [True] * 3)
            manager.process_intensity(0.01)  # audio quiet, motion still hot
            assert manager.current_state == AlertState.TRIGGERED
            assert manager.trigger_source == "motion"

            # No motion frames for > motion_window_s: motion goes cold -> cooldown
            seq = self.feed(manager, t, [False] * 11, start_seq=seq)
            assert manager.current_state == AlertState.COOLDOWN
            self.feed(manager, t, [False] * 11, start_seq=seq)  # > cooldown_s

        assert manager.current_state == AlertState.IDLE
        assert recorder.stop_recording_calls == 1

    def test_motion_only_reaches_idle_without_audio(self, manager, recorder):
        """Camera-only deployments: motion calls alone must drive cooldown -> idle."""
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            self.feed(manager, t, [True] * 3 + [False] * 30)

        assert manager.current_state == AlertState.IDLE
        assert recorder.stop_recording_calls == 1

    def test_motion_does_not_light_cry_indicator(self, manager):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            self.feed(manager, t, [True] * 3)

        assert manager.alert_active is True
        assert manager.audio_active is False

    def test_trigger_source_written_to_status_file(self, manager, config):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            self.feed(manager, t, [True] * 3)

        data = json.loads(config.status_file.read_text())
        assert data["alert_state"] == "triggered"
        assert data["trigger_source"] == "motion"

    def test_steady_state_does_not_rewrite_status_every_call(self, manager):
        """process_motion runs at ~20 Hz — only state changes may hit the disk."""
        with patch.object(manager, "_write_status") as write:
            for i in range(50):
                manager.process_motion(False, sample_id=i)
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
                    manager.process_motion(i % 3 == 0, sample_id=i)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=audio), threading.Thread(target=motion)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert errors == []
        assert manager.current_state in set(AlertState)
        assert len(recorder.start_recording_calls) >= 1


class TestClipLength:
    """Clips run for the whole alert, capped at record_max_s with rollover."""

    @pytest.fixture
    def config(self, tmp_path):
        return AlertConfig(
            status_file=tmp_path / "status.json",
            clips_dir=tmp_path / "clips",
            screenshots_dir=tmp_path / "screenshots",
            trigger_high=0.1,
            trigger_low=0.05,
            cooldown_s=1.0,
            screenshot_on_peak=False,
        )

    @pytest.fixture
    def recorder(self):
        return MockRecorder()

    @pytest.fixture
    def manager(self, config, recorder):
        return AlertManager(config, recorder=recorder)

    def test_clip_capped_at_record_max_s(self, manager, recorder):
        manager.process_intensity(0.2)
        _, max_s = recorder.start_recording_calls[0]
        assert max_s == 300.0

    def test_clip_saved_in_day_folder(self, manager, recorder, config):
        manager.process_intensity(0.2)
        path, _ = recorder.start_recording_calls[0]
        assert path.parent.parent == config.clips_dir
        assert len(path.parent.name) == len("2026-10-06")

    def test_rollover_when_cap_reached_during_alert(self, manager, recorder):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_intensity(0.2)
            recorder.finish()  # ffmpeg hit -t record_max_s and exited
            t.return_value = 300.0
            manager.process_intensity(0.2)

        assert len(recorder.start_recording_calls) == 2

    def test_no_rollover_during_cooldown(self, manager, recorder):
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_intensity(0.2)
            t.return_value = 300.0
            manager.process_intensity(0.01)  # cooldown
            recorder.finish()
            t.return_value = 300.5
            manager.process_intensity(0.01)

        assert len(recorder.start_recording_calls) == 1

    def test_failed_clip_retry_is_rate_limited(self, manager, recorder):
        """Failure path: ffmpeg dying instantly (stream down) must not be respawned at loop rate."""
        with patch("bbwatch.alert.time.time") as t:
            t.return_value = 0.0
            manager.process_intensity(0.2)
            for i in range(1, 40):  # 2 s of updates at 20 Hz, each time ffmpeg already dead
                recorder.finish()
                t.return_value = i * 0.05
                manager.process_intensity(0.2)
            assert len(recorder.start_recording_calls) == 1

            recorder.finish()
            t.return_value = 5.1  # past RECORD_RETRY_S
            manager.process_intensity(0.2)

        assert len(recorder.start_recording_calls) == 2

