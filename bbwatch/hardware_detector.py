"""Hardware device detection service.

Discovers available audio (ALSA) and video (V4L2, Pi Camera) devices on the system.
"""

import logging
import subprocess

from bbwatch.hardware import _AUDIO_DEVICE_PATTERN, _PICAMERA_PATTERN, AudioDevice, VideoDevice

LOGGER = logging.getLogger(__name__)

__all__ = ["AudioDevice", "VideoDevice", "HardwareDetector"]


class HardwareDetector:
    """Discover available audio and video devices on this system."""

    def __init__(self, timeout_sec: int = 10, fake: bool = False):
        """Initialize detector.

        Args:
            timeout_sec: Subprocess timeout for device detection (seconds).
            fake: If True, return fake devices for testing without hardware.
        """
        self.timeout = timeout_sec
        self.fake = fake

    def detect_audio_devices(self) -> list[AudioDevice]:
        """Detect ALSA audio capture devices.

        Returns:
            List of detected AudioDevice. Empty if arecord not found or times out.
        """
        if self.fake:
            return [AudioDevice(card=0, device=0, name="Fake Audio")]

        try:
            result = subprocess.run(
                ["arecord", "-l"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            devices = self._parse_arecord(result.stdout)
            LOGGER.debug(f"Detected {len(devices)} ALSA audio device(s)")
            return devices
        except FileNotFoundError:
            LOGGER.debug("arecord not found—skipping ALSA audio detection")
            return []
        except subprocess.TimeoutExpired:
            LOGGER.debug("arecord timeout—skipping ALSA audio detection")
            return []
        except subprocess.CalledProcessError as e:
            LOGGER.debug(f"arecord failed: {e}—skipping ALSA audio detection")
            return []

    def detect_video_devices(self) -> list[VideoDevice]:
        """Detect V4L2 USB video devices.

        Returns:
            List of detected VideoDevice. Empty if v4l2-ctl not found.
        """
        if self.fake:
            return [VideoDevice(path="/dev/video0", name="Fake USB Camera")]

        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            devices = self._parse_v4l2ctl(result.stdout)
            LOGGER.debug(f"Detected {len(devices)} V4L2 video device(s)")
            return devices
        except FileNotFoundError:
            LOGGER.debug("v4l2-ctl not found—skipping V4L2 video detection")
            return []
        except subprocess.TimeoutExpired:
            LOGGER.debug("v4l2-ctl timeout—skipping V4L2 video detection")
            return []
        except subprocess.CalledProcessError as e:
            LOGGER.debug(f"v4l2-ctl failed: {e}—skipping V4L2 video detection")
            return []

    def detect_picamera_devices(self) -> list[VideoDevice]:
        """Detect Raspberry Pi Camera modules via libcamera.

        Returns:
            List of detected VideoDevice with rpicam:N paths.
            Empty if libcamera not available (not on Raspberry Pi).
        """
        if self.fake:
            return [VideoDevice(path="rpicam:0", name="Fake Pi Camera")]

        try:
            result = subprocess.run(
                ["libcamera-hello", "--list-cameras"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            devices = self._parse_libcamera(result.stdout)
            LOGGER.debug(f"Detected {len(devices)} Pi Camera device(s)")
            return devices
        except FileNotFoundError:
            LOGGER.debug("libcamera not available—not on Raspberry Pi")
            return []
        except subprocess.TimeoutExpired:
            LOGGER.warning("libcamera-hello timed out—camera may be locked by another process")
            return []
        except subprocess.CalledProcessError as e:
            LOGGER.warning(f"libcamera-hello failed: {e}—check ribbon cable and dmesg")
            return []

    @staticmethod
    def _parse_arecord(output: str) -> list[AudioDevice]:
        """Parse arecord -l output into AudioDevice list."""
        devices = []
        for line in output.splitlines():
            if match := _AUDIO_DEVICE_PATTERN.search(line):
                devices.append(
                    AudioDevice(
                        card=int(match.group(1)),
                        device=int(match.group(3)),
                        name=match.group(2),
                    )
                )
        return devices

    @staticmethod
    def _parse_v4l2ctl(output: str) -> list[VideoDevice]:
        """Parse v4l2-ctl --list-devices output into VideoDevice list."""
        devices = []
        current_name = "Unknown"

        for line in output.splitlines():
            line_stripped = line.rstrip()

            if not line_stripped:
                continue

            # Device name lines: end with ":" and not indented
            if line_stripped.endswith(":") and not line_stripped.startswith("\t"):
                current_name = line_stripped.rstrip(":").strip()
                # Remove driver info in parentheses if present
                if "(" in current_name:
                    current_name = current_name.split("(")[0].strip()

            # Device path lines: indented and contain /dev/video
            elif "/dev/video" in line_stripped:
                devices.append(VideoDevice(path=line_stripped.strip(), name=current_name))

        return devices

    @staticmethod
    def _parse_libcamera(output: str) -> list[VideoDevice]:
        """Parse libcamera-hello --list-cameras output into VideoDevice list."""
        devices = []
        for line in output.splitlines():
            if match := _PICAMERA_PATTERN.search(line):
                index = match.group(1)
                name = match.group(2).strip()
                devices.append(
                    VideoDevice(
                        path=f"rpicam:{index}",
                        name=f"Pi Camera {index} ({name})",
                    )
                )
        return devices
