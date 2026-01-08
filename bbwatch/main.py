"""Main entry point for bbwatch baby monitor."""

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from bbwatch import __version__
from bbwatch.alert import AlertManager
from bbwatch.capture import SlidingWindowCapture
from bbwatch.config import BBWatchConfig, get_default_config_path
from bbwatch.detector import SegmentHandler
from bbwatch.hardware import (
    get_preferred_audio_device,
    get_preferred_video_device,
    log_detected_hardware,
)
from bbwatch.overlay import OverlayController
from bbwatch.storage import StorageManager

LOGGER = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    """Configure logging for bbwatch.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR).
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


class BabyMonitor:
    """Main baby monitor application.

    Coordinates all components: audio capture, detection,
    storage management, and overlay updates.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        """Initialize the baby monitor.

        Args:
            config_path: Path to config.yaml. Uses default if None.
        """
        # Load configuration
        if config_path is None:
            config_path = get_default_config_path()

        self.config = BBWatchConfig.from_yaml(Path(config_path))
        self.config = self.config.resolve_paths()
        self.config.ensure_directories()

        setup_logging(self.config.log_level)

        LOGGER.info(f"bbwatch v{__version__} starting")
        LOGGER.info(f"Data directory: {self.config.data_dir}")

        # Component references (initialized in start())
        self._capture: SlidingWindowCapture | None = None
        self._storage: StorageManager | None = None
        self._observer: BaseObserver | None = None
        self._overlay: OverlayController | None = None
        self._alert_manager: AlertManager | None = None
        self._running = False

        # Detected hardware
        self._audio_device: str | None = None
        self._video_device: str | None = None

    def _detect_hardware(self) -> bool:
        """Detect and validate hardware.

        Returns:
            True if required hardware was found.
        """
        if self.config.fake_hardware:
            LOGGER.info("Using fake hardware (development mode)")
            self._audio_device = "hw:0,0"
            self._video_device = "/dev/video0"
            self._video_device = "/dev/video0"
            return True

        # Check for network stream (RTSP/HTTP)
        if "://" in self.config.audio.device_index:
            LOGGER.info(f"Using network stream: {self.config.audio.device_index}")
            self._audio_device = self.config.audio.device_index
            # Network mode doesn't need local video device
            return True

        audio_devices, video_devices = log_detected_hardware()

        # Get preferred devices
        audio = get_preferred_audio_device(audio_devices)
        video = get_preferred_video_device(video_devices)

        if audio is None:
            LOGGER.error("No audio capture device found!")
            return False

        self._audio_device = audio.alsa_id
        LOGGER.info(f"Using audio device: {audio}")

        if video is None:
            LOGGER.warning("No video device found - video streaming will not work")
        else:
            self._video_device = video.path
            LOGGER.info(f"Using video device: {video}")

        return True

    def start(self) -> None:
        """Start all monitor components."""
        if self._running:
            raise RuntimeError("Monitor already running")

        # Detect hardware
        if not self._detect_hardware():
            raise RuntimeError("Required hardware not found")

        # Start storage manager
        self._storage = StorageManager(
            wav_dir=self.config.storage.wav_dir,
            max_size_mb=self.config.storage.max_size_mb,
            check_interval_s=self.config.storage.cleanup_interval_s,
        )
        self._storage.start()

        # Setup alert manager
        self._alert_manager = AlertManager(self.config.alerts)

        # Setup overlay controller
        self._overlay = OverlayController(
            overlay_dir=self.config.alerts.overlay_dir,
            status_file=self.config.alerts.status_file,
            health_timeout_s=self.config.alerts.health_timeout_s,
        )
        self._overlay.setup()
        self._overlay.update()  # Create initial overlay to unblock go2rtc

        # Start file watcher for detection
        handler = SegmentHandler(
            config=self.config.detection,
            alert_manager=self._alert_manager,
            delete_empty=self.config.storage.delete_empty_segments,
        )

        self._observer = Observer()
        self._observer.schedule(
            handler,
            str(self.config.storage.wav_dir),
            recursive=False,
        )
        self._observer.start()

        if self._audio_device is None:
            raise RuntimeError("Audio device not initialized")

        # Start audio capture
        self._capture = SlidingWindowCapture(
            device=self._audio_device,
            output_dir=self.config.storage.wav_dir,
            segment_duration=self.config.audio.segment_duration_s,
            overlap=self.config.audio.overlap_s,
            sample_rate=self.config.audio.sample_rate,
            channels=self.config.audio.channels,
        )
        self._capture.start()

        self._running = True
        LOGGER.info("Baby monitor started successfully")

    def stop(self) -> None:
        """Stop all monitor components gracefully."""
        LOGGER.info("Stopping baby monitor...")

        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception as e:
                LOGGER.error(f"Error stopping capture: {e}")
            self._capture = None

        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5.0)
            except Exception as e:
                LOGGER.error(f"Error stopping observer: {e}")
            self._observer = None

        if self._storage is not None:
            try:
                self._storage.stop()
            except Exception as e:
                LOGGER.error(f"Error stopping storage: {e}")
            self._storage = None

        self._running = False
        LOGGER.info("Baby monitor stopped")

    def run(self) -> None:
        """Run the main monitoring loop."""
        try:
            self.start()

            while self._running:
                # Update overlay based on current status
                if self._overlay is not None:
                    self._overlay.update()

                # Log periodic status
                if self._storage is not None:
                    current, max_size = self._storage.get_usage()
                    LOGGER.debug(f"Storage: {current:.1f}/{max_size:.1f} MB ({current / max_size * 100:.1f}%)")

                time.sleep(self._overlay.poll_interval_s if self._overlay else 1.0)

        except KeyboardInterrupt:
            LOGGER.info("Received interrupt signal")
        finally:
            self.stop()

    def _check_directories(self) -> None:
        """Verify and recreate directories if needed."""
        if not self.config.storage.wav_dir.exists():
            LOGGER.warning("wav_dir missing, recreating")
            self.config.storage.wav_dir.mkdir(parents=True, exist_ok=True)

        if not self.config.alerts.overlay_dir.exists():
            LOGGER.warning("overlay_dir missing, recreating")
            self.config.alerts.overlay_dir.mkdir(parents=True, exist_ok=True)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns:
        Parsed arguments namespace.
    """
    parser = argparse.ArgumentParser(
        prog="bbwatch",
        description="FOSS Baby Monitor with cry detection",
    )

    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml",
    )

    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"bbwatch {__version__}",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    parser.add_argument(
        "--fake-hardware",
        action="store_true",
        help="Use fake hardware for testing",
    )

    return parser.parse_args()


def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 for success).
    """
    args = parse_args()

    if args.debug:
        setup_logging("DEBUG")
    else:
        setup_logging("INFO")

    # Create monitor
    monitor = BabyMonitor(config_path=args.config)

    if args.fake_hardware:
        monitor.config.fake_hardware = True

    # Setup signal handlers
    def signal_handler(signum: int, frame: object) -> None:
        LOGGER.info(f"Received signal {signum}")
        monitor.stop()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Run
    try:
        monitor.run()
        return 0
    except Exception as e:
        LOGGER.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
