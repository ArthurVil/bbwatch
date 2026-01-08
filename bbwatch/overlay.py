"""Visual overlay generation for video stream alerts.

Generates overlay images (red/blue/transparent) based on the
current detection status for compositing with the video feed.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, cast

LOGGER = logging.getLogger(__name__)

# Alert states
ALERT_NONE = "none"
ALERT_CRY = "cry"
ALERT_ERROR = "error"


def create_overlay_image(
    output_path: Path,
    color: tuple[int, int, int, int],
    width: int = 640,
    height: int = 480,
) -> None:
    """Create an RGBA overlay image.

    Uses pure Python to avoid PIL dependency - creates a simple
    PPM/PAM file that can be converted by FFmpeg.

    Args:
        output_path: Path to save the image.
        color: RGBA tuple (0-255 for each channel).
        width: Image width.
        height: Image height.
    """
    output_path = Path(output_path)

    # Create raw RGBA data
    r, g, b, a = color
    pixel = bytes([r, g, b, a])
    data = pixel * (width * height)

    # Write as raw RGBA file (can be used by FFmpeg with -f rawvideo)
    # Using .rgba extension for clarity
    rgba_path = output_path.with_suffix(".rgba")
    rgba_path.write_bytes(data)

    # Also create a simple PPM as fallback (no alpha, but widely supported)
    ppm_path = output_path.with_suffix(".ppm")
    with open(ppm_path, "wb") as f:
        f.write(f"P6\n{width} {height}\n255\n".encode())
        rgb_pixel = bytes([r, g, b])
        f.write(rgb_pixel * (width * height))

    LOGGER.debug(f"Created overlay: {output_path.name} ({color})")


def generate_overlay_set(overlay_dir: Path, width: int = 640, height: int = 480) -> None:
    """Generate the full set of overlay images.

    Creates:
    - none.rgba: Fully transparent (no overlay)
    - cry.rgba: Red tint at 30% opacity
    - error.rgba: Blue tint at 30% opacity

    Args:
        overlay_dir: Directory to save overlays.
        width: Image width.
        height: Image height.
    """
    overlay_dir = Path(overlay_dir)
    overlay_dir.mkdir(parents=True, exist_ok=True)

    overlays = {
        "none": (0, 0, 0, 0),  # Transparent
        "cry": (255, 0, 0, 77),  # Red at 30% opacity (255 * 0.3)
        "error": (0, 0, 255, 77),  # Blue at 30% opacity
    }

    for name, color in overlays.items():
        create_overlay_image(
            overlay_dir / name,
            color,
            width,
            height,
        )

    LOGGER.info(f"Generated overlay set in {overlay_dir}")


def read_status(status_file: Path) -> dict[str, Any] | None:
    """Read the current status from status.json.

    Args:
        status_file: Path to status.json.

    Returns:
        Status dictionary, or None if file doesn't exist or is invalid.
    """
    try:
        if not status_file.exists():
            return None

        with open(status_file) as f:
            return cast(dict[str, Any], json.load(f))

    except (json.JSONDecodeError, OSError) as e:
        LOGGER.warning(f"Failed to read status file: {e}")
        return None


def get_alert_state(status_file: Path, health_timeout_s: float = 10.0) -> str:
    """Determine the current alert state from status file.

    Args:
        status_file: Path to status.json.
        health_timeout_s: Seconds without update before considering system unhealthy.

    Returns:
        One of ALERT_NONE, ALERT_CRY, or ALERT_ERROR.
    """
    status = read_status(status_file)

    if status is None:
        return ALERT_ERROR

    # Check system health
    timestamp = status.get("timestamp", 0)
    if time.time() - timestamp > health_timeout_s:
        LOGGER.warning("Status file stale - system may be unhealthy")
        return ALERT_ERROR

    system_status = status.get("system_status", "unknown")
    if system_status != "ok":
        return ALERT_ERROR

    # Check for active alert
    if status.get("alert_active", False):
        return ALERT_CRY

    return ALERT_NONE


def update_current_overlay(overlay_dir: Path, status_file: Path, health_timeout_s: float = 10.0) -> str:
    """Update the current overlay symlink based on status.

    Creates a symlink 'current.rgba' pointing to the appropriate overlay.

    Args:
        overlay_dir: Directory containing overlay images.
        status_file: Path to status.json.
        health_timeout_s: Health timeout in seconds.

    Returns:
        Current alert state.
    """
    overlay_dir = Path(overlay_dir)
    status_file = Path(status_file)

    state = get_alert_state(status_file, health_timeout_s)

    # Map state to overlay file
    overlay_file = overlay_dir / f"{state}.ppm"
    current_link = overlay_dir / "current.ppm"

    if not overlay_file.exists():
        LOGGER.error(f"Overlay file not found: {overlay_file}")
        return state

    try:
        # Atomic update: create temp link, then rename
        temp_link = overlay_dir / "current.tmp"
        if temp_link.exists():
            temp_link.unlink()

        temp_link.symlink_to(overlay_file.name)
        temp_link.rename(current_link)

    except OSError as e:
        LOGGER.error(f"Failed to update overlay symlink: {e}")

    # Also update RGBA link for backward compatibility or raw usage
    try:
        rgba_file = overlay_dir / f"{state}.rgba"
        rgba_link = overlay_dir / "current.rgba"
        rgba_temp = overlay_dir / "current_rgba.tmp"
        if rgba_file.exists():
            if rgba_temp.exists():
                rgba_temp.unlink()
            rgba_temp.symlink_to(rgba_file.name)
            rgba_temp.rename(rgba_link)
    except OSError:
        pass  # Non-critical

    return state


class OverlayController:
    """Monitors status and updates overlay in real-time.

    Polls the status file and updates the overlay symlink
    when the alert state changes.
    """

    def __init__(
        self,
        overlay_dir: Path,
        status_file: Path,
        poll_interval_s: float = 0.5,
        health_timeout_s: float = 10.0,
    ) -> None:
        """Initialize the overlay controller.

        Args:
            overlay_dir: Directory containing overlay images.
            status_file: Path to status.json.
            poll_interval_s: Interval between status checks.
            health_timeout_s: Health timeout in seconds.
        """
        self.overlay_dir = Path(overlay_dir)
        self.status_file = Path(status_file)
        self.poll_interval_s = poll_interval_s
        self.health_timeout_s = health_timeout_s
        self._current_state = ALERT_NONE

    def setup(self) -> None:
        """Generate overlay images if they don't exist."""
        if not (self.overlay_dir / "cry.rgba").exists():
            generate_overlay_set(self.overlay_dir)

    def update(self) -> str:
        """Update overlay based on current status.

        Returns:
            Current alert state.
        """
        new_state = update_current_overlay(
            self.overlay_dir,
            self.status_file,
            self.health_timeout_s,
        )

        if new_state != self._current_state:
            LOGGER.info(f"Overlay state changed: {self._current_state} -> {new_state}")
            self._current_state = new_state

        return new_state

    @property
    def current_state(self) -> str:
        """Get the current alert state."""
        return self._current_state
