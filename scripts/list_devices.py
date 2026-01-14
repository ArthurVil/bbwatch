import subprocess
import sys
from pathlib import Path


def collect_audio_alsa() -> list[dict[str, str]]:
    """Collect ALSA audio devices."""
    devices = []
    try:
        result = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
        if result.stdout.strip():
            import re

            pattern = re.compile(r"card (\d+):.*\[(.+?)\].*device (\d+):")
            for line in result.stdout.splitlines():
                match = pattern.search(line)
                if match:
                    card, name, dev_id = match.groups()
                    devices.append({"name": name, "device_index": f"hw:{card},{dev_id}"})
    except Exception:
        pass
    return devices


def collect_audio_pulse() -> list[dict[str, str]]:
    """Collect PulseAudio sources."""
    sources = []
    try:
        result = subprocess.run(["pactl", "list", "sources", "short"], capture_output=True, text=True, timeout=5)
        if result.stdout.strip():
            for line in result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    sources.append({"name": parts[1], "device_index": parts[1]})
    except Exception:
        pass
    return sources


def collect_video() -> list[dict[str, str]]:
    """Collect video devices."""
    devices = []
    try:
        result = subprocess.run(["v4l2-ctl", "--list-devices"], capture_output=True, text=True, timeout=5)
        if result.stdout.strip():
            lines = result.stdout.splitlines()
            current_name = "Unknown"
            for line in lines:
                if line.endswith(":") and not line.startswith("\t"):
                    current_name = line.rstrip(":").strip()
                elif "/dev/video" in line:
                    path = line.strip()
                    # Only take the first video device for each hardware block (usually the capture one)
                    if not any(d["name"] == current_name for d in devices):
                        devices.append({"name": current_name, "path": path})
    except Exception:
        # Fallback to /dev/video* glob
        for dev in sorted(Path("/dev").glob("video*")):
            if not any(d["path"] == str(dev) for d in devices):
                devices.append({"name": f"Video Device ({dev.name})", "path": str(dev)})
    return devices


def main() -> int:
    print("=" * 60)
    print("HARDWARE DISCOVERY")
    print("=" * 60)
    print("Scanning for audio and video devices...\n")

    audio_alsa = collect_audio_alsa()
    audio_pulse = collect_audio_pulse()
    video_devs = collect_video()

    if not any([audio_alsa, audio_pulse, video_devs]):
        print("❌ No devices found.")
        return 1

    print("FOUND DEVICES:")
    if audio_alsa:
        print("\n  AUDIO (ALSA):")
        for i, d in enumerate(audio_alsa):
            print(f"    [{i}] {d['name']} ({d['device_index']})")

    if audio_pulse:
        print("\n  AUDIO (PulseAudio/PipeWire):")
        for i, d in enumerate(audio_pulse):
            print(f"    [P{i}] {d['name']}")

    if video_devs:
        print("\n  VIDEO (V4L2):")
        for i, d in enumerate(video_devs):
            print(f"    [V{i}] {d['name']} ({d['path']})")

    print("\n" + "=" * 60)
    print("SUGGESTED CONFIGURATION (Copy-paste this into config.yaml)")
    print("=" * 60)

    # Use first found devices as default suggestion
    audio_idx = (
        audio_alsa[0]["device_index"] if audio_alsa else (audio_pulse[0]["device_index"] if audio_pulse else "0")
    )
    pulse_src = audio_pulse[0]["name"] if audio_pulse else ""
    video_path = video_devs[0]["path"] if video_devs else "/dev/video0"

    print(f"""
audio:
  device_index: "{audio_idx}"

host:
  video_device: "{video_path}"
  pulse_source: "{pulse_src}"
""")

    print("-" * 60)
    print("go2rtc.yaml suggestion (Update the 'exec:ffmpeg' names):")
    print("-" * 60)
    print(f"Video device: {video_path}")
    if pulse_src:
        print(f"Pulse source: {pulse_src}")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
