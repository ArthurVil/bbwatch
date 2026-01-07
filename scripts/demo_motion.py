#!/usr/bin/env python3
"""Demo script for testing bbwatch motion detection.

This script demonstrates the motion detection pipeline of bbwatch.
It connects to a webcam, displays the live feed, and shows real-time
motion detection with visual feedback.

Features:
- Webcam capture via OpenCV
- Real-time motion detection using frame differencing
- Adjustable sensitivity threshold
- Visual overlay showing detected motion regions
- Motion history display

Controls:
    '+'/'-': Adjust motion sensitivity
    's': Toggle motion region display (contours)
    'm': Toggle motion history visualization
    'q': Quit

Usage:
    python scripts/demo_motion.py
    python scripts/demo_motion.py --device 1  # Use /dev/video1
    python scripts/demo_motion.py --threshold 25  # Set sensitivity

Requirements:
    pip install opencv-python numpy
    (Included in `make install-dev`)
"""

import argparse
import logging
import sys
import time
from collections import deque
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


def run_motion_demo(device: int = 0, threshold: int = 25, blur_size: int = 21) -> None:
    """Run motion detection demo with webcam.

    Args:
        device: Video device index (0 = /dev/video0).
        threshold: Motion detection threshold (0-255, lower = more sensitive).
        blur_size: Gaussian blur kernel size for noise reduction (must be odd).
    """
    if not check_opencv():
        print("❌ OpenCV not installed. Run: pip install opencv-python")
        sys.exit(1)

    import cv2
    import numpy as np

    print("\n" + "=" * 60)
    print("🎥 BBWatch Motion Detection Demo")
    print("=" * 60)
    print(f"Device: {device}")
    print(f"Threshold: {threshold} (lower = more sensitive)")
    print("Controls:")
    print("  '+'/'-': Adjust sensitivity")
    print("  's': Toggle contour display")
    print("  'm': Toggle motion history")
    print("  'q': Quit")
    print("=" * 60 + "\n")

    cap = cv2.VideoCapture(device)

    if not cap.isOpened():
        print(f"❌ Could not open video device {device}")
        sys.exit(1)

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)

    print(f"📹 Opened: {width}x{height} @ {fps:.1f} FPS")
    print("   Window will open shortly...\n")

    # Motion detection state
    prev_gray = None
    motion_threshold = threshold
    show_contours = True
    show_history = True
    motion_history = deque(maxlen=100)  # Last 100 frames of motion percentage

    frame_count = 0
    start_time = time.time()

    # Motion accumulator for visualization
    motion_accumulator = None

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("⚠️  Frame capture failed")
                break

            frame_count += 1
            display_frame = frame.copy()

            # Convert to grayscale
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (blur_size, blur_size), 0)

            motion_detected = False
            motion_percent = 0.0

            if prev_gray is not None:
                # Frame difference
                frame_diff = cv2.absdiff(prev_gray, gray)

                # Threshold to binary
                _, thresh = cv2.threshold(frame_diff, motion_threshold, 255, cv2.THRESH_BINARY)

                # Dilate to fill gaps
                thresh = cv2.dilate(thresh, None, iterations=2)

                # Calculate motion percentage
                motion_pixels = np.sum(thresh > 0)
                total_pixels = thresh.shape[0] * thresh.shape[1]
                motion_percent = (motion_pixels / total_pixels) * 100
                motion_history.append(motion_percent)

                # Determine if significant motion
                motion_detected = motion_percent > 1.0  # > 1% of frame is moving

                # Update motion accumulator for heat map
                if motion_accumulator is None:
                    motion_accumulator = np.zeros_like(gray, dtype=np.float32)
                motion_accumulator = motion_accumulator * 0.9 + thresh.astype(np.float32) * 0.1

                # Find and draw contours
                if show_contours:
                    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    for contour in contours:
                        if cv2.contourArea(contour) > 500:  # Filter small contours
                            x, y, w, h = cv2.boundingRect(contour)
                            cv2.rectangle(display_frame, (x, y), (x + w, y + h), (0, 255, 0), 2)

                # Show motion history visualization
                if show_history and motion_accumulator is not None:
                    heat = cv2.normalize(motion_accumulator, None, 0, 255, cv2.NORM_MINMAX)
                    heat = heat.astype(np.uint8)
                    heat_colored = cv2.applyColorMap(heat, cv2.COLORMAP_JET)
                    display_frame = cv2.addWeighted(display_frame, 0.7, heat_colored, 0.3, 0)

            prev_gray = gray.copy()

            # Draw motion alert
            if motion_detected:
                cv2.putText(
                    display_frame,
                    f"MOTION DETECTED ({motion_percent:.1f}%)",
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 0, 255),
                    2,
                )
            else:
                cv2.putText(
                    display_frame,
                    f"No motion ({motion_percent:.1f}%)",
                    (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.8,
                    (0, 255, 0),
                    2,
                )

            # Draw threshold info
            cv2.putText(
                display_frame,
                f"Threshold: {motion_threshold}",
                (10, 60),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (255, 255, 255),
                1,
            )

            # Draw mini motion graph
            if len(motion_history) > 1:
                graph_height = 50
                graph_width = 200
                graph_x = width - graph_width - 10
                graph_y = 10

                # Background
                cv2.rectangle(
                    display_frame,
                    (graph_x, graph_y),
                    (graph_x + graph_width, graph_y + graph_height),
                    (40, 40, 40),
                    -1,
                )

                # Plot motion history
                max_motion = max(motion_history) if max(motion_history) > 0 else 1
                points = []
                for i, m in enumerate(motion_history):
                    px = graph_x + int(i * graph_width / len(motion_history))
                    py = graph_y + graph_height - int(m * graph_height / max(10, max_motion))
                    points.append((px, py))

                if len(points) > 1:
                    for i in range(len(points) - 1):
                        cv2.line(display_frame, points[i], points[i + 1], (0, 255, 0), 1)

            # Show FPS
            elapsed = time.time() - start_time
            if elapsed > 0:
                current_fps = frame_count / elapsed
                cv2.putText(
                    display_frame,
                    f"FPS: {current_fps:.1f}",
                    (10, height - 10),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (255, 255, 255),
                    1,
                )

            cv2.imshow("BBWatch Motion Demo", display_frame)

            # Handle keyboard
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            elif key == ord("+") or key == ord("="):
                motion_threshold = min(255, motion_threshold + 5)
                print(f"Threshold: {motion_threshold} (less sensitive)")
            elif key == ord("-"):
                motion_threshold = max(1, motion_threshold - 5)
                print(f"Threshold: {motion_threshold} (more sensitive)")
            elif key == ord("s"):
                show_contours = not show_contours
                print(f"Contours: {'ON' if show_contours else 'OFF'}")
            elif key == ord("m"):
                show_history = not show_history
                print(f"Motion history: {'ON' if show_history else 'OFF'}")

    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        cv2.destroyAllWindows()
        print(f"\n✅ Captured {frame_count} frames in {elapsed:.1f}s ({frame_count / elapsed:.1f} FPS)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BBWatch motion detection demo - webcam with motion tracking",
    )

    parser.add_argument(
        "--device",
        "-d",
        type=int,
        default=0,
        help="Video device index (default: 0 = /dev/video0)",
    )

    parser.add_argument(
        "--threshold",
        "-t",
        type=int,
        default=25,
        help="Motion threshold 0-255 (default: 25, lower = more sensitive)",
    )

    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List available video devices and exit",
    )

    args = parser.parse_args()

    if args.list:
        from demo_video import list_video_devices

        devices = list_video_devices()
        if devices:
            print("OpenCV-accessible devices:")
            for dev in devices:
                print(f"  {dev}")
        return 0

    run_motion_demo(device=args.device, threshold=args.threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
