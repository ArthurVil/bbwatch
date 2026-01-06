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


def record_with_arecord(config: BBWatchConfig, duration: float, device: str):
    """Record using arecord subprocess (fallback for when PortAudio misses devices)."""
    import subprocess
    import numpy as np

    cmd = [
        "arecord",
        "-D",
        device,
        "-f",
        "S32_LE",
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
        bytes_per_sample = 4
        expected_samples = int(duration * config.audio.sample_rate)
        total_bytes = expected_samples * bytes_per_sample * config.audio.channels
        data_bytes = proc.stdout.read(total_bytes)
        proc.terminate()
        if len(data_bytes) == 0:
            stderr = proc.stderr.read().decode()
            raise RuntimeError(f"arecord failed/empty: {stderr}")
        audio = np.frombuffer(data_bytes, dtype=np.int32)
        audio = audio.astype(np.float32) / np.iinfo(np.int32).max
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
    try:
        import sounddevice as sd
        import soundfile as sf
    except ImportError:
        print("❌ sounddevice not installed. Run: pip install sounddevice")
        sys.exit(1)

    print("=" * 60)
    print("🎤 BBWatch Audio Detection Demo")
    print("=" * 60)
    print(f"Duration: {duration}s | Sample rate: {config.audio.sample_rate} Hz")
    print(f"Bandpass: {config.detection.bandpass_low_hz}-{config.detection.bandpass_high_hz} Hz")
    print(f"RMS Threshold: {config.detection.rms_threshold}")
    print()

    # Show available audio devices (SoundDevice / PortAudio)
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
    try:
        device = config.audio.device_index
        if device is not None:
            print(f"🎤 Using configured device: {device}")

        audio = sd.rec(
            int(duration * config.audio.sample_rate),
            samplerate=config.audio.sample_rate,
            channels=config.audio.channels,
            dtype="float32",
            device=device,
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

    # Save temporarily
    tmp_path = Path("/tmp/bbwatch_demo.wav")
    sf.write(tmp_path, audio, config.audio.sample_rate)

    # Check if empty
    if is_segment_empty(tmp_path):
        print("📊 Result: SILENCE (no audio detected)")
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
