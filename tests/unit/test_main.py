from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bbwatch.main import BabyMonitor


@pytest.fixture
def mock_components():
    with (
        patch("bbwatch.main.SlidingWindowCapture") as cap,
        patch("bbwatch.main.StorageManager") as store,
        patch("bbwatch.main.Observer") as obs,
        patch("bbwatch.main.OverlayController") as ov_ctrl,
        patch("bbwatch.main.OverlayGenerator") as ov_gen,
        patch("bbwatch.main.MotionDetector") as mot,
        patch("bbwatch.main.AlertManager") as alert,
        patch("bbwatch.main.SegmentHandler") as seg_handler,
        patch("bbwatch.main.get_default_config_path") as get_cfg,
    ):
        get_cfg.return_value = Path("config.yaml.example")

        yield {
            "capture": cap,
            "storage": store,
            "observer": obs,
            "overlay_controller": ov_ctrl,
            "overlay_generator": ov_gen,
            "motion": mot,
            "alert": alert,
            "segment_handler": seg_handler,
        }


@pytest.fixture
def monitor(mock_components, tmp_path):
    # Mock config loading to avoid reading real file
    with patch("bbwatch.main.BBWatchConfig") as MockConfig:
        config_inst = MagicMock()
        MockConfig.from_yaml.return_value = config_inst
        config_inst.resolve_paths.return_value = config_inst

        # Setup fake config
        config_inst.storage.wav_dir = tmp_path / "wav"
        config_inst.alerts.overlay_dir = tmp_path / "overlays"
        config_inst.alerts.status_file = tmp_path / "status.json"
        config_inst.log_level = "INFO"
        config_inst.fake_hardware = True  # Default to fake hardware to bypass detection logic complexities
        config_inst.motion.motion_threshold_percent = 5.0  # Float value for comparisons

        mon = BabyMonitor(config_path=Path("dummy.yaml"))
        return mon


def test_initialization(monitor):
    assert monitor.config is not None
    assert monitor._running is False


def test_detect_hardware_fake(monitor):
    monitor.config.fake_hardware = True
    assert monitor._detect_hardware() is True
    assert monitor._audio_source is not None
    assert monitor._video_source is not None
    # With fake_hardware=True, we get fake ALSA/V4L2 devices from HardwareDetector
    assert "ALSA" in repr(monitor._audio_source) or "Mock" in repr(monitor._audio_source)


def test_detect_hardware_network(monitor):
    monitor.config.fake_hardware = False
    monitor.config.audio.device_index = "rtsp://test"

    assert monitor._detect_hardware() is True
    assert monitor._audio_source is not None
    assert "RTSP" in repr(monitor._audio_source)
    assert monitor._video_source is not None
    assert "RTSP" in repr(monitor._video_source)


def test_detect_hardware_local_fail(monitor):
    monitor.config.fake_hardware = False
    monitor.config.audio.device_index = 0  # Local

    # Mock HardwareDetector to return no devices
    with patch("bbwatch.main.HardwareDetector") as mock_detector_class:
        mock_detector = MagicMock()
        mock_detector_class.return_value = mock_detector
        mock_detector.detect_audio_devices.return_value = []
        mock_detector.detect_picamera_devices.return_value = []
        mock_detector.detect_video_devices.return_value = []

        # Even with no hardware, fallback to mock sources means success
        assert monitor._detect_hardware() is True
        assert monitor._audio_source is not None  # Mock fallback
        assert monitor._video_source is not None  # Mock fallback


def test_start_stop_sequence(monitor, mock_components):
    # Setup for success
    monitor.config.fake_hardware = True

    monitor.start()

    assert monitor._running is True

    # Check components started
    mock_components["storage"].return_value.start.assert_called_once()
    mock_components["alert"].assert_called_once()
    mock_components["overlay_controller"].return_value.setup.assert_called_once()

    # Motion detector checks video device
    # In fake hardware mode, video device is set
    mock_components["motion"].assert_called_once()
    mock_components["motion"].return_value.start.assert_called_once()

    # Overlay generator (dynamic overlay defaults to True?)
    # Need to check config default or mock it
    # Assuming dynamic overlay enabled in mock config
    if monitor.config.alerts.enable_dynamic_overlay:
        mock_components["overlay_generator"].assert_called_once()
        mock_components["overlay_generator"].return_value.start.assert_called_once()

    mock_components["observer"].return_value.start.assert_called_once()
    mock_components["capture"].return_value.start.assert_called_once()

    # Stop
    monitor.stop()
    assert monitor._running is False

    mock_components["motion"].return_value.stop.assert_called_once()
    mock_components["capture"].return_value.stop.assert_called_once()
    mock_components["observer"].return_value.stop.assert_called_once()
    mock_components["storage"].return_value.stop.assert_called_once()


def test_run_loop(monitor, mock_components):
    # Test run loop briefly
    monitor.config.fake_hardware = True

    # Configure mock motion detector return
    mock_components["motion"].return_value.get_current_motion.return_value = 0.0
    mock_components["storage"].return_value.get_usage.return_value = (100, 1000)

    with patch("time.sleep") as mock_sleep:
        # Raise exception to break loop
        mock_sleep.side_effect = KeyboardInterrupt

        monitor.run()

        # Verify it started and stopped
        assert monitor._running is False  # Should be stopped by finally block
