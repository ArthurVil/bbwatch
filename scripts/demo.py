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

    print(f"\n{'=' * 60}")
    print(f"🎤 BBWatch Audio Detection Demo")
    print(f"{'=' * 60}")
    print(f"Duration: {duration}s | Sample rate: {config.audio.sample_rate} Hz")
    print(f"Bandpass: {config.detection.bandpass_low_hz}-{config.detection.bandpass_high_hz} Hz")
    print(f"RMS Threshold: {config.detection.rms_threshold}")
    print()

    # Show available audio devices
    print("Available audio devices:")
    devices = detect_audio_devices()
    if devices:
        for dev in devices:
            print(f"  {dev}")
    else:
        print("  (using system default)")
    print()

    # Record
    print(f"🔴 Recording for {duration}s... (make some noise!)")
    try:
        audio = sd.rec(
            int(duration * config.audio.sample_rate),
            samplerate=config.audio.sample_rate,
            channels=config.audio.channels,
            dtype="float32",
        )
        sd.wait()
    except Exception as e:
        print(f"❌ Recording failed: {e}")
        print('   Try: python -c "import sounddevice; print(sounddevice.query_devices())"')
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
    is_cry, rms, active_ratio = detect_cry(
        wav_path=tmp_path,
        lowcut=config.detection.bandpass_low_hz,
        highcut=config.detection.bandpass_high_hz,
        rms_threshold=config.detection.rms_threshold,
        min_active_ratio=config.detection.min_active_ratio,
    )

    print(f"📊 Results:")
    print(f"   RMS Energy:    {rms:.4f} (threshold: {config.detection.rms_threshold:.4f})")
    print(f"   Active Ratio:  {active_ratio:.2%} (min: {config.detection.min_active_ratio:.0%})")
    print()

    if is_cry:
        print("🔴 ALERT: Cry/loud noise detected!")
    else:
        print("🟢 OK: No cry detected")

    return is_cry


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
