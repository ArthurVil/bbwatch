"""Pytest configuration and shared fixtures for bbwatch tests."""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf


@pytest.fixture
def tmp_wav_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for wav files."""
    wav_dir = tmp_path / "wav_segments"
    wav_dir.mkdir()
    return wav_dir


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def silence_wav(tmp_path: Path) -> Path:
    """Generate a silent audio file."""
    path = tmp_path / "silence.wav"
    samples = np.zeros(16000 * 3, dtype=np.float32)  # 3 seconds
    sf.write(path, samples, 16000)
    return path


@pytest.fixture
def baby_cry_wav(tmp_path: Path) -> Path:
    """Generate a synthetic baby cry audio file."""
    path = tmp_path / "baby_cry.wav"
    sr = 16000
    duration = 3.0
    t = np.linspace(0, duration, int(duration * sr))

    # Baby cry: 450 Hz fundamental with modulation
    f0 = 450 + 100 * np.sin(2 * np.pi * 2 * t)

    signal = np.zeros_like(t)
    for harmonic in [1, 2, 3, 4]:
        signal += (1 / harmonic) * np.sin(2 * np.pi * f0 * harmonic * t)

    # Pulsed envelope
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
    signal *= envelope
    signal = signal / np.max(np.abs(signal)) * 0.8

    sf.write(path, signal.astype(np.float32), sr)
    return path


@pytest.fixture
def adult_speech_wav(tmp_path: Path) -> Path:
    """Generate synthetic adult speech audio file."""
    path = tmp_path / "adult_speech.wav"
    sr = 16000
    duration = 3.0
    t = np.linspace(0, duration, int(duration * sr))

    # Adult voice: 100 Hz fundamental (lower pitch)
    f0 = 100 + 20 * np.sin(2 * np.pi * 0.5 * t)

    # Harmonics with faster decay (1/n^2)
    signal = np.zeros_like(t)
    for harmonic in [1, 2, 3, 4, 5]:
        signal += (1 / (harmonic**2)) * np.sin(2 * np.pi * f0 * harmonic * t)

    signal = signal / np.max(np.abs(signal)) * 0.1  # Very low amplitude to pass threshold test
    sf.write(path, signal.astype(np.float32), sr)
    return path


@pytest.fixture
def white_noise_wav(tmp_path: Path) -> Path:
    """Generate white noise audio file."""
    path = tmp_path / "white_noise.wav"
    sr = 16000
    duration = 3.0

    samples = np.random.randn(int(duration * sr)).astype(np.float32)
    samples = samples / np.max(np.abs(samples)) * 0.3

    sf.write(path, samples, sr)
    return path


@pytest.fixture
def test_config() -> dict:
    """Default DetectionConfig kwargs for tests.

    Only real DetectionConfig fields belong here — config models forbid
    unknown keys, so stray entries fail construction (as they should).
    """
    return {
        "bandpass_low_hz": 250.0,
        "bandpass_high_hz": 800.0,
        "rms_threshold": 0.02,
        "min_active_ratio": 0.3,
        "silence_threshold": 0.001,
    }
