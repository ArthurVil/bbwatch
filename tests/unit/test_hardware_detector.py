"""Unit tests for HardwareDetector._parse_* methods and detection lifecycle."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bbwatch.hardware import AudioDevice, VideoDevice
from bbwatch.hardware_detector import HardwareDetector
from tests.fixtures.hardware_outputs import (
    ARECORD_EMPTY,
    ARECORD_SINGLE_BUILTIN,
    ARECORD_WITH_USB_MIC,
    LIBCAMERA_EMPTY,
    LIBCAMERA_MULTIPLE_CAMERAS,
    LIBCAMERA_RPI5_SINGLE,
    LIBCAMERA_RPI5_WITH_WARNINGS,
    V4L2CTL_EMPTY,
    V4L2CTL_MULTIPLE_DEVICES,
    V4L2CTL_SINGLE_USB,
)

# ---------------------------------------------------------------------------
# _parse_arecord
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output, expected_count",
    [
        (ARECORD_WITH_USB_MIC, 2),
        (ARECORD_SINGLE_BUILTIN, 1),
        (ARECORD_EMPTY, 0),
    ],
    ids=["two_devices", "single_device", "empty"],
)
def test_parse_arecord(output, expected_count):
    result = HardwareDetector._parse_arecord(output)
    assert len(result) == expected_count
    assert all(isinstance(d, AudioDevice) for d in result)


def test_parse_arecord_fields():
    result = HardwareDetector._parse_arecord(ARECORD_WITH_USB_MIC)
    usb = next(d for d in result if "USB" in d.name)
    assert usb.card == 1
    assert usb.device == 0
    assert usb.alsa_id == "hw:1,0"


# ---------------------------------------------------------------------------
# _parse_v4l2ctl
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output, expected_count",
    [
        (V4L2CTL_SINGLE_USB, 2),
        (V4L2CTL_MULTIPLE_DEVICES, 3),
        (V4L2CTL_EMPTY, 0),
    ],
    ids=["single_usb_two_nodes", "multiple_devices", "empty"],
)
def test_parse_v4l2ctl(output, expected_count):
    result = HardwareDetector._parse_v4l2ctl(output)
    assert len(result) == expected_count
    assert all(isinstance(d, VideoDevice) for d in result)


def test_parse_v4l2ctl_no_leading_tab_in_path():
    """Paths must not have leading tabs — regression guard for line_stripped vs .strip()."""
    result = HardwareDetector._parse_v4l2ctl(V4L2CTL_SINGLE_USB)
    assert all(d.path.startswith("/dev/") for d in result)


def test_parse_v4l2ctl_strips_driver_info():
    result = HardwareDetector._parse_v4l2ctl(V4L2CTL_SINGLE_USB)
    assert all("usb-" not in d.name for d in result)


# ---------------------------------------------------------------------------
# _parse_libcamera
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output, expected_count, expected_paths",
    [
        (LIBCAMERA_RPI5_SINGLE, 1, ["rpicam:0"]),
        (LIBCAMERA_RPI5_WITH_WARNINGS, 1, ["rpicam:0"]),
        (LIBCAMERA_MULTIPLE_CAMERAS, 2, ["rpicam:0", "rpicam:1"]),
        (LIBCAMERA_EMPTY, 0, []),
    ],
    ids=["single", "with_warnings", "multiple", "empty"],
)
def test_parse_libcamera(output, expected_count, expected_paths):
    result = HardwareDetector._parse_libcamera(output)
    assert len(result) == expected_count
    assert [d.path for d in result] == expected_paths
    assert all(isinstance(d, VideoDevice) for d in result)


def test_parse_libcamera_name_format():
    result = HardwareDetector._parse_libcamera(LIBCAMERA_RPI5_SINGLE)
    assert result[0].name == "Pi Camera 0 (imx708)"


# ---------------------------------------------------------------------------
# detect_* with subprocess mocking
# ---------------------------------------------------------------------------


@patch("bbwatch.hardware_detector.subprocess.run")
def test_detect_audio_returns_parsed(mock_run):
    mock_run.return_value = MagicMock(stdout=ARECORD_WITH_USB_MIC, returncode=0)
    result = HardwareDetector().detect_audio_devices()
    assert len(result) == 2


@patch("bbwatch.hardware_detector.subprocess.run")
def test_detect_audio_missing_tool(mock_run):
    mock_run.side_effect = FileNotFoundError()
    assert HardwareDetector().detect_audio_devices() == []


@patch("bbwatch.hardware_detector.subprocess.run")
def test_detect_audio_timeout(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired("arecord", 5)
    assert HardwareDetector().detect_audio_devices() == []


@patch("bbwatch.hardware_detector.subprocess.run")
def test_detect_video_returns_parsed(mock_run):
    mock_run.return_value = MagicMock(stdout=V4L2CTL_SINGLE_USB, returncode=0)
    result = HardwareDetector().detect_video_devices()
    assert len(result) == 2


@patch("bbwatch.hardware_detector.subprocess.run")
def test_detect_picamera_returns_parsed(mock_run):
    mock_run.return_value = MagicMock(stdout=LIBCAMERA_RPI5_SINGLE, returncode=0)
    result = HardwareDetector().detect_picamera_devices()
    assert len(result) == 1
    assert result[0].path == "rpicam:0"


@patch("bbwatch.hardware_detector.subprocess.run")
def test_detect_picamera_missing_libcamera(mock_run):
    mock_run.side_effect = FileNotFoundError()
    assert HardwareDetector().detect_picamera_devices() == []


# ---------------------------------------------------------------------------
# fake mode
# ---------------------------------------------------------------------------


def test_fake_mode_audio():
    result = HardwareDetector(fake=True).detect_audio_devices()
    assert len(result) == 1
    assert result[0].alsa_id == "hw:0,0"


def test_fake_mode_video():
    result = HardwareDetector(fake=True).detect_video_devices()
    assert len(result) == 1
    assert result[0].path == "/dev/video0"


def test_fake_mode_picamera():
    result = HardwareDetector(fake=True).detect_picamera_devices()
    assert len(result) == 1
    assert result[0].path == "rpicam:0"


def test_fake_mode_returns_hardware_types():
    """Fake devices must be the same AudioDevice/VideoDevice types as real detection."""
    audio = HardwareDetector(fake=True).detect_audio_devices()
    video = HardwareDetector(fake=True).detect_video_devices()
    assert isinstance(audio[0], AudioDevice)
    assert isinstance(video[0], VideoDevice)
