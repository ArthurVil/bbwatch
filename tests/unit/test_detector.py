"""Unit tests for cry detection algorithm."""

import numpy as np
import pytest
from scipy.signal import sosfilt

from bbwatch.detector import butter_bandpass, detect_cry, is_segment_empty


class TestBandpassFilter:
    """Tests for bandpass filter design."""

    def test_bandpass_coefficients_valid(self):
        """Filter coefficients should be finite and stable."""
        sos = butter_bandpass(250, 800, fs=16000, order=4)
        assert np.all(np.isfinite(sos))
        assert sos.shape[0] == 4  # 4th order = 4 second-order sections

    def test_bandpass_attenuates_low_frequencies(self):
        """Frequencies below cutoff should be attenuated."""
        fs = 16000
        sos = butter_bandpass(250, 800, fs=fs)

        # Generate 100 Hz sine (should be attenuated)
        t = np.linspace(0, 1, fs)
        low_freq = np.sin(2 * np.pi * 100 * t)
        filtered = sosfilt(sos, low_freq)

        # Should attenuate by at least 90%
        assert np.std(filtered) < np.std(low_freq) * 0.1

    def test_bandpass_passes_target_frequencies(self):
        """Frequencies within passband should pass through."""
        fs = 16000
        sos = butter_bandpass(250, 800, fs=fs)

        # Generate 500 Hz sine (should pass)
        t = np.linspace(0, 1, fs)
        target_freq = np.sin(2 * np.pi * 500 * t)
        filtered = sosfilt(sos, target_freq)

        # Should retain at least 80% of the signal
        assert np.std(filtered) > np.std(target_freq) * 0.8

    def test_bandpass_attenuates_high_frequencies(self):
        """Frequencies above cutoff should be attenuated."""
        fs = 16000
        sos = butter_bandpass(250, 800, fs=fs)

        # Generate 2000 Hz sine (should be attenuated)
        t = np.linspace(0, 1, fs)
        high_freq = np.sin(2 * np.pi * 2000 * t)
        filtered = sosfilt(sos, high_freq)

        assert np.std(filtered) < np.std(high_freq) * 0.1

    def test_invalid_frequencies_raise_error(self):
        """Invalid frequency parameters should raise ValueError."""
        with pytest.raises(ValueError):
            butter_bandpass(-100, 800, fs=16000)  # Negative frequency

        with pytest.raises(ValueError):
            butter_bandpass(800, 250, fs=16000)  # Low > High

        with pytest.raises(ValueError):
            butter_bandpass(250, 10000, fs=16000)  # Above Nyquist


class TestCryDetection:
    """Tests for cry detection logic."""

    def test_silence_not_detected_as_cry(self, silence_wav):
        """Silent audio should never trigger an alert."""
        result = detect_cry(
            silence_wav,
            lowcut=250.0,
            highcut=800.0,
            rms_threshold=0.02,
            min_active_ratio=0.3,
            window_ms=100.0,
        )
        assert result.is_cry is False
        assert result.filtered_rms < 0.001
        assert result.active_ratio < 0.1

    def test_baby_cry_detected(self, baby_cry_wav):
        """Baby cry audio should trigger an alert."""
        result = detect_cry(
            baby_cry_wav,
            lowcut=250.0,
            highcut=800.0,
            rms_threshold=0.02,
            min_active_ratio=0.3,
            window_ms=100.0,
        )
        assert result.is_cry is True
        assert result.active_ratio > 0.3

    def test_adult_speech_not_detected(self, adult_speech_wav):
        """Adult speech should NOT trigger (different frequency profile)."""
        result = detect_cry(
            adult_speech_wav,
            lowcut=250.0,
            highcut=800.0,
            rms_threshold=0.02,
            min_active_ratio=0.3,
            window_ms=100.0,
        )
        # Adult fundamental is 85-180 Hz, should have reduced energy in cry band
        assert result.is_cry is False or result.active_ratio < 0.5

    def test_white_noise_limited_detection(self, white_noise_wav):
        """Broadband noise should not reliably trigger."""
        result = detect_cry(
            white_noise_wav,
            lowcut=250.0,
            highcut=800.0,
            rms_threshold=0.02,
            min_active_ratio=0.3,
            window_ms=100.0,
        )
        # White noise has distributed energy, ratio should be reduced
        assert result.active_ratio < 0.7

    def test_file_not_found_raises_error(self, tmp_path):
        """Missing file should raise FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            detect_cry(
                tmp_path / "nonexistent.wav",
                lowcut=250.0,
                highcut=800.0,
                rms_threshold=0.02,
                min_active_ratio=0.3,
                window_ms=100.0,
            )

    def test_duration_returned_correctly(self, baby_cry_wav):
        """Duration should match the actual file duration."""
        result = detect_cry(
            baby_cry_wav,
            lowcut=250.0,
            highcut=800.0,
            rms_threshold=0.02,
            min_active_ratio=0.3,
            window_ms=100.0,
        )
        assert abs(result.duration_s - 3.0) < 0.1

    def test_custom_thresholds(self, baby_cry_wav):
        """Custom thresholds should affect detection."""
        # Very high threshold should prevent detection
        result = detect_cry(
            baby_cry_wav,
            lowcut=250.0,
            highcut=800.0,
            rms_threshold=1.0,
            min_active_ratio=0.3,
            window_ms=100.0,
        )
        assert result.is_cry is False

        # Very low threshold should always detect
        result = detect_cry(
            baby_cry_wav,
            lowcut=250.0,
            highcut=800.0,
            rms_threshold=0.0001,
            min_active_ratio=0.01,
            window_ms=100.0,
        )
        assert result.is_cry is True


class TestSegmentEmpty:
    """Tests for empty segment detection."""

    def test_silent_segment_marked_empty(self, silence_wav):
        """Silent segments should be marked for deletion."""
        assert is_segment_empty(silence_wav) is True

    def test_audio_segment_not_empty(self, baby_cry_wav):
        """Segments with audio should not be deleted."""
        assert is_segment_empty(baby_cry_wav) is False

    def test_missing_file_is_empty(self, tmp_path):
        """Missing file should return True (empty)."""
        assert is_segment_empty(tmp_path / "missing.wav") is True

    def test_custom_threshold(self, silence_wav, baby_cry_wav):
        """Custom silence threshold should work."""
        # Very high threshold should mark everything as empty
        assert is_segment_empty(baby_cry_wav, silence_threshold=10.0) is True

        # Very low threshold should mark nothing as empty (except actual silence)
        assert is_segment_empty(silence_wav, silence_threshold=0.0) is False
