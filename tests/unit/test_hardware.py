"""Unit tests for hardware device detection."""

import subprocess
from unittest.mock import MagicMock, patch

from bbwatch.hardware import (
    AudioDevice,
    VideoDevice,
    detect_audio_devices,
    detect_picamera_devices,
    detect_video_devices,
    get_preferred_audio_device,
    get_preferred_video_device,
    log_detected_hardware,
)


class TestDetectAudioDevices:
    """Tests for detect_audio_devices()."""

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_audio_returns_parsed_devices(self, mock_run):
        """Parse arecord output and return AudioDevice list."""
        mock_run.return_value = MagicMock(
            stdout=(
                "arecord: device_list.c:268: (snd_device_name_hint) Cannot connect to server\n"
                "card 0: PCH [HDA Intel PCH], device 0: ALC892 Analog\n"
                "card 1: Device [USB PnP Sound Device], device 0: USB Audio\n"
            ),
            returncode=0,
        )

        result = detect_audio_devices()

        assert len(result) == 2
        assert result[0] == AudioDevice(card=0, device=0, name="HDA Intel PCH")
        assert result[1] == AudioDevice(card=1, device=0, name="USB PnP Sound Device")

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_audio_missing_tool(self, mock_run):
        """Return empty list if arecord not found."""
        mock_run.side_effect = FileNotFoundError()

        result = detect_audio_devices()

        assert result == []

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_audio_timeout(self, mock_run):
        """Return empty list on subprocess timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired("arecord", 5)

        result = detect_audio_devices()

        assert result == []

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_audio_command_error(self, mock_run):
        """Return empty list on subprocess error."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "arecord")

        result = detect_audio_devices()

        assert result == []

    def test_audio_alsa_id_property(self):
        """AudioDevice.alsa_id formats as hw:card,device."""
        device = AudioDevice(card=1, device=2, name="Test Mic")
        assert device.alsa_id == "hw:1,2"

    def test_audio_device_str(self):
        """AudioDevice string representation includes ALSA ID and name."""
        device = AudioDevice(card=0, device=0, name="USB Mic")
        assert str(device) == "[hw:0,0] USB Mic"


class TestDetectVideoDevices:
    """Tests for detect_video_devices()."""

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_video_returns_parsed_devices(self, mock_run):
        """Parse v4l2-ctl output and return VideoDevice list."""
        mock_run.return_value = MagicMock(
            stdout=(
                "USB Camera:\n"
                "\t/dev/video0\n"
                "\t/dev/video1\n"
                "HD Video Capture:\n"
                "\t/dev/video2\n"
            ),
            returncode=0,
        )

        result = detect_video_devices()

        assert len(result) == 3
        assert result[0] == VideoDevice(path="/dev/video0", name="USB Camera")
        assert result[1] == VideoDevice(path="/dev/video1", name="USB Camera")
        assert result[2] == VideoDevice(path="/dev/video2", name="HD Video Capture")

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_video_name_strips_driver(self, mock_run):
        """Strip driver info from device name (text in parentheses)."""
        mock_run.return_value = MagicMock(
            stdout=(
                "USB Camera (usb-046d_0809-1101):\n"
                "\t/dev/video0\n"
            ),
            returncode=0,
        )

        result = detect_video_devices()

        assert len(result) == 1
        assert result[0].name == "USB Camera"

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_video_missing_tool(self, mock_run):
        """Return empty list if v4l2-ctl not found."""
        mock_run.side_effect = FileNotFoundError()

        result = detect_video_devices()

        assert result == []

    def test_video_device_str(self):
        """VideoDevice string representation includes path and name."""
        device = VideoDevice(path="/dev/video0", name="USB Camera")
        assert str(device) == "[/dev/video0] USB Camera"


class TestDetectPicameraDevices:
    """Tests for detect_picamera_devices()."""

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_picamera_returns_parsed_devices(self, mock_run):
        """Parse libcamera-hello output and return VideoDevice list with rpicam:N path."""
        mock_run.return_value = MagicMock(
            stdout="0 : imx708 [4608x2592 10-bit RGGB]\n",
            returncode=0,
        )

        result = detect_picamera_devices()

        assert len(result) == 1
        assert result[0] == VideoDevice(path="rpicam:0", name="Pi Camera 0 (imx708)")

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_picamera_multiple_cameras(self, mock_run):
        """Parse multiple camera lines and return list with correct indices."""
        mock_run.return_value = MagicMock(
            stdout=(
                "0 : imx708 [4608x2592 10-bit RGGB]\n"
                "1 : ov5647 [2592x1944 10-bit BAYER]\n"
            ),
            returncode=0,
        )

        result = detect_picamera_devices()

        assert len(result) == 2
        assert result[0].path == "rpicam:0"
        assert result[1].path == "rpicam:1"

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_picamera_missing_libcamera(self, mock_run):
        """Return empty list if libcamera-hello not found (non-RPi host)."""
        mock_run.side_effect = FileNotFoundError()

        result = detect_picamera_devices()

        assert result == []

    @patch("bbwatch.hardware.subprocess.run")
    def test_detect_picamera_timeout(self, mock_run):
        """Return empty list on libcamera timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired("libcamera-hello", 5)

        result = detect_picamera_devices()

        assert result == []


class TestGetPreferredVideoDevice:
    """Tests for get_preferred_video_device()."""

    def test_prefers_rpicam_over_v4l2(self):
        """Prefer rpicam:N device over /dev/video0."""
        devices = [
            VideoDevice(path="/dev/video0", name="USB Camera"),
            VideoDevice(path="rpicam:0", name="Pi Camera 0 (imx708)"),
        ]

        result = get_preferred_video_device(devices)

        assert result.path == "rpicam:0"

    def test_prefers_dev_video0_as_fallback(self):
        """Prefer /dev/video0 when no rpicam available."""
        devices = [
            VideoDevice(path="/dev/video1", name="Second Camera"),
            VideoDevice(path="/dev/video0", name="USB Camera"),
        ]

        result = get_preferred_video_device(devices)

        assert result.path == "/dev/video0"

    def test_returns_first_when_no_preferred(self):
        """Return first device when no rpicam or /dev/video0."""
        devices = [VideoDevice(path="/dev/video2", name="Third Camera")]

        result = get_preferred_video_device(devices)

        assert result.path == "/dev/video2"

    def test_returns_none_when_empty(self):
        """Return None when device list is empty."""
        result = get_preferred_video_device([])

        assert result is None


class TestGetPreferredAudioDevice:
    """Tests for get_preferred_audio_device()."""

    def test_prefers_usb_audio(self):
        """Prefer device with USB in name (case-insensitive)."""
        devices = [
            AudioDevice(card=0, device=0, name="HDA Intel PCH"),
            AudioDevice(card=1, device=0, name="USB PnP Sound Device"),
        ]

        result = get_preferred_audio_device(devices)

        assert result.card == 1

    def test_prefers_usb_case_insensitive(self):
        """USB preference check is case-insensitive."""
        devices = [
            AudioDevice(card=0, device=0, name="Generic Audio"),
            AudioDevice(card=1, device=0, name="usb microphone"),
        ]

        result = get_preferred_audio_device(devices)

        assert result.card == 1

    def test_returns_first_as_fallback(self):
        """Return first device when no USB name found."""
        devices = [
            AudioDevice(card=0, device=0, name="Built-in Audio"),
            AudioDevice(card=1, device=0, name="Analog Output"),
        ]

        result = get_preferred_audio_device(devices)

        assert result.card == 0

    def test_returns_none_when_empty(self):
        """Return None when device list is empty."""
        result = get_preferred_audio_device([])

        assert result is None


class TestLogDetectedHardware:
    """Tests for log_detected_hardware()."""

    @patch("bbwatch.hardware.detect_picamera_devices")
    @patch("bbwatch.hardware.detect_video_devices")
    @patch("bbwatch.hardware.detect_audio_devices")
    def test_log_returns_picam_before_v4l2(self, mock_audio, mock_video, mock_picam):
        """Return tuple with (audio, picam + v4l2 list) — Pi Cameras first."""
        audio_device = AudioDevice(card=0, device=0, name="USB Mic")
        v4l2_device = VideoDevice(path="/dev/video0", name="USB Camera")
        picam_device = VideoDevice(path="rpicam:0", name="Pi Camera 0 (imx708)")

        mock_audio.return_value = [audio_device]
        mock_picam.return_value = [picam_device]
        mock_video.return_value = [v4l2_device]

        audio_result, video_result = log_detected_hardware()

        assert audio_result == [audio_device]
        # picam should come first in video list
        assert video_result[0] == picam_device
        assert video_result[1] == v4l2_device

    @patch("bbwatch.hardware.detect_picamera_devices")
    @patch("bbwatch.hardware.detect_video_devices")
    @patch("bbwatch.hardware.detect_audio_devices")
    def test_log_no_devices_found(self, mock_audio, mock_video, mock_picam):
        """Return empty lists when no devices found."""
        mock_audio.return_value = []
        mock_picam.return_value = []
        mock_video.return_value = []

        audio_result, video_result = log_detected_hardware()

        assert audio_result == []
        assert video_result == []

    @patch("bbwatch.hardware.detect_picamera_devices")
    @patch("bbwatch.hardware.detect_video_devices")
    @patch("bbwatch.hardware.detect_audio_devices")
    def test_log_only_picam(self, mock_audio, mock_video, mock_picam):
        """Return only picam when no V4L2 devices available."""
        picam_device = VideoDevice(path="rpicam:0", name="Pi Camera 0 (imx708)")

        mock_audio.return_value = []
        mock_picam.return_value = [picam_device]
        mock_video.return_value = []

        audio_result, video_result = log_detected_hardware()

        assert audio_result == []
        assert video_result == [picam_device]
