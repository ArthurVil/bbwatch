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
from bbwatch.hardware_detector import HardwareDetector
from bbwatch.motion import MotionDetector
from bbwatch.notifier import make_notifier
from bbwatch.overlay import OverlayController
from bbwatch.overlay_generator import OverlayGenerator
from bbwatch.sources import (
    ALSASource,
    AudioSource,
    MockAudioSource,
    MockVideoSource,
    RPiCameraSource,
    RTSPAudioSource,
    RTSPVideoSource,
    V4L2Source,
    VideoSource,
)
from bbwatch.storage import StorageManager
from bbwatch.watchdog import StreamWatchdog

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
        self._watchdog: StreamWatchdog | None = None
        self._running = False

        # Detected hardware sources
        self._audio_source: AudioSource | None = None
        self._video_source: VideoSource | None = None

    def _detect_hardware(self) -> bool:
        """Detect and configure audio/video sources.

        Returns:
            True if at least one critical path is available (audio OR video).
        """
        detector = HardwareDetector(fake=self.config.fake_hardware)

        LOGGER.info("=" * 50)
        LOGGER.info("HARDWARE DETECTION")
        LOGGER.info("=" * 50)

        # Discover all available sources
        audio_sources = self._discover_audio_sources(detector)
        video_sources = self._discover_video_sources(detector)

        # Select best available source for each
        self._audio_source = self._select_source(audio_sources, "Audio")
        self._video_source = self._select_source(video_sources, "Motion Detection")

        LOGGER.info("=" * 50)
        LOGGER.info(f"Cry Detection:     {'ENABLED' if self._audio_source else 'DISABLED'}")
        LOGGER.info(f"Motion Detection:  {'ENABLED' if self._video_source else 'DISABLED'}")
        LOGGER.info("=" * 50)

        # Return True if at least one critical path is available
        return self._audio_source is not None or self._video_source is not None

    def _discover_audio_sources(self, detector: HardwareDetector) -> list[AudioSource]:
        """Build list of potential audio sources, in priority order."""
        if self.config.fake_hardware:
            return [MockAudioSource("Audio")]

        sources: list[AudioSource] = []

        # Option 1: Docker network RTSP (highest priority in Docker mode)
        if isinstance(self.config.audio.device_index, str) and self.config.audio.device_index.startswith("rtsp://"):
            sources.append(RTSPAudioSource(self.config.audio.device_index))

        # Option 2: Local ALSA devices
        for device in detector.detect_audio_devices():
            sources.append(ALSASource(device.alsa_id))

        # No mock fallback outside --fake-hardware: a monitor that silently
        # pretends to listen is worse than one that refuses to start.
        return sources

    def _discover_video_sources(self, detector: HardwareDetector) -> list[VideoSource]:
        """Build list of potential video sources, in priority order."""
        if self.config.fake_hardware:
            return [MockVideoSource("Video")]

        sources: list[VideoSource] = []

        # Option 1: Pi Camera via RTSP restream (go2rtc holds the hardware lock)
        for device in detector.detect_picamera_devices():
            sources.append(RPiCameraSource(device.path))

        # Option 2: V4L2 USB cameras
        for device in detector.detect_video_devices():
            sources.append(V4L2Source(device.path))

        # Option 3: RTSP restream. Explicit motion.stream_url wins; otherwise
        # derive the host from an RTSP audio URL (Docker mode). The explicit
        # form is required for camera-only deployments where audio is disabled.
        if self.config.motion.stream_url:
            sources.append(RTSPVideoSource(self.config.motion.stream_url))
        elif isinstance(self.config.audio.device_index, str) and self.config.audio.device_index.startswith("rtsp://"):
            raw_video_url = self.config.audio.device_index.rsplit("/", 1)[0] + "/raw_video"
            sources.append(RTSPVideoSource(raw_video_url))

        # No mock fallback outside --fake-hardware (see _discover_audio_sources).
        return sources

    @staticmethod
    def _select_source(sources: list[AudioSource | VideoSource], name: str) -> AudioSource | VideoSource | None:
        """Pick first available source from list.

        Args:
            sources: List of sources to try, in priority order.
            name: Human-readable name for logging.

        Returns:
            First available source, or None if no sources available.
        """
        for source in sources:
            if source.is_available():
                LOGGER.info(f"{name}: {source}")
                return source

        LOGGER.warning(f"{name}: NO SOURCES AVAILABLE")
        return None

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
        if self._video_source:
            # Motion Detector
            LOGGER.info(f"Initializing motion detector on {self._video_source}")
            self._motion_detector = MotionDetector(
                device_index=self._video_source.open(),
                threshold=self.config.motion.threshold,
                blur_size=self.config.motion.blur_size,
                history_len=self.config.motion.history_len,
                motion_threshold_percent=self.config.motion.motion_threshold_percent,
                dilation_iterations=self.config.motion.dilation_iterations,
                fps=self.config.motion.fps,
                zoom=self.config.motion.zoom,
                offset_x=self.config.motion.offset_x,
                offset_y=self.config.motion.offset_y,
                process_width=self.config.motion.process_width,
                latency_report_interval_s=self.config.latency_report_interval_s,
                equalize_luminosity=self.config.motion.equalize_luminosity,
            )
            # zoom/offset/process_width have no effect on the video anyone
            # watches — only on the ROI MotionDetector analyzes internally.
            # Log the resolved values explicitly so a config change (or the
            # lack of one) is verifiable without reading source or attaching
            # a debugger.
            if self.config.motion.zoom > 1.0:
                LOGGER.info(
                    f"Motion ROI: zoom={self.config.motion.zoom} offset_x={self.config.motion.offset_x} "
                    f"offset_y={self.config.motion.offset_y} process_width={self.config.motion.process_width} "
                    "(crops the analysis region only — does not change the streamed video)"
                )
            else:
                LOGGER.info("Motion ROI: zoom=1.0 (full frame, no crop)")

        # Overlay Generator
        overlay_pipe = self.config.data_dir / "overlays/overlay.pipe"
        if self.config.alerts.enable_dynamic_overlay:
            LOGGER.info(f"Initializing dynamic overlay generator (pipe={overlay_pipe})")
            self._overlay_generator = OverlayGenerator(
                pipe_path=overlay_pipe,
                width=self.config.alerts.overlay_width,
                height=self.config.alerts.overlay_height,
                fps=self.config.alerts.overlay_fps,
                history_len=self.config.motion.history_len,
                latency_report_interval_s=self.config.latency_report_interval_s,
                drop_stale_frames=self.config.alerts.overlay_drop_stale_frames,
            )
            self._overlay_generator.start()
        else:
            LOGGER.info("Dynamic overlay disabled by config")

        # Start motion detector if video device available
        if self._motion_detector:
            self._motion_detector.start()

        # Audio pipeline (file watcher + capture) — only with a real audio source.
        # _detect_hardware() allows video-only operation; make that explicit
        # and loud rather than crashing or pretending to listen.
        if self._audio_source is not None:
            handler = SegmentHandler(
                config=self.config.detection,
                alert_manager=self._alert_manager,
                delete_empty=self.config.storage.delete_empty_segments,
                overlay_generator=self._overlay_generator,
                latency_report_interval_s=self.config.latency_report_interval_s,
            )

            self._observer = Observer()
            self._observer.schedule(
                handler,
                str(self.config.storage.wav_dir),
                recursive=False,
            )
            self._observer.start()

            self._capture = SlidingWindowCapture(
                device=self._audio_source.open(),
                output_dir=self.config.storage.wav_dir,
                segment_duration=self.config.audio.segment_duration_s,
                overlap=self.config.audio.overlap_s,
                sample_rate=self.config.audio.sample_rate,
                channels=self.config.audio.channels,
            )
            self._capture.start()
        else:
            LOGGER.error("No audio source available — CRY DETECTION IS DISABLED for this run")

        if self.config.watchdog.enabled:
            self._watchdog = StreamWatchdog(self.config.watchdog, make_notifier(self.config.watchdog))
            self._watchdog.start()

        self._running = True
        LOGGER.info("Baby monitor started successfully")

    def stop(self) -> None:
        """Stop all monitor components gracefully."""
        LOGGER.info("Stopping baby monitor...")

        if self._watchdog:
            self._watchdog.stop()
            self._watchdog = None

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

            last_health_check = time.time()
            last_heartbeat = 0.0
            while self._running:
                # Heartbeat status.json so liveness reflects the process being
                # alive, not audio activity specifically — otherwise a
                # camera-only deployment (no audio source) reads as
                # permanently unhealthy once health_timeout_s elapses.
                # Throttled: this is a disk write, not free at 20Hz.
                now_hb = time.time()
                if self._alert_manager is not None and now_hb - last_heartbeat >= 1.0:
                    last_heartbeat = now_hb
                    self._alert_manager.heartbeat()

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
                    latency_ms = None
                    if self._alert_manager:
                        audio_level = self._alert_manager.current_intensity
                        audio_alert = self._alert_manager.alert_active
                        latency_ms = self._alert_manager.last_latency_ms

                    # Update overlay
                    self._overlay_generator.update_state(
                        motion_detected=motion_detected,
                        audio_alert=audio_alert,
                        motion_level=motion_level,
                        audio_level=audio_level,
                        latency_ms=latency_ms,
                    )

                # Component health check (throttled): a dead capture thread
                # must be reported, never silently served as stale data.
                now = time.time()
                if now - last_health_check >= 10.0:
                    last_health_check = now
                    if self._motion_detector and not self._motion_detector.is_healthy():
                        LOGGER.error(
                            "Motion detector UNHEALTHY: capture thread dead or no frame "
                            f"for {self._motion_detector.last_frame_age_s()} s"
                        )
                    if self._capture and not self._capture.is_running():
                        LOGGER.error("Audio capture UNHEALTHY: FFmpeg not running — cry detection is down")

                time.sleep(0.05)  # 20Hz — responsive overlay state updates

        except KeyboardInterrupt:
            LOGGER.info("Received interrupt signal")
        finally:
            self.stop()


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
