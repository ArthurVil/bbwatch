#!/usr/bin/env python3
"""Hardware discovery utility for bbwatch.

This script scans the system for available audio and video hardware
that can be used by bbwatch. It checks:
1. ALSA audio capture devices (arecord)
2. PulseAudio/PipeWire sources (pactl)
3. V4L2 video devices (v4l2-ctl)
4. /dev/video* device files

It also suggests the correct Docker arguments (`--device ...`) to expose
these devices to the bbwatch container.

Usage:
    python scripts/list_devices.py
    make devices
"""

import subprocess
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def list_audio_devices() -> None:
    """List ALSA audio capture devices."""
    print("=" * 60)
    print("AUDIO DEVICES (ALSA)")
    print("=" * 60)

    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            print(result.stdout)
        else:
            print("No audio capture devices found.\n")
    except FileNotFoundError:
        print("arecord not found. Install alsa-utils.\n")
    except subprocess.TimeoutExpired:
        print("arecord timed out.\n")

    # Also try PulseAudio/PipeWire
    print("-" * 60)
    print("AUDIO SOURCES (PulseAudio/PipeWire)")
    print("-" * 60)

    try:
        result = subprocess.run(
            ["pactl", "list", "sources", "short"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            print(result.stdout)
        else:
            print("No PulseAudio sources found.\n")
    except FileNotFoundError:
        print("pactl not found. PulseAudio not available.\n")
    except subprocess.TimeoutExpired:
        print("pactl timed out.\n")


def list_video_devices() -> None:
    """List V4L2 video devices."""
    print("=" * 60)
    print("VIDEO DEVICES (V4L2)")
    print("=" * 60)

    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            print(result.stdout)
        else:
            print("No video devices found.\n")
    except FileNotFoundError:
        print("v4l2-ctl not found. Install v4l-utils.\n")
    except subprocess.TimeoutExpired:
        print("v4l2-ctl timed out.\n")

    # List /dev/video* files
    print("-" * 60)
    print("VIDEO DEVICE FILES")
    print("-" * 60)

    video_devices = sorted(Path("/dev").glob("video*"))
    if video_devices:
        for dev in video_devices:
            print(f"  {dev}")
        print()
    else:
        print("No /dev/video* devices found.\n")


def list_docker_device_args() -> None:
    """Suggest Docker device arguments."""
    print("=" * 60)
    print("DOCKER DEVICE ARGUMENTS")
    print("=" * 60)

    video_devices = sorted(Path("/dev").glob("video*"))
    audio_available = Path("/dev/snd").exists()

    args = []
    if video_devices:
        args.append(f"--device {video_devices[0]}:{video_devices[0]}")
        args.append("--group-add video")
    if audio_available:
        args.append("--device /dev/snd:/dev/snd")
        args.append("--group-add audio")

    if args:
        print("Suggested Docker run arguments:")
        print()
        print("  docker run --rm -it \\")
        for arg in args:
            print(f"    {arg} \\")
        print("    bbwatch:dev python scripts/demo.py")
    else:
        print("No suitable devices found for Docker passthrough.")
    print()


def main() -> int:
    list_audio_devices()
    list_video_devices()
    list_docker_device_args()
    return 0


if __name__ == "__main__":
    sys.exit(main())
