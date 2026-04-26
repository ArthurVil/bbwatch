"""Audio and video source abstractions.

Provides protocol definitions for pluggable audio/video inputs (ALSA, RTSP, V4L2, rpicam)
and concrete implementations for each source type.
"""

import logging
import subprocess
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)


class AudioSource(Protocol):
    """Protocol for audio input sources (ALSA device, RTSP stream, or mock)."""

    def open(self) -> Any:
        """Open the audio stream for reading."""
        ...

    def close(self) -> None:
        """Close the audio stream cleanly."""
        ...

    def is_available(self) -> bool:
        """Non-blocking check: is this source usable right now?"""
        ...

    def __repr__(self) -> str:
        """User-friendly identifier: 'ALSA(hw:1,0)' or 'RTSP(babycam)' etc."""
        ...


class VideoSource(Protocol):
    """Protocol for video input sources (V4L2, rpicam, RTSP, or mock)."""

    def open(self) -> Any:
        """Open the video stream for reading."""
        ...

    def close(self) -> None:
        """Close the video stream cleanly."""
        ...

    def is_available(self) -> bool:
        """Non-blocking check: is this source usable right now?"""
        ...

    def __repr__(self) -> str:
        """User-friendly identifier: 'V4L2(/dev/video0)', 'RTSP(raw_video)', etc."""
        ...


# ============================================================================
# Audio Source Implementations
# ============================================================================


class ALSASource:
    """ALSA audio input from local sound card."""

    def __init__(self, device_id: str):
        """Initialize ALSA source.

        Args:
            device_id: ALSA device string, e.g., 'hw:0,0'
        """
        self.device_id = device_id

    def open(self) -> Any:
        """Return FFmpeg command for ALSA input."""
        return self.device_id

    def close(self) -> None:
        """No cleanup needed for ALSA sources."""
        pass

    def is_available(self) -> bool:
        """Fast check: can we list ALSA devices?"""
        try:
            subprocess.run(
                ["arecord", "-l"],
                timeout=2,
                capture_output=True,
                check=True,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return False

    def __repr__(self) -> str:
        return f"ALSA({self.device_id})"


class RTSPAudioSource:
    """Audio input from RTSP stream (remote or local go2rtc)."""

    def __init__(self, url: str):
        """Initialize RTSP audio source.

        Args:
            url: Full RTSP URL, e.g., 'rtsp://localhost:8554/babycam'
        """
        self.url = url

    def open(self) -> str:
        """Return the RTSP URL for FFmpeg."""
        return self.url

    def close(self) -> None:
        """No cleanup needed for RTSP sources."""
        pass

    def is_available(self) -> bool:
        """Fast check: URL is well-formed."""
        return self.url.startswith("rtsp://") and len(self.url) > 10

    def __repr__(self) -> str:
        # Extract stream name from URL for cleaner display
        stream_name = self.url.split("/")[-1]
        return f"RTSP({stream_name})"


class MockAudioSource:
    """Mock audio source for testing without hardware."""

    def __init__(self, name: str = "Audio"):
        self.name = name

    def open(self) -> Any:
        return None

    def close(self) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"Mock({self.name})"


# ============================================================================
# Video Source Implementations
# ============================================================================


class V4L2Source:
    """V4L2 video input from USB camera or local device."""

    def __init__(self, device_path: str):
        """Initialize V4L2 source.

        Args:
            device_path: Device path, e.g., '/dev/video0'
        """
        self.device_path = device_path

    def open(self) -> str:
        """Return the device path for OpenCV."""
        return self.device_path

    def close(self) -> None:
        """No cleanup needed for V4L2 sources."""
        pass

    def is_available(self) -> bool:
        """Check if device file exists."""
        from pathlib import Path
        return Path(self.device_path).exists()

    def __repr__(self) -> str:
        return f"V4L2({self.device_path})"


class RPiCameraSource:
    """Raspberry Pi Camera accessed via go2rtc RTSP restream.

    Note: The raw rpicam:N device is held by go2rtc (camera hardware lock).
    Motion detection accesses the camera via RTSP restream to avoid contention.
    """

    def __init__(self, rpicam_path: str, rtsp_url: str = "rtsp://localhost:8554/raw_video"):
        """Initialize Pi Camera source.

        Args:
            rpicam_path: Device path like 'rpicam:0'
            rtsp_url: RTSP restream URL from go2rtc (default: local go2rtc)
        """
        self.rpicam_path = rpicam_path
        self.rtsp_url = rtsp_url

    def open(self) -> str:
        """Return RTSP URL for OpenCV (motion detection uses restream, not direct device)."""
        return self.rtsp_url

    def close(self) -> None:
        """No cleanup needed for RTSP sources."""
        pass

    def is_available(self) -> bool:
        """Check if the rpicam path is valid and RTSP URL is reachable."""
        # Quick format check—full connectivity tested at runtime
        return self.rpicam_path.startswith("rpicam:") and self.rtsp_url.startswith("rtsp://")

    def __repr__(self) -> str:
        return f"RPiCamera({self.rpicam_path})"


class RTSPVideoSource:
    """Video input from RTSP stream (remote or local go2rtc)."""

    def __init__(self, url: str):
        """Initialize RTSP video source.

        Args:
            url: Full RTSP URL, e.g., 'rtsp://localhost:8554/raw_video'
        """
        self.url = url

    def open(self) -> str:
        """Return the RTSP URL for OpenCV."""
        return self.url

    def close(self) -> None:
        """No cleanup needed for RTSP sources."""
        pass

    def is_available(self) -> bool:
        """Fast check: URL is well-formed."""
        return self.url.startswith("rtsp://") and len(self.url) > 10

    def __repr__(self) -> str:
        stream_name = self.url.split("/")[-1]
        return f"RTSP({stream_name})"


class MockVideoSource:
    """Mock video source for testing without hardware."""

    def __init__(self, name: str = "Video"):
        self.name = name

    def open(self) -> Any:
        return None

    def close(self) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"Mock({self.name})"
