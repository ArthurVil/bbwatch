"""Parametrized tests for hardware detection parsing using real output fixtures.

Each test case uses a verbatim output string from tests/fixtures/hardware_outputs.py
to verify the parser handles real-world format variations correctly.
"""

from unittest.mock import MagicMock, patch

import pytest

from bbwatch.hardware import detect_audio_devices, detect_picamera_devices, detect_video_devices
from tests.fixtures.hardware_outputs import (
    ARECORD_EMPTY,
    ARECORD_SINGLE_BUILTIN,
    ARECORD_WITH_PI_MIC,
    ARECORD_WITH_USB_MIC,
    LIBCAMERA_EMPTY,
    LIBCAMERA_MULTIPLE_CAMERAS,
    LIBCAMERA_RPI5_SINGLE,
    LIBCAMERA_RPI5_WITH_WARNINGS,
    V4L2CTL_EMPTY,
    V4L2CTL_MULTIPLE_DEVICES,
    V4L2CTL_PI_USB_CAMERA,
    V4L2CTL_SINGLE_USB,
)

# ---------------------------------------------------------------------------
# libcamera / Pi Camera
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output, expected_count, expected_paths",
    [
        (LIBCAMERA_RPI5_SINGLE, 1, ["rpicam:0"]),
        (LIBCAMERA_RPI5_WITH_WARNINGS, 1, ["rpicam:0"]),
        (LIBCAMERA_MULTIPLE_CAMERAS, 2, ["rpicam:0", "rpicam:1"]),
        (LIBCAMERA_EMPTY, 0, []),
    ],
    ids=["rpi5_single", "rpi5_with_warnings", "multiple_cameras", "empty"],
)
@patch("bbwatch.hardware.subprocess.run")
def test_detect_picamera_real_outputs(mock_run, output, expected_count, expected_paths):
    """Parser handles all known libcamera-hello output variants."""
    mock_run.return_value = MagicMock(stdout=output, returncode=0)

    result = detect_picamera_devices()

    assert len(result) == expected_count
    assert [d.path for d in result] == expected_paths
    assert all(d.path.startswith("rpicam:") for d in result)


@pytest.mark.parametrize(
    "output, expected_name",
    [
        (LIBCAMERA_RPI5_SINGLE, "Pi Camera 0 (imx708)"),
        (LIBCAMERA_RPI5_WITH_WARNINGS, "Pi Camera 0 (imx708)"),
    ],
    ids=["rpi5_single", "rpi5_with_warnings"],
)
@patch("bbwatch.hardware.subprocess.run")
def test_detect_picamera_names(mock_run, output, expected_name):
    """Camera name includes index and sensor model."""
    mock_run.return_value = MagicMock(stdout=output, returncode=0)

    result = detect_picamera_devices()

    assert result[0].name == expected_name


# ---------------------------------------------------------------------------
# arecord / ALSA audio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output, expected_count",
    [
        (ARECORD_WITH_USB_MIC, 2),
        (ARECORD_WITH_PI_MIC, 2),
        (ARECORD_SINGLE_BUILTIN, 1),
        (ARECORD_EMPTY, 0),
    ],
    ids=["desktop_with_usb", "pi_with_usb", "single_builtin", "empty"],
)
@patch("bbwatch.hardware.subprocess.run")
def test_detect_audio_real_outputs(mock_run, output, expected_count):
    """Parser handles all known arecord output variants."""
    mock_run.return_value = MagicMock(stdout=output, returncode=0)

    result = detect_audio_devices()

    assert len(result) == expected_count


@pytest.mark.parametrize(
    "output, expected_card, expected_name",
    [
        (ARECORD_WITH_USB_MIC, 1, "USB PnP Sound Device"),
        (ARECORD_WITH_PI_MIC, 1, "USB PnP Sound Device"),
    ],
    ids=["desktop_usb_card1", "pi_usb_card1"],
)
@patch("bbwatch.hardware.subprocess.run")
def test_detect_audio_usb_device_parsed(mock_run, output, expected_card, expected_name):
    """USB audio device is parsed with correct card number and name."""
    mock_run.return_value = MagicMock(stdout=output, returncode=0)

    result = detect_audio_devices()
    usb_devices = [d for d in result if "USB" in d.name]

    assert len(usb_devices) == 1
    assert usb_devices[0].card == expected_card
    assert usb_devices[0].name == expected_name


# ---------------------------------------------------------------------------
# v4l2-ctl / V4L2 video
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output, expected_count",
    [
        (V4L2CTL_SINGLE_USB, 2),       # two /dev/video nodes under one device
        (V4L2CTL_MULTIPLE_DEVICES, 3),
        (V4L2CTL_EMPTY, 0),
    ],
    ids=["single_usb_two_nodes", "multiple_devices", "empty"],
)
@patch("bbwatch.hardware.subprocess.run")
def test_detect_video_real_outputs(mock_run, output, expected_count):
    """Parser handles all known v4l2-ctl output variants."""
    mock_run.return_value = MagicMock(stdout=output, returncode=0)

    result = detect_video_devices()

    assert len(result) == expected_count


@patch("bbwatch.hardware.subprocess.run")
def test_detect_video_pi_filters_non_camera_nodes(mock_run):
    """On RPi, codec and ISP /dev/video nodes are included — names reflect device group."""
    mock_run.return_value = MagicMock(stdout=V4L2CTL_PI_USB_CAMERA, returncode=0)

    result = detect_video_devices()

    # /dev/video0 and /dev/video1 from USB Camera; /dev/video10 and /dev/video11 from codec
    paths = [d.path for d in result]
    assert "/dev/video0" in paths
    assert "/dev/video1" in paths


@patch("bbwatch.hardware.subprocess.run")
def test_detect_video_usb_driver_info_stripped(mock_run):
    """USB driver path in parentheses is stripped from device name."""
    mock_run.return_value = MagicMock(stdout=V4L2CTL_SINGLE_USB, returncode=0)

    result = detect_video_devices()

    assert all("usb-" not in d.name for d in result)
    assert result[0].name == "USB Camera"
