"""Configuration management for bbwatch using Pydantic."""

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOGGER = logging.getLogger(__name__)


class AudioConfig(BaseModel):
    """Audio capture configuration."""

    segment_duration_s: float = Field(default=3.0, ge=1.0, le=10.0)
    overlap_s: float = Field(default=1.0, ge=0.0)
    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=2)
    device_index: str = Field(default="0")

    @field_validator("overlap_s")
    @classmethod
    def overlap_less_than_duration(cls, v: float, info: ValidationInfo) -> float:
        """Ensure overlap is less than segment duration."""
        duration = info.data.get("segment_duration_s", 3.0)
        if v >= duration:
            raise ValueError(f"overlap_s ({v}) must be less than segment_duration_s ({duration})")
        return v


class DetectionConfig(BaseModel):
    """Cry detection configuration."""

    bandpass_low_hz: float = Field(default=250.0, ge=50.0, le=1000.0)
    bandpass_high_hz: float = Field(default=800.0, ge=200.0, le=2000.0)
    rms_threshold: float = Field(default=0.02, ge=0.001, le=1.0)
    min_active_ratio: float = Field(default=0.3, ge=0.0, le=1.0)
    silence_threshold: float = Field(default=0.001, ge=0.0, le=0.1)

    @field_validator("bandpass_high_hz")
    @classmethod
    def high_greater_than_low(cls, v: float, info: ValidationInfo) -> float:
        """Ensure high frequency is greater than low."""
        low = info.data.get("bandpass_low_hz", 250.0)
        if v <= low:
            raise ValueError(f"bandpass_high_hz ({v}) must be greater than bandpass_low_hz ({low})")
        return v


class StorageConfig(BaseModel):
    """Disk storage configuration."""

    wav_dir: Path = Field(default=Path("wav_segments"))
    max_size_mb: float = Field(default=1024.0, ge=100.0, le=10000.0)
    delete_empty_segments: bool = Field(default=True)
    cleanup_interval_s: float = Field(default=60.0, ge=10.0)


class AlertConfig(BaseModel):
    """Alert configuration."""

    status_file: Path = Field(default=Path("status.json"))
    overlay_dir: Path = Field(default=Path("overlays"))

    # Hysteresis thresholds
    trigger_high: float = Field(default=0.03, ge=0.001, le=1.0)
    trigger_low: float = Field(default=0.015, ge=0.001, le=1.0)

    # Timing
    cooldown_s: float = Field(default=5.0, ge=0.0)

    # Actions
    record_clip_s: float = Field(default=10.0, ge=1.0)
    screenshot_on_peak: bool = Field(default=True)
    health_timeout_s: float = Field(default=10.0, ge=1.0)

    # Output directories
    clips_dir: Path = Field(default=Path("clips"))
    screenshots_dir: Path = Field(default=Path("screenshots"))

    @field_validator("trigger_low")
    @classmethod
    def low_less_than_high(cls, v: float, info: ValidationInfo) -> float:
        """Ensure trigger_low is less than trigger_high for hysteresis."""
        high = info.data.get("trigger_high", 0.03)
        if v >= high:
            raise ValueError(f"trigger_low ({v}) must be less than trigger_high ({high})")
        return v


class BBWatchConfig(BaseSettings):
    """Main configuration for bbwatch."""

    model_config = SettingsConfigDict(
        env_prefix="BBWATCH_",
        env_nested_delimiter="__",
    )

    # Sub-configurations
    audio: AudioConfig = Field(default_factory=AudioConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)

    # Runtime options
    log_level: str = Field(default="INFO")
    fake_hardware: bool = Field(default=False)
    data_dir: Path = Field(default=Path.home() / "bbwatch_data")

    @classmethod
    def from_yaml(cls, path: Path) -> "BBWatchConfig":
        """Load configuration from YAML file.

        Args:
            path: Path to YAML configuration file.

        Returns:
            Loaded configuration object.

        Raises:
            FileNotFoundError: If config file doesn't exist.
            ValueError: If config file is invalid.
        """
        if not path.exists():
            LOGGER.warning(f"Config file not found: {path}, using defaults")
            return cls()

        try:
            with open(path) as f:
                data = yaml.safe_load(f) or {}

            LOGGER.info(f"Loaded configuration from {path}")
            return cls(**data)

        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in config file: {e}") from e

    def resolve_paths(self, base_dir: Path | None = None) -> "BBWatchConfig":
        """Resolve relative paths to absolute paths.

        Args:
            base_dir: Base directory for relative paths. Defaults to data_dir.

        Returns:
            New config with resolved paths.
        """
        base = base_dir or self.data_dir
        base = base.expanduser().resolve()

        # Create a copy with resolved paths
        return self.model_copy(
            update={
                "data_dir": base,
                "storage": self.storage.model_copy(
                    update={
                        "wav_dir": base / self.storage.wav_dir
                        if not self.storage.wav_dir.is_absolute()
                        else self.storage.wav_dir,
                    }
                ),
                "alerts": self.alerts.model_copy(
                    update={
                        "status_file": base / self.alerts.status_file
                        if not self.alerts.status_file.is_absolute()
                        else self.alerts.status_file,
                        "overlay_dir": base / self.alerts.overlay_dir
                        if not self.alerts.overlay_dir.is_absolute()
                        else self.alerts.overlay_dir,
                    }
                ),
            }
        )

    def ensure_directories(self) -> None:
        """Create all required directories."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage.wav_dir.mkdir(parents=True, exist_ok=True)
        self.alerts.overlay_dir.mkdir(parents=True, exist_ok=True)
        self.alerts.clips_dir.mkdir(parents=True, exist_ok=True)
        self.alerts.screenshots_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.debug(f"Ensured directories exist under {self.data_dir}")


def get_default_config_path() -> Path:
    """Get the default configuration file path."""
    # Check in order: current dir, home dir, /etc
    candidates = [
        Path.cwd() / "config.yaml",
        Path.home() / ".config" / "bbwatch" / "config.yaml",
        Path("/etc/bbwatch/config.yaml"),
    ]

    for path in candidates:
        if path.exists():
            return path

    # Return first candidate as default (will use defaults if not found)
    return candidates[0]
