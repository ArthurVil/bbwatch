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
from bbwatch.motion import MotionDetector
from bbwatch.overlay import OverlayController
from bbwatch.overlay_generator import OverlayGenerator
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
        self._overlay_controller: OverlayController | None = None
        self._overlay_generator: OverlayGenerator | None = None
        self._motion_detector: MotionDetector | None = None
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
            return True

        # Check for network stream (RTSP/HTTP)
        network_audio = isinstance(self.config.audio.device_index, str) and "://" in self.config.audio.device_index
        if network_audio:
            LOGGER.info(f"Using network audio stream: {self.config.audio.device_index}")
            self._audio_device = self.config.audio.device_index
            # Use raw_video for motion detection to avoid seeing the overlay
            # We skip local hardware discovery to avoid v4l2-ctl errors when go2rtc holds the device
            self._video_device = self.config.audio.device_index.replace("babycam", "raw_video")
            LOGGER.info(f"Using network video for motion: {self._video_device}")
            return True

        # Local hardware mode
        from bbwatch.hardware import get_preferred_audio_device, get_preferred_video_device, log_detected_hardware

        audio_devices, video_devices = log_detected_hardware()

        # Get preferred audio device
        audio = get_preferred_audio_device(audio_devices)
        if audio is None:
            LOGGER.error("No audio capture device found!")
            return False

        self._audio_device = audio.alsa_id
        LOGGER.info(f"Using audio device: {audio}")

        # Get preferred video device
        video = get_preferred_video_device(video_devices)
        if video is None:
            LOGGER.warning("No local video device found - motion detection will be disabled")
        else:
            # If rpicam device, motion detection must use RTSP restream from go2rtc
            if video.path.startswith("rpicam:"):
                self._video_device = "rtsp://localhost:8554/raw_video"
                LOGGER.info(f"Using Pi Camera via go2rtc RTSP restream for motion: {video}")
            else:
                self._video_device = video.path
                LOGGER.info(f"Using video device for motion: {video}")

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

        # Legacy Overlay Controller (for static images if dynamic disabled, or just setup)
        # Keeps compatibility with status file updates
        self._overlay_controller = OverlayController(
            overlay_dir=self.config.alerts.overlay_dir,
            status_file=self.config.alerts.status_file,
            health_timeout_s=self.config.alerts.health_timeout_s,
        )
        self._overlay_controller.setup()

        # New Dynamic Components
        if self._video_device:
            # Motion Detector
            LOGGER.info(f"Initializing motion detector on {self._video_device}")
            self._motion_detector = MotionDetector(
                device_index=self._video_device,
                threshold=self.config.motion.threshold,
                blur_size=self.config.motion.blur_size,
                history_len=self.config.motion.history_len,
                motion_threshold_percent=self.config.motion.motion_threshold_percent,
                dilation_iterations=self.config.motion.dilation_iterations,
                fps=self.config.motion.fps,
            )

        # Overlay Generator
        overlay_pipe = self.config.data_dir / "overlays/overlay.pipe"
        if self.config.alerts.enable_dynamic_overlay:
            LOGGER.info(f"Initializing dynamic overlay generator (pipe={overlay_pipe})")
            self._overlay_generator = OverlayGenerator(
                pipe_path=overlay_pipe, fps=self.config.alerts.overlay_fps, history_len=self.config.motion.history_len
            )
            self._overlay_generator.start()
        else:
            LOGGER.info("Dynamic overlay disabled by config")

        # Start motion detector if video device available
        if self._motion_detector:
            self._motion_detector.start()

        # Start file watcher for detection
        handler = SegmentHandler(
            config=self.config.detection,
            alert_manager=self._alert_manager,
            delete_empty=self.config.storage.delete_empty_segments,
            overlay_generator=self._overlay_generator,
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

        if self._overlay_generator:
            LOGGER.info("Stopping overlay generator...")
            self._overlay_generator.stop()
            self._overlay_generator = None

        if self._motion_detector:
            LOGGER.info("Stopping motion detector...")
            self._motion_detector.stop()
            self._motion_detector = None

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
                # Update legacy overlay logic (file linking)
                if self._overlay_controller is not None:
                    self._overlay_controller.update()

                # --- Update Dynamic Data ---
                if self._overlay_generator and self._running:
                    # Get motion level
                    motion_level = 0.0
                    motion_detected = False
                    if self._motion_detector:
                        motion_level = self._motion_detector.get_current_motion()
                        motion_detected = motion_level > self.config.motion.motion_threshold_percent

                    # Get audio level and alert status
                    audio_level = 0.0
                    audio_alert = False
                    if self._alert_manager:
                        audio_level = self._alert_manager.current_intensity
                        audio_alert = self._alert_manager.alert_active

                    # Update overlay
                    self._overlay_generator.update_state(
                        motion_detected=motion_detected,
                        audio_alert=audio_alert,
                        motion_level=motion_level,
                        audio_level=audio_level,
                    )

                # Log periodic status
                if self._storage is not None:
                    current, max_size = self._storage.get_usage()
                    # LOGGER.debug(f"Storage: {current:.1f}/{max_size:.1f} MB ({current / max_size * 100:.1f}%)")

                time.sleep(0.1)  # Main loop tick (10Hz for responsive overlay updates)

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
