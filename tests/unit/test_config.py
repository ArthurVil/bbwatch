"""Unit tests for configuration management."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from bbwatch.config import (
    AudioConfig,
    BBWatchConfig,
    DetectionConfig,
    StorageConfig,
)


class TestAudioConfig:
    """Tests for AudioConfig validation."""

    def test_default_values(self):
        """Default values should be valid."""
        config = AudioConfig()
        assert config.segment_duration_s == 3.0
        assert config.overlap_s == 1.0
        assert config.sample_rate == 16000
        assert config.channels == 1

    def test_overlap_validation(self):
        """Overlap must be less than segment duration."""
        with pytest.raises(ValueError):
            AudioConfig(segment_duration_s=3.0, overlap_s=3.0)

        with pytest.raises(ValueError):
            AudioConfig(segment_duration_s=3.0, overlap_s=4.0)

    def test_range_validation(self):
        """Values must be within valid ranges."""
        with pytest.raises(ValueError):
            AudioConfig(segment_duration_s=0.05)  # Too short

        with pytest.raises(ValueError):
            AudioConfig(sample_rate=1000)  # Too low


class TestDetectionConfig:
    """Tests for DetectionConfig validation."""

    def test_default_values(self):
        """Default values should be valid."""
        config = DetectionConfig()
        assert config.bandpass_low_hz == 250.0
        assert config.bandpass_high_hz == 800.0
        assert config.rms_threshold == 0.02

    def test_frequency_order(self):
        """High frequency must be greater than low."""
        with pytest.raises(ValueError):
            DetectionConfig(bandpass_low_hz=800.0, bandpass_high_hz=250.0)

    def test_threshold_range(self):
        """Thresholds must be within valid ranges."""
        with pytest.raises(ValueError):
            DetectionConfig(rms_threshold=2.0)  # > 1.0

        with pytest.raises(ValueError):
            DetectionConfig(min_active_ratio=-0.5)  # < 0.0


class TestStorageConfig:
    """Tests for StorageConfig."""

    def test_default_values(self):
        """Default values should be valid."""
        config = StorageConfig()
        assert config.max_size_mb == 1024.0
        assert config.delete_empty_segments is True

    def test_path_default(self):
        """Default path should be relative."""
        config = StorageConfig()
        assert not config.wav_dir.is_absolute()


class TestBBWatchConfig:
    """Tests for main BBWatchConfig."""

    def test_default_config(self, monkeypatch):
        """Default config should be valid."""
        monkeypatch.delenv("BBWATCH_LOG_LEVEL", raising=False)
        monkeypatch.delenv("BBWATCH_FAKE_HARDWARE", raising=False)
        config = BBWatchConfig()
        assert config.log_level == "INFO"
        assert config.fake_hardware is False

    def test_from_yaml_missing_file(self, tmp_path, monkeypatch):
        """Missing file should return defaults."""
        monkeypatch.delenv("BBWATCH_LOG_LEVEL", raising=False)
        config = BBWatchConfig.from_yaml(tmp_path / "missing.yaml")
        assert config.log_level == "INFO"

    def test_from_yaml_valid_file(self, tmp_path):
        """Valid YAML should be loaded."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
log_level: DEBUG
detection:
  rms_threshold: 0.05
"""
        )
        config = BBWatchConfig.from_yaml(config_file)
        assert config.log_level == "DEBUG"
        assert config.detection.rms_threshold == 0.05

    def test_from_yaml_unknown_key_rejected(self, tmp_path):
        """An unknown key in a config section must fail loudly, not be ignored.

        Regression: config.yaml shipped `alert_cooldown_s` (real field:
        `cooldown_s`); Pydantic silently dropped it and the operator's value
        never applied.
        """
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
alerts:
  alert_cooldown_s: 1.0
"""
        )
        with pytest.raises(ValidationError):
            BBWatchConfig.from_yaml(config_file)

    def test_from_yaml_cooldown_applies(self, tmp_path):
        """The correctly-named cooldown_s key must reach AlertConfig."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
alerts:
  cooldown_s: 1.0
"""
        )
        config = BBWatchConfig.from_yaml(config_file)
        assert config.alerts.cooldown_s == 1.0

    def test_overlay_drop_stale_frames_defaults_true(self):
        """Default overlay frame policy is drop-stale (bounded latency)."""
        assert BBWatchConfig().alerts.overlay_drop_stale_frames is True

    def test_overlay_drop_stale_frames_from_yaml(self, tmp_path):
        """The no-loss opt-out must reach AlertConfig via YAML."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            """
alerts:
  overlay_drop_stale_frames: false
"""
        )
        config = BBWatchConfig.from_yaml(config_file)
        assert config.alerts.overlay_drop_stale_frames is False

    def test_shipped_config_yaml_is_valid(self):
        """The config.yaml shipped in the repo must load without errors."""
        repo_config = Path(__file__).parents[2] / "config.yaml"
        config = BBWatchConfig.from_yaml(repo_config)
        assert config.alerts.cooldown_s == 1.0
        assert config.alerts.overlay_drop_stale_frames is True

    def test_from_yaml_invalid_yaml(self, tmp_path):
        """Invalid YAML should raise ValueError."""
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("{ invalid: yaml: [")

        with pytest.raises(ValueError):
            BBWatchConfig.from_yaml(config_file)

    def test_resolve_paths(self, tmp_path):
        """Paths should be resolved relative to data_dir."""
        config = BBWatchConfig(data_dir=tmp_path)
        resolved = config.resolve_paths()

        assert resolved.storage.wav_dir.is_absolute()
        assert str(tmp_path) in str(resolved.storage.wav_dir)

    def test_ensure_directories(self, tmp_path):
        """ensure_directories should create required dirs."""
        config = BBWatchConfig(data_dir=tmp_path / "new_dir")
        config = config.resolve_paths()
        config.ensure_directories()

        assert config.data_dir.exists()
        assert config.storage.wav_dir.exists()
        assert config.alerts.overlay_dir.exists()

    def test_env_override(self, tmp_path, monkeypatch):
        """Environment variables should override config."""
        monkeypatch.setenv("BBWATCH_LOG_LEVEL", "ERROR")
        monkeypatch.setenv("BBWATCH_FAKE_HARDWARE", "true")

        config = BBWatchConfig()
        assert config.log_level == "ERROR"
        assert config.fake_hardware is True
