"""Generate test audio fixtures for testing."""

import argparse
import time
from pathlib import Path

import numpy as np
import soundfile as sf


def generate_silence(path: Path, duration_s: float = 3.0, sr: int = 16000) -> None:
    """Generate silent audio file.

    Args:
        path: Output file path.
        duration_s: Duration in seconds.
        sr: Sample rate.
    """
    samples = np.zeros(int(duration_s * sr), dtype=np.float32)
    sf.write(path, samples, sr)
    print(f"Generated: {path}")


def generate_baby_cry(path: Path, duration_s: float = 3.0, sr: int = 16000) -> None:
    """Generate synthetic baby cry (fundamental 400-600 Hz with harmonics).

    Args:
        path: Output file path.
        duration_s: Duration in seconds.
        sr: Sample rate.
    """
    t = np.linspace(0, duration_s, int(duration_s * sr))

    # Fundamental frequency modulation (crying is not monotone)
    f0 = 450 + 100 * np.sin(2 * np.pi * 2 * t)  # 450 Hz ± 100 Hz

    # Generate harmonics
    signal = np.zeros_like(t)
    for harmonic in [1, 2, 3, 4]:
        signal += (1 / harmonic) * np.sin(2 * np.pi * f0 * harmonic * t)

    # Add amplitude envelope (crying is pulsed)
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)  # 3 Hz pulsing
    signal *= envelope

    # Normalize
    signal = signal / np.max(np.abs(signal)) * 0.8

    sf.write(path, signal.astype(np.float32), sr)
    print(f"Generated: {path}")


def generate_adult_speech(path: Path, duration_s: float = 3.0, sr: int = 16000) -> None:
    """Generate synthetic adult speech (fundamental 100-200 Hz).

    Args:
        path: Output file path.
        duration_s: Duration in seconds.
        sr: Sample rate.
    """
    t = np.linspace(0, duration_s, int(duration_s * sr))

    # Lower fundamental for adult voice
    f0 = 150 + 30 * np.sin(2 * np.pi * 0.5 * t)  # 150 Hz ± 30 Hz

    signal = np.zeros_like(t)
    for harmonic in [1, 2, 3, 4, 5]:
        signal += (1 / harmonic) * np.sin(2 * np.pi * f0 * harmonic * t)

    signal = signal / np.max(np.abs(signal)) * 0.7
    sf.write(path, signal.astype(np.float32), sr)
    print(f"Generated: {path}")


def generate_white_noise(path: Path, duration_s: float = 3.0, sr: int = 16000) -> None:
    """Generate white noise.

    Args:
        path: Output file path.
        duration_s: Duration in seconds.
        sr: Sample rate.
    """
    samples = np.random.randn(int(duration_s * sr)).astype(np.float32)
    samples = samples / np.max(np.abs(samples)) * 0.3
    sf.write(path, samples, sr)
    print(f"Generated: {path}")


def generate_all_fixtures(output_dir: Path) -> None:
    """Generate all test fixtures.

    Args:
        output_dir: Directory to save fixtures.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    generate_silence(output_dir / "silence_3s.wav")
    generate_baby_cry(output_dir / "baby_cry_3s.wav")
    generate_adult_speech(output_dir / "adult_speech_3s.wav")
    generate_white_noise(output_dir / "white_noise_3s.wav")


def generate_loop(output_dir: Path, interval_s: float = 1.0) -> None:
    """Continuously generate audio files for integration testing.

    Alternates between silence and baby cry.

    Args:
        output_dir: Directory to save files.
        interval_s: Interval between files.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    counter = 0

    print(f"Generating audio files every {interval_s}s in {output_dir}")
    print("Press Ctrl+C to stop")

    try:
        while True:
            filename = output_dir / f"seg_{counter:05d}.wav"

            # Alternate: mostly silence, occasional cry
            if counter % 10 == 5:
                generate_baby_cry(filename, duration_s=3.0)
            else:
                generate_silence(filename, duration_s=3.0)

            counter += 1
            time.sleep(interval_s)

    except KeyboardInterrupt:
        print("\nStopped")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Generate test audio fixtures")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tests/fixtures"),
        help="Output directory",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Continuously generate files for integration testing",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Interval between files in loop mode",
    )

    args = parser.parse_args()

    if args.loop:
        generate_loop(args.output, args.interval)
    else:
        generate_all_fixtures(args.output)
        print(f"Generated fixtures in {args.output}")


if __name__ == "__main__":
    main()
