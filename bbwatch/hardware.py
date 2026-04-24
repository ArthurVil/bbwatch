"""Hardware detection and logging for bbwatch.

Detects available audio and video capture devices on the system
and logs them at startup for user verification.
"""

import logging
import re
import subprocess
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

# Compiled regexes for device detection
_AUDIO_DEVICE_PATTERN = re.compile(r"card (\d+):.*\[(.+?)\].*device (\d+):")
_PICAMERA_PATTERN = re.compile(r"(\d+)\s*:\s*(.+?)\s*\[")


@dataclass
class AudioDevice:
    """Represents an ALSA audio capture device."""

    card: int
    device: int
    name: str

    @property
    def alsa_id(self) -> str:
        """Get the ALSA device identifier (e.g., 'hw:1,0')."""
        return f"hw:{self.card},{self.device}"

    def __str__(self) -> str:
        return f"[{self.alsa_id}] {self.name}"


@dataclass
class VideoDevice:
    """Represents a V4L2 video capture device."""

    path: str
    name: str

    def __str__(self) -> str:
        return f"[{self.path}] {self.name}"


def detect_audio_devices() -> list[AudioDevice]:
    """Detect all available ALSA audio capture devices.

    Returns:
        List of detected audio devices. Empty list if none found or on error.
    """
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        LOGGER.warning(f"Audio device detection failed: {e}")
        return []

    devices = []

    for line in result.stdout.splitlines():
        match = _AUDIO_DEVICE_PATTERN.search(line)
        if match:
            card = int(match.group(1))
            name = match.group(2)
            device = int(match.group(3))
            devices.append(AudioDevice(card=card, device=device, name=name))

    return devices


def detect_video_devices() -> list[VideoDevice]:
    """Detect all available V4L2 video devices.

    Returns:
        List of detected video devices. Empty list if none found or on error.
    """
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        LOGGER.warning(f"Video device detection failed: {e}")
        return []

    devices = []
    current_name = "Unknown"

    for line in result.stdout.splitlines():
        line = line.rstrip()

        if not line:
            continue

        # Device name lines end with ":"
        if line.endswith(":") and not line.startswith("\t"):
            # Remove trailing colon and clean up
            current_name = line.rstrip(":").strip()
            # Remove USB path info in parentheses if present
            if "(" in current_name:
                current_name = current_name.split("(")[0].strip()

        # Device path lines are indented and start with /dev/
        elif "/dev/video" in line:
            path = line.strip()
            devices.append(VideoDevice(path=path, name=current_name))

    return devices


def detect_picamera_devices() -> list[VideoDevice]:
    """Detect available Raspberry Pi Camera modules via libcamera.

    Returns:
        List of detected Pi Camera devices. Empty list if none found or on error.
    """
    try:
        result = subprocess.run(
            ["libcamera-hello", "--list-cameras"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
        LOGGER.debug(f"Pi Camera detection skipped (libcamera not available): {e}")
        return []

    devices = []

    for line in result.stdout.splitlines():
        match = _PICAMERA_PATTERN.search(line)
        if match:
            camera_index = match.group(1)
            camera_name = match.group(2).strip()
            # Create a VideoDevice with rpicam:<index> path
            devices.append(VideoDevice(path=f"rpicam:{camera_index}", name=f"Pi Camera {camera_index} ({camera_name})"))

    return devices


def log_detected_hardware() -> tuple[list[AudioDevice], list[VideoDevice]]:
    """Detect and log all available hardware at startup.

    Returns:
        Tuple of (audio_devices, video_devices).
    """
    audio_devices = detect_audio_devices()
    video_devices = detect_video_devices()
    picamera_devices = detect_picamera_devices()

    LOGGER.info("=" * 50)
    LOGGER.info("DETECTED HARDWARE")
    LOGGER.info("=" * 50)

    LOGGER.info("Audio Capture Devices:")
    if audio_devices:
        for dev in audio_devices:
            LOGGER.info(f"  {dev}")
    else:
        LOGGER.warning("  No audio capture devices found!")

    LOGGER.info("Video Capture Devices (V4L2):")
    if video_devices:
        for vdev in video_devices:
            LOGGER.info(f"  {vdev}")
    else:
        LOGGER.info("  No V4L2 video devices found")

    LOGGER.info("Raspberry Pi Camera Modules:")
    if picamera_devices:
        for pdev in picamera_devices:
            LOGGER.info(f"  {pdev}")
    else:
        LOGGER.debug("  No Pi Camera modules detected")

    LOGGER.info("=" * 50)

    # Combine video devices (rpicam + v4l2)
    all_video_devices = picamera_devices + video_devices
    return audio_devices, all_video_devices


def get_preferred_audio_device(devices: list[AudioDevice]) -> AudioDevice | None:
    """Get the preferred audio device for capture.

    Prefers USB audio devices over built-in.

    Args:
        devices: List of detected audio devices.

    Returns:
        Preferred audio device, or None if no devices available.
    """
    if not devices:
        return None

    # Prefer USB devices
    usb_devices = [d for d in devices if "USB" in d.name.upper()]
    if usb_devices:
        return usb_devices[0]

    # Fall back to first available
    return devices[0]


def get_preferred_video_device(devices: list[VideoDevice]) -> VideoDevice | None:
    """Get the preferred video device for capture.

    Prefers rpicam (native Pi Camera) if available, then /dev/video0, else first device.

    Args:
        devices: List of detected video devices.

    Returns:
        Preferred video device, or None if no devices available.
    """
    if not devices:
        return None

    # Prefer rpicam (native Pi Camera over V4L2)
    for dev in devices:
        if dev.path.startswith("rpicam:"):
            return dev

    # Prefer /dev/video0 (V4L2)
    for dev in devices:
        if dev.path == "/dev/video0":
            return dev

    # Fall back to first available
    return devices[0]
