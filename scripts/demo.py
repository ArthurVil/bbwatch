#!/usr/bin/env python3
"""Demo script for testing bbwatch audio detection.

This script demonstrates the core audio detection pipeline of bbwatch.
It records audio from your default microphone input and processes it using
the same DSP logic (bandpass filter + RMS energy) used in the main application.

Features:
- Real-time recording from microphone
- Configurable recording duration
- Loop mode for continuous monitoring
- Visual feedback on detection (RMS energy, active ratio)

Usage:
    python scripts/demo.py              # Record 3s and analyze
    python scripts/demo.py --loop       # Run continuously
    python scripts/demo.py --duration 5 # Record 5s segments

Requirements:
    pip install sounddevice numpy scipy
    (Included in `make install-dev`)
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to path for development
sys.path.insert(0, str(Path(__file__).parent.parent))

from bbwatch.config import BBWatchConfig, get_default_config_path
from bbwatch.detector import detect_cry, is_segment_empty
from bbwatch.hardware import detect_audio_devices

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    datefmt="%H:%M:%S",
)

LOGGER = logging.getLogger(__name__)


def resolve_pulseaudio_source(alsa_device: str) -> str | None:
    """Resolve ALSA device string (e.g. 'hw:1,0') to PulseAudio source name.

    Args:
        alsa_device: ALSA device string.

    Returns:
        PulseAudio source name (e.g. 'alsa_input.usb-...') or None if not found.
    """
    import subprocess
    import json
    import re

    # Extract card index from alsa device string
    # hw:1,0 -> card 1
    # plughw:1,0 -> card 1
    match = re.search(r"(?:hw|plughw):(\d+)", alsa_device)
    if not match:
        return None

    target_card_idx = match.group(1)

    try:
        # Get sources in JSON format
        result = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True, check=True)
        sources = json.loads(result.stdout)

        for source in sources:
            props = source.get("properties", {})
            # check both alsa.card and device.string approaches
            card_idx = props.get("alsa.card")

            # If alsa.card matches our target
            if card_idx == target_card_idx:
                return source.get("name")

    except (subprocess.CalledProcessError, json.JSONDecodeError, FileNotFoundError):
        pass

    return None


def record_with_pulseaudio(config: BBWatchConfig, duration: float, device_name: str):
    """Record using PulseAudio (parecord/parec) when PulseAudio is holding the device."""
    import subprocess
    import numpy as np

    # Try parecord first (newer), fall back to parec (older)
    cmd_base = None
    for cmd_name in ["parecord", "parec"]:
        try:
            subprocess.run([cmd_name, "--version"], capture_output=True, check=True, timeout=2)
            cmd_base = cmd_name
            break
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue

    if cmd_base is None:
        raise RuntimeError("Neither parecord nor parec found. Install pulseaudio-utils.")

    # Resolve specific source if device name looks like an ALSA string (e.g. "hw:1,0")
    if device_name.startswith("hw:") or device_name.startswith("plughw:"):
        resolved_source = resolve_pulseaudio_source(device_name)
        if resolved_source:
            print(f"🔍 Resolved ALSA device '{device_name}' to PulseAudio source: '{resolved_source}'")
            device_name = resolved_source
        else:
            print(f"⚠️  Could not resolve ALSA device '{device_name}' to PulseAudio source. Using raw name.")

    # PulseAudio device name format: alsa_input.usb-ESSENTIELB_WEBCAM_ESSENTIELB_W1_SN0001-02.mono-fallback
    # But we can also use the ALSA device directly if PulseAudio sees it
    cmd = [
        cmd_base,
        "--device=" + device_name,
        "--rate=" + str(config.audio.sample_rate),
        "--channels=" + str(config.audio.channels),
        "--format=s16le",
        "--raw",
    ]

    print(f"🎤 Recording with {cmd_base}: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        bytes_per_sample = 2  # S16_LE = 2 bytes
        expected_samples = int(duration * config.audio.sample_rate)
        total_bytes = expected_samples * bytes_per_sample * config.audio.channels

        import time

        time.sleep(duration)

        data_bytes = proc.stdout.read(total_bytes)
        proc.terminate()
        proc.wait(timeout=2)

        if len(data_bytes) == 0:
            stderr = proc.stderr.read().decode()
            raise RuntimeError(f"{cmd_base} failed/empty: {stderr}")

        audio = np.frombuffer(data_bytes, dtype=np.int16)
        audio = audio.astype(np.float32) / np.iinfo(np.int16).max
        if config.audio.channels > 1:
            audio = audio.reshape(-1, config.audio.channels)
        if len(audio) > expected_samples:
            audio = audio[:expected_samples]
        return audio
    except FileNotFoundError:
        print(f"❌ '{cmd_base}' not found. Please install pulseaudio-utils.")
        raise


def record_with_arecord(config: BBWatchConfig, duration: float, device: str):
    """Record using arecord subprocess (fallback for when PortAudio misses devices)."""
    import subprocess
    import numpy as np

    cmd = [
        "arecord",
        "-D",
        device,
        "-f",
        "S16_LE",  # 16-bit signed little-endian (widely supported)
        "-r",
        str(config.audio.sample_rate),
        "-c",
        str(config.audio.channels),
        "-d",
        str(int(duration) + 1),
        "-t",
        "raw",
    ]
    print(f"🎤 Recording with arecord: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        bytes_per_sample = 2  # S16_LE = 2 bytes
        expected_samples = int(duration * config.audio.sample_rate)
        total_bytes = expected_samples * bytes_per_sample * config.audio.channels
        data_bytes = proc.stdout.read(total_bytes)
        proc.terminate()
        if len(data_bytes) == 0:
            stderr = proc.stderr.read().decode()
            raise RuntimeError(f"arecord failed/empty: {stderr}")
        audio = np.frombuffer(data_bytes, dtype=np.int16)
        audio = audio.astype(np.float32) / np.iinfo(np.int16).max
        if config.audio.channels > 1:
            audio = audio.reshape(-1, config.audio.channels)
        if len(audio) > expected_samples:
            audio = audio[:expected_samples]
        return audio
    except FileNotFoundError:
        print("❌ 'arecord' not found. Please install alsa-utils.")
        raise


def record_and_detect(config: BBWatchConfig, duration: float = 3.0) -> bool:
    """Record from microphone and run detection.

    Args:
        config: BBWatch configuration.
        duration: Recording duration in seconds.

    Returns:
        True if cry was detected.
    """
    # Import soundfile for WAV writing (always needed)
    import soundfile as sf

    print("=" * 60)
    print("🎤 BBWatch Audio Detection Demo")
    print("=" * 60)
    print(f"Duration: {duration}s | Sample rate: {config.audio.sample_rate} Hz")
    print(f"Bandpass: {config.detection.bandpass_low_hz}-{config.detection.bandpass_high_hz} Hz")
    print(f"RMS Threshold: {config.detection.rms_threshold}")
    print()

    # Show available audio devices (SoundDevice / PortAudio) only if available
    import sounddevice as sd

    print("Available audio devices (PortAudio indices):")
    try:
        print(sd.query_devices())
    except Exception as e:
        print(f"Could not query devices: {e}")
    print()

    # Also show system hardware (ALSA) for reference
    print("System hardware (ALSA):")
    sys_devices = detect_audio_devices()
    if sys_devices:
        for dev in sys_devices:
            print(f"  {dev}")
    else:
        print("  (no ALSA devices found)")
    print()

    # Record
    print(f"🔴 Recording for {duration}s... (make some noise!)")

    device = config.audio.device_index
    audio = None

    # Determine if device is an ALSA string (needs arecord) or PortAudio index
    # ALSA strings start with "hw:" or "plughw:"
    is_alsa_device = (
        device is not None and isinstance(device, str) and (device.startswith("hw:") or device.startswith("plughw:"))
    )

    if is_alsa_device:
        # Prefer plughw: for automatic format conversion
        arecord_device = device if device.startswith("plughw:") else device.replace("hw:", "plughw:")
        print(f"🎤 Using ALSA device via arecord: {arecord_device}")
        try:
            audio = record_with_arecord(config, duration, arecord_device)
        except Exception as e:
            error_msg = str(e)
            # If device is busy, likely PulseAudio is holding it - try PulseAudio instead
            if "Device or resource busy" in error_msg or "busy" in error_msg.lower():
                print(f"⚠️  Device busy (likely PulseAudio). Trying PulseAudio interface...")
                try:
                    audio = record_with_pulseaudio(config, duration, arecord_device)
                except Exception as pa_error:
                    print(f"❌ PulseAudio also failed: {pa_error}")
                    print("\n💡 Tip: Close apps using the webcam, or run: pulseaudio -k")
                    return False
            else:
                print(f"❌ arecord failed: {e}")
                return False
    else:
        # PortAudio path
        try:
            import sounddevice as sd
        except ImportError:
            print("❌ PortAudio not available and device is not an ALSA string")
            print("   Install sounddevice: pip install sounddevice")
            return False

        try:
            if device is not None:
                print(f"🎤 Using PortAudio device index: {device}")
            else:
                print("🎤 Using default PortAudio device")

            audio = sd.rec(
                int(duration * config.audio.sample_rate),
                samplerate=config.audio.sample_rate,
                channels=config.audio.channels,
                dtype="float32",
                device=int(device) if device is not None else None,
            )
            sd.wait()
        except Exception as e:
            print(f"❌ Recording failed: {e}")
            try:
                print("\nDevice capabilities:")
                print(sd.query_devices())
            except Exception:
                pass
            return False

    print("✅ Recording complete!\n")

    # Debug: show raw audio stats
    import numpy as np

    raw_rms = np.sqrt(np.mean(audio**2))
    raw_max = np.max(np.abs(audio))
    print(f"📈 Raw audio stats: RMS={raw_rms:.6f}, Max={raw_max:.6f}")
    if raw_rms < 0.001:
        print("⚠️  Warning: Very low audio levels detected!")
    print()

    # Save temporarily
    tmp_path = Path("/tmp/bbwatch_demo.wav")
    sf.write(tmp_path, audio, config.audio.sample_rate)

    # Check if empty (use config threshold)
    if is_segment_empty(tmp_path, config.detection.silence_threshold):
        print(f"📊 Result: SILENCE (RMS < {config.detection.silence_threshold})")
        return False

    # Run detection
    result = detect_cry(
        wav_path=tmp_path,
        lowcut=config.detection.bandpass_low_hz,
        highcut=config.detection.bandpass_high_hz,
        rms_threshold=config.detection.rms_threshold,
        min_active_ratio=config.detection.min_active_ratio,
    )

    print(f"📊 Results:")
    print(f"   RMS Energy:    {result.filtered_rms:.4f} (threshold: {config.detection.rms_threshold:.4f})")
    print(f"   Active Ratio:  {result.active_ratio:.2%} (min: {config.detection.min_active_ratio:.0%})")
    print()

    if result.is_cry:
        print("🔴 ALERT: Cry/loud noise detected!")
    else:
        print("🟢 OK: No cry detected")

    return result.is_cry


def main() -> int:
    parser = argparse.ArgumentParser(
        description="BBWatch audio detection demo - records from mic and detects",
    )

    parser.add_argument(
        "--duration",
        "-d",
        type=float,
        default=3.0,
        help="Recording duration in seconds (default: 3.0)",
    )

    parser.add_argument(
        "--list-only",
        action="store_true",
        help="List available devices and exit",
    )

    parser.add_argument(
        "--loop",
        "-l",
        action="store_true",
        help="Continuously record and detect",
    )

    parser.add_argument(
        "--config",
        "-c",
        type=Path,
        default=None,
        help="Path to config.yaml",
    )

    args = parser.parse_args()

    # Load config
    config_path = args.config or get_default_config_path()
    if config_path.exists():
        config = BBWatchConfig.from_yaml(config_path)
        print(f"📝 Loaded config from: {config_path}")
    else:
        config = BBWatchConfig()
        print(f"📝 Using default config")

    config = config.resolve_paths()

    # Run
    if args.loop:
        print("Running in loop mode. Press Ctrl+C to stop.\n")
        try:
            while True:
                record_and_detect(config, args.duration)
                print("\n" + "-" * 60 + "\n")
        except KeyboardInterrupt:
            print("\n👋 Stopped.")
    else:
        record_and_detect(config, args.duration)

    return 0


if __name__ == "__main__":
    sys.exit(main())
