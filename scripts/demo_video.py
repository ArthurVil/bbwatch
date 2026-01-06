#!/usr/bin/env python3
"""Demo script for testing bbwatch video streaming.

Opens webcam and displays video with overlay capability.

Usage:
    python scripts/demo_video.py
    python scripts/demo_video.py --device /dev/video0
"""

import argparse
import logging
import sys
import time
from pathlib import Path

# Add project root to path for development
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    datefmt="%H:%M:%S",
)

LOGGER = logging.getLogger(__name__)


def check_opencv() -> bool:
    """Check if OpenCV is available."""
    try:
        import cv2

        return True
    except ImportError:
        return False


def list_video_devices() -> list[str]:
    """List available video devices."""
    import subprocess

    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        print("Available video devices:")
        for line in result.stdout.splitlines():
            if line.strip():
                print(f"  {line}")
        print()
    except FileNotFoundError:
        print("Note: v4l2-ctl not found, showing OpenCV device indices\n")

    # Try to find OpenCV-accessible devices
    if check_opencv():
        import cv2

        devices = []
        for i in range(10):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                devices.append(f"/dev/video{i} (index {i})")
                cap.release()
        return devices
    return []


def run_video_demo(device: int = 0, show_overlay: bool = True) -> None:
    """Run video capture demo with optional overlay.

    Args:
        device: Video device index (0 = /dev/video0).
        show_overlay: Whether to show overlay controls.
    """
    if not check_opencv():
        print("❌ OpenCV not installed. Run: pip install opencv-python")
        sys.exit(1)

    import cv2
    import numpy as np

    print("\n" + "=" * 60)
    print("🎥 BBWatch Video Demo")
    print("=" * 60)
    print(f"Device: {device}")
    print("Press 'q' to quit")
    print("Press 'r' to toggle RED overlay (cry alert)")
    print("Press 'b' to toggle BLUE overlay (error)")
    print("Press 'n' to clear overlay")
    print("=" * 60 + "\n")

    cap = cv2.VideoCapture(device)

    if not cap.isOpened():
        print(f"❌ Could not open video device {device}")
        print("   Try running: python scripts/demo_video.py --list")
        sys.exit(1)

    # Get video properties
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)

    print(f"📹 Opened: {width}x{height} @ {fps:.1f} FPS")
    print("   Window will open shortly...\n")

    overlay_mode = "none"  # none, red, blue
    frame_count = 0
    start_time = time.time()

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("⚠️  Frame capture failed")
                break

            frame_count += 1

            # Apply overlay
            if overlay_mode == "red":
                overlay = np.zeros_like(frame)
                overlay[:, :, 2] = 255  # Red channel
                frame = cv2.addWeighted(frame, 0.7, overlay, 0.3, 0)
                cv2.putText(frame, "ALERT: CRY DETECTED", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            elif overlay_mode == "blue":
                overlay = np.zeros_like(frame)
                overlay[:, :, 0] = 255  # Blue channel
                frame = cv2.addWeighted(frame, 0.7, overlay, 0.3, 0)
                cv2.putText(frame, "ERROR: SIGNAL LOST", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 0, 0), 2)

            # Show FPS
            elapsed = time.time() - start_time
            if elapsed > 0:
                current_fps = frame_count / elapsed
                cv2.putText(
                    frame, f"FPS: {current_fps:.1f}", (width - 120, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2
                )

            # Show timestamp
            timestamp = time.strftime("%H:%M:%S")
            cv2.putText(frame, timestamp, (10, height - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

            cv2.imshow("BBWatch Video Demo", frame)

            # Handle keyboard
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            elif key == ord("r"):
                overlay_mode = "red" if overlay_mode != "red" else "none"
                print(f"Overlay: {overlay_mode}")
            elif key == ord("b"):
                overlay_mode = "blue" if overlay_mode != "blue" else "none"
                print(f"Overlay: {overlay_mode}")
            elif key == ord("n"):
                overlay_mode = "none"
                print(f"Overlay: {overlay_mode}")

    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()
        print(f"\n✅ Captured {frame_count} frames in {elapsed:.1f}s ({current_fps:.1f} FPS)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BBWatch video demo - webcam with overlay",
    )

    parser.add_argument(
        "--device",
        "-d",
        type=int,
        default=0,
        help="Video device index (default: 0 = /dev/video0)",
    )

    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List available video devices and exit",
    )

    args = parser.parse_args()

    if args.list:
        devices = list_video_devices()
        if devices:
            print("OpenCV-accessible devices:")
            for dev in devices:
                print(f"  {dev}")
        return 0

    run_video_demo(device=args.device)
    return 0


if __name__ == "__main__":
    sys.exit(main())
