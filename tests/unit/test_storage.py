"""Unit tests for storage management."""

import time

from bbwatch.storage import (
    StorageManager,
    enforce_storage_limit,
    get_directory_size_bytes,
    get_directory_size_mb,
)


class TestDirectorySize:
    """Tests for directory size calculation."""

    def test_empty_directory(self, tmp_wav_dir):
        """Empty directory should return 0."""
        assert get_directory_size_bytes(tmp_wav_dir) == 0
        assert get_directory_size_mb(tmp_wav_dir) == 0.0

    def test_nonexistent_directory(self, tmp_path):
        """Nonexistent directory should return 0."""
        assert get_directory_size_bytes(tmp_path / "missing") == 0

    def test_counts_only_wav_files(self, tmp_wav_dir):
        """Should only count .wav files."""
        # Create wav file
        wav_file = tmp_wav_dir / "test.wav"
        wav_file.write_bytes(b"\x00" * 1000)

        # Create non-wav file
        txt_file = tmp_wav_dir / "test.txt"
        txt_file.write_bytes(b"\x00" * 2000)

        # Should only count wav file
        assert get_directory_size_bytes(tmp_wav_dir) == 1000

    def test_sums_multiple_files(self, tmp_wav_dir):
        """Should sum all wav files."""
        for i in range(5):
            (tmp_wav_dir / f"test_{i}.wav").write_bytes(b"\x00" * 1000)

        assert get_directory_size_bytes(tmp_wav_dir) == 5000


class TestEnforceStorageLimit:
    """Tests for storage limit enforcement."""

    def test_no_deletion_under_limit(self, tmp_wav_dir):
        """Files should not be deleted if under limit."""
        (tmp_wav_dir / "test.wav").write_bytes(b"\x00" * 1000)

        deleted = enforce_storage_limit(tmp_wav_dir, max_size_mb=1.0)

        assert deleted == 0
        assert (tmp_wav_dir / "test.wav").exists()

    def test_deletes_oldest_first(self, tmp_wav_dir):
        """Oldest files should be deleted first."""
        # Create files with different ages
        files = []
        for i in range(5):
            f = tmp_wav_dir / f"seg_{i:02d}.wav"
            f.write_bytes(b"\x00" * (512 * 1024))  # 512 KB each
            files.append(f)
            time.sleep(0.1)  # Ensure different mtimes

        # Total: 2.5 MB, limit: 2 MB
        deleted = enforce_storage_limit(tmp_wav_dir, max_size_mb=2.0)

        assert deleted >= 1  # At least one file deleted
        assert not files[0].exists()  # Oldest should be gone
        assert files[-1].exists()  # Newest should remain

    def test_targets_90_percent(self, tmp_wav_dir):
        """Should delete until reaching 90% of limit."""
        # Create files totaling ~2 MB
        for i in range(4):
            (tmp_wav_dir / f"seg_{i:02d}.wav").write_bytes(b"\x00" * (512 * 1024))

        # Limit: 1.5 MB, target: 1.35 MB
        enforce_storage_limit(tmp_wav_dir, max_size_mb=1.5, target_ratio=0.9)

        current_size = get_directory_size_mb(tmp_wav_dir)
        assert current_size <= 1.5 * 0.9

    def test_handles_empty_directory(self, tmp_wav_dir):
        """Should handle empty directory gracefully."""
        deleted = enforce_storage_limit(tmp_wav_dir, max_size_mb=1.0)
        assert deleted == 0


class TestStorageManager:
    """Tests for StorageManager class."""

    def test_context_manager(self, tmp_wav_dir):
        """Should work as context manager."""
        with StorageManager(tmp_wav_dir, max_size_mb=100.0) as manager:
            assert manager is not None

    def test_get_usage(self, tmp_wav_dir):
        """Should report current usage."""
        (tmp_wav_dir / "test.wav").write_bytes(b"\x00" * (1024 * 1024))  # 1 MB

        manager = StorageManager(tmp_wav_dir, max_size_mb=10.0)
        current, max_size = manager.get_usage()

        assert abs(current - 1.0) < 0.1
        assert max_size == 10.0

    def test_manual_enforce(self, tmp_wav_dir):
        """Manual enforce_limit should work."""
        # Create 2MB of files
        for i in range(4):
            (tmp_wav_dir / f"seg_{i:02d}.wav").write_bytes(b"\x00" * (512 * 1024))

        manager = StorageManager(tmp_wav_dir, max_size_mb=1.0)
        deleted = manager.enforce_limit()

        assert deleted >= 2
        assert get_directory_size_mb(tmp_wav_dir) <= 1.0
