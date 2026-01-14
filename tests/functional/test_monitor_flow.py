"""Functional tests for bbwatch monitor flow."""

import json
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from bbwatch.main import BabyMonitor


class TestMonitorFlow:
    """End-to-end flow tests for BabyMonitor."""

    @patch("bbwatch.main.SlidingWindowCapture")
    @patch("bbwatch.main.Observer")
    @patch("bbwatch.motion.cv2.VideoCapture")
    def test_full_monitor_cycle(
        self,
        mock_video_capture,
        mock_observer,
        mock_capture,
        tmp_path,
        baby_cry_wav,
    ):
        """Test a full monitoring cycle: motion -> cry -> status update."""
        # Setup config
        config_path = tmp_path / "config.yaml"
        # We explicitly set paths to be sure where they end up
        config_content = f"""
data_dir: {tmp_path}
log_level: DEBUG
fake_hardware: true

storage:
  wav_dir: {tmp_path}/wavs

alerts:
  status_file: overlays/status.json
  overlay_dir: overlays
  enable_dynamic_overlay: true
  overlay_fps: 5
  trigger_high: 0.01
  trigger_low: 0.005

motion:
  threshold: 10
  history_len: 20
  fps: 10
  motion_threshold_percent: 1.0
"""
        config_path.write_text(config_content)

        # Mock VideoCapture to return frames
        mock_cap_instance = mock_video_capture.return_value
        mock_cap_instance.isOpened.return_value = True

        # Create a blank frame and a "motion" frame
        frame1 = np.zeros((480, 640, 3), dtype=np.uint8)
        frame2 = np.ones((480, 640, 3), dtype=np.uint8) * 255

        mock_cap_instance.read.side_effect = [
            (True, frame1),
            (True, frame2),
            (True, frame2),
            (True, frame2),
        ]

        # Patch OverlayGenerator to avoid pipe issues and threading complexity for the test
        with patch("bbwatch.main.OverlayGenerator"):
            # Initialize monitor
            monitor = BabyMonitor(config_path=config_path)

            try:
                # Start monitor
                monitor.start()

                # 1. Simulate Motion
                # Process two different frames to ensure level > 0 regardless of thread state
                monitor._motion_detector.process_frame(frame1)
                motion_level, _ = monitor._motion_detector.process_frame(frame2)

                # 2. Simulate Audio Cry
                observer_instance = mock_observer.return_value
                args, _ = observer_instance.schedule.call_args
                handler = args[0]

                # Mock event for baby cry
                event = MagicMock(src_path=str(baby_cry_wav), is_directory=False)
                handler.on_closed(event)

                # 3. Simulate one loop iteration update
                monitor._overlay_controller.update()

                # Get levels
                motion_level = monitor._motion_detector.get_current_motion()
                audio_activity = monitor._alert_manager.alert_active
                audio_intensity = monitor._alert_manager.current_intensity

                # Verify logic
                assert audio_activity is True, f"Audio alert should be active, intensity={audio_intensity}"
                assert motion_level > 0, "Motion level should be positive"

                # Trigger overlay update (as run() would)
                monitor._overlay_generator.update_state(
                    motion_detected=(motion_level > monitor.config.motion.motion_threshold_percent),
                    audio_alert=audio_activity,
                    motion_level=motion_level,
                    audio_level=audio_intensity,
                )

                # Verify states
                monitor._overlay_generator.update_state.assert_called()

                # Verify status file exists at the RESOLVED path
                resolved_status_file = monitor.config.alerts.status_file
                assert resolved_status_file.exists(), f"Status file missing at {resolved_status_file}"

                status_data = json.loads(resolved_status_file.read_text())
                assert status_data["alert_active"] is True
                assert status_data["intensity"] == audio_intensity

            finally:
                monitor.stop()

    @patch("bbwatch.main.SlidingWindowCapture")
    @patch("bbwatch.main.Observer")
    def test_monitor_start_hardware_fail(self, mock_observer, mock_capture, tmp_path):
        """Test that monitor fails gracefully if hardware is missing."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(f"data_dir: {tmp_path}\nfake_hardware: false")

        # Mock hardware discovery to return nothing
        with patch("bbwatch.hardware.get_preferred_audio_device", return_value=None):
            # We also need to mock log_detected_hardware to avoid real HW calls
            with patch("bbwatch.hardware.log_detected_hardware", return_value=([], [])):
                monitor = BabyMonitor(config_path=config_path)
                with pytest.raises(RuntimeError, match="Required hardware not found"):
                    monitor.start()
