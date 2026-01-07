"""Disk storage management for audio segments.

Ensures disk usage stays within configured limits by automatically
deleting oldest segments when necessary.
"""

import logging
import threading
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def get_directory_size_bytes(path: Path) -> int:
    """Get total size of .wav files in directory.

    Args:
        path: Directory to measure.

    Returns:
        Total size in bytes.
    """
    if not path.exists():
        return 0

    total = 0
    for f in path.glob("*.wav"):
        try:
            total += f.stat().st_size
        except OSError:
            # File may have been deleted
            continue

    return total


def get_directory_size_mb(path: Path) -> float:
    """Get total size of .wav files in directory in MB.

    Args:
        path: Directory to measure.

    Returns:
        Total size in megabytes.
    """
    return get_directory_size_bytes(path) / (1024 * 1024)


def enforce_storage_limit(
    wav_dir: Path,
    max_size_mb: float = 1024.0,
    target_ratio: float = 0.9,
) -> int:
    """Delete oldest segments to stay under storage limit.

    Implements a high-water-mark strategy: when limit is exceeded,
    delete until we're at target_ratio of the limit to avoid
    frequent small deletions.

    Args:
        wav_dir: Directory containing .wav segments.
        max_size_mb: Maximum allowed size in MB.
        target_ratio: Target ratio of max_size to achieve after cleanup.

    Returns:
        Number of files deleted.
    """
    current_size_mb = get_directory_size_mb(wav_dir)

    if current_size_mb <= max_size_mb:
        return 0

    target_size_mb = max_size_mb * target_ratio
    deleted = 0

    # Sort by modification time (oldest first)
    try:
        wav_files = sorted(
            wav_dir.glob("*.wav"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError as e:
        LOGGER.error(f"Failed to list wav files: {e}")
        return 0

    for wav_file in wav_files:
        if current_size_mb <= target_size_mb:
            break

        try:
            file_size_mb = wav_file.stat().st_size / (1024 * 1024)
            wav_file.unlink()
            current_size_mb -= file_size_mb
            deleted += 1
            LOGGER.debug(f"Deleted old segment: {wav_file.name}")
        except OSError as e:
            LOGGER.warning(f"Failed to delete {wav_file.name}: {e}")

    if deleted > 0:
        LOGGER.info(f"Storage cleanup: deleted {deleted} files, now at {current_size_mb:.1f} MB")

    return deleted


class StorageManager:
    """Manages disk storage for audio segments.

    Runs periodic cleanup to ensure disk usage stays within limits.
    Thread-safe for use with the main application.
    """

    def __init__(
        self,
        wav_dir: Path,
        max_size_mb: float = 1024.0,
        check_interval_s: float = 60.0,
    ) -> None:
        """Initialize the storage manager.

        Args:
            wav_dir: Directory containing .wav segments.
            max_size_mb: Maximum allowed size in MB.
            check_interval_s: Interval between cleanup checks.
        """
        self.wav_dir = Path(wav_dir)
        self.max_size_mb = max_size_mb
        self.check_interval_s = check_interval_s

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background cleanup thread."""
        if self._thread is not None and self._thread.is_alive():
            LOGGER.warning("StorageManager already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._cleanup_loop,
            name="StorageManager",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info(f"StorageManager started: max={self.max_size_mb}MB, interval={self.check_interval_s}s")

    def stop(self) -> None:
        """Stop the background cleanup thread."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        LOGGER.info("StorageManager stopped")

    def enforce_limit(self) -> int:
        """Manually trigger storage limit enforcement.

        Returns:
            Number of files deleted.
        """
        return enforce_storage_limit(self.wav_dir, self.max_size_mb)

    def get_usage(self) -> tuple[float, float]:
        """Get current storage usage.

        Returns:
            Tuple of (current_size_mb, max_size_mb).
        """
        return get_directory_size_mb(self.wav_dir), self.max_size_mb

    def _cleanup_loop(self) -> None:
        """Background loop for periodic cleanup."""
        while not self._stop_event.wait(self.check_interval_s):
            try:
                enforce_storage_limit(self.wav_dir, self.max_size_mb)
            except Exception as e:
                LOGGER.error(f"Storage cleanup failed: {e}")

    def __enter__(self) -> "StorageManager":
        """Context manager entry."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        """Context manager exit."""
        self.stop()
