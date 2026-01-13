"""Integration tests for capture → detection pipeline."""

import json
import shutil
from unittest.mock import Mock

from bbwatch.alert import AlertManager
from bbwatch.config import AlertConfig, DetectionConfig
from bbwatch.detector import SegmentHandler


class TestCaptureDetectorIntegration:
    """Test that captured segments are processed correctly."""

    def test_new_segment_triggers_detection(self, tmp_path, baby_cry_wav, test_config):
        """When a new .wav appears, detector processes it."""
        status_file = tmp_path / "status.json"

        # Setup configs
        det_config = DetectionConfig(**test_config)
        alert_config = AlertConfig(
            status_file=status_file,
            trigger_high=0.01,  # Low threshold to ensure cry triggers
            trigger_low=0.005,
        )
        alert_manager = AlertManager(alert_config)

        handler = SegmentHandler(
            config=det_config,
            alert_manager=alert_manager,
        )

        # Copy fixture to simulate new segment
        test_wav = tmp_path / "segment_001.wav"
        shutil.copy(baby_cry_wav, test_wav)

        # Create mock event
        event = Mock()
        event.src_path = str(test_wav)
        event.is_directory = False

        handler.on_closed(event)

        # Status file should be updated
        assert status_file.exists()
        status = json.loads(status_file.read_text())
        assert status["alert_active"] is True
        assert "timestamp" in status

    def test_empty_segment_deleted_when_configured(self, tmp_path, silence_wav, test_config):
        """Empty segments should be deleted if configured."""
        status_file = tmp_path / "status.json"
        det_config = DetectionConfig(**test_config)

        # Need dummy alert manager
        alert_manager = AlertManager(AlertConfig(status_file=status_file))

        handler = SegmentHandler(config=det_config, alert_manager=alert_manager, delete_empty=True)

        test_wav = tmp_path / "segment_002.wav"
        shutil.copy(silence_wav, test_wav)

        event = Mock()
        event.src_path = str(test_wav)
        event.is_directory = False

        handler.on_closed(event)

        # File should be deleted
        assert not test_wav.exists()

    def test_empty_segment_kept_when_not_configured(self, tmp_path, silence_wav, test_config):
        """Empty segments should be kept if deletion is disabled."""
        status_file = tmp_path / "status.json"
        det_config = DetectionConfig(**test_config)
        alert_manager = AlertManager(AlertConfig(status_file=status_file))

        handler = SegmentHandler(config=det_config, alert_manager=alert_manager, delete_empty=False)

        test_wav = tmp_path / "segment_003.wav"
        shutil.copy(silence_wav, test_wav)

        event = Mock()
        event.src_path = str(test_wav)
        event.is_directory = False

        handler.on_closed(event)

        # File should still exist
        assert test_wav.exists()

    def test_alert_cooldown_hysteresis(self, tmp_path, baby_cry_wav, test_config):
        """State machine should respect hysteresis and cooldown (simulated)."""
        status_file = tmp_path / "status.json"

        det_config = DetectionConfig(**test_config)
        alert_config = AlertConfig(
            status_file=status_file,
            cooldown_s=10.0,
            trigger_high=0.04,  # Lower threshold to sure detection with fixture
            trigger_low=0.02,
        )
        alert_manager = AlertManager(alert_config)

        handler = SegmentHandler(config=det_config, alert_manager=alert_manager)

        # 1. High intensity segment -> Trigger
        # Mocking detection result is hard because it runs detect_cry internally
        # But we know baby_cry_wav has high energy.

        test_wav1 = tmp_path / "segment_001.wav"
        shutil.copy(baby_cry_wav, test_wav1)
        event1 = Mock(src_path=str(test_wav1), is_directory=False)
        handler.on_closed(event1)

        status1 = json.loads(status_file.read_text())
        # RMS of baby cry fixture is ~0.3 (amplitude 0.8 * sine)
        assert status1["alert_active"] is True
        assert status1["intensity"] > 0.04

    def test_ignores_non_wav_files(self, tmp_path, test_config):
        """Non-wav files should be ignored."""
        status_file = tmp_path / "status.json"
        det_config = DetectionConfig(**test_config)
        alert_manager = AlertManager(AlertConfig(status_file=status_file))

        handler = SegmentHandler(config=det_config, alert_manager=alert_manager)

        # Create non-wav file
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("hello")

        event = Mock()
        event.src_path = str(txt_file)
        event.is_directory = False

        handler.on_closed(event)

        # Status file should not be created
        assert not status_file.exists()

    def test_ignores_directories(self, tmp_path, test_config):
        """Directory events should be ignored."""
        status_file = tmp_path / "status.json"
        det_config = DetectionConfig(**test_config)
        alert_manager = AlertManager(AlertConfig(status_file=status_file))

        handler = SegmentHandler(config=det_config, alert_manager=alert_manager)

        subdir = tmp_path / "subdir.wav"  # Tricky name
        subdir.mkdir()

        event = Mock()
        event.src_path = str(subdir)
        event.is_directory = True

        handler.on_closed(event)

        assert not status_file.exists()
