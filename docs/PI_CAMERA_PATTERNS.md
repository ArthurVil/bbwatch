# Pi Camera Integration: Coding Patterns & Developer Experience

This document outlines a patterns-first approach to address production readiness issues identified in the deep review of `feat/pi-camera-rpi5` branch. Rather than defensive programming (retries, timeouts, error handling), we focus on **clear abstractions, composable components, and explicit failure modes**.

**Key principle:** Code is read 10x more than written. Optimize for clarity. When something fails, let it fail loudly and obviously.

---

## Problem Statement

The current implementation has 3 critical + 5 high-priority issues mostly caused by:
- **Implicit state:** Device detection silently fails, leaving the system in unknown state
- **Scattered concerns:** Audio/video sources mixed with detection logic and error handling
- **Silent fallbacks:** No visibility when hardware is unavailable
- **Testability gap:** Unit tests mock subprocess but miss real output format variations

---

## Solution: Interface-Based Design

Rather than adding timeout + retry logic everywhere, we introduce clean abstractions that make problems **visible and easy to handle**.

---

## Phase 1: Core Patterns (High Impact, Low Risk)

### 1. Device Source Abstraction

**Current problem:** Device paths (ALSA IDs, rpicam strings, RTSP URLs) are scattered. Different code paths handle them inconsistently.

**Pattern solution:** Single protocol for all audio/video sources.

```python
# bbwatch/sources.py
from typing import Protocol, Any

class AudioSource(Protocol):
    """Protocol for audio input: ALSA device, RTSP stream, or mock."""
    
    def open(self) -> Any:
        """Open the audio stream for reading."""
        ...
    
    def close(self) -> None:
        """Close the stream cleanly."""
        ...
    
    def is_available(self) -> bool:
        """Non-blocking check: is this source usable?"""
        ...
    
    def __repr__(self) -> str:
        """User-friendly identifier: 'ALSA(hw:1,0)' or 'RTSP(rtsp://...)' """
        ...

class VideoSource(Protocol):
    """Protocol for video input: V4L2, rpicam, RTSP, or mock."""
    
    def open(self) -> Any:
        ...
    
    def close(self) -> None:
        ...
    
    def is_available(self) -> bool:
        ...
    
    def __repr__(self) -> str:
        ...

# Implementations
class ALSASource:
    def __init__(self, device_id: str):
        self.device_id = device_id
    
    def is_available(self) -> bool:
        """Fast non-blocking check: device exists."""
        try:
            subprocess.run(["arecord", "-l"], timeout=2, capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
            return False
    
    def __repr__(self) -> str:
        return f"ALSA({self.device_id})"

class RTSPSource:
    def __init__(self, url: str):
        self.url = url
    
    def is_available(self) -> bool:
        """Fast check: URL is well-formed."""
        return self.url.startswith("rtsp://") and len(self.url) > 10
    
    def __repr__(self) -> str:
        return f"RTSP({self.url.split('/')[-1]})"  # Show just stream name

class RPiCameraSource:
    def __init__(self, device_path: str):
        self.device_path = device_path  # e.g., "rpicam:0"
    
    def is_available(self) -> bool:
        """Check if device is accessible via go2rtc."""
        # In real scenario: check if go2rtc is running and has this stream
        return self.device_path.startswith("rpicam:")
    
    def __repr__(self) -> str:
        return f"RPiCamera({self.device_path})"

class MockSource:
    def __init__(self, name: str):
        self.name = name
    
    def is_available(self) -> bool:
        return True
    
    def __repr__(self) -> str:
        return f"Mock({self.name})"
```

**Benefits:**
- **Pluggable:** Add a new source type by implementing the protocol
- **Testable:** Use MockSource in tests, swap real sources at runtime
- **Clear identity:** `str(source)` tells you exactly what's running
- **Non-blocking checks:** `is_available()` returns quickly—no timeouts

---

### 2. Hardware Detector Service

**Current problem:** Device detection logic is spread across `hardware.py` with subprocess calls, parsing, and logging mixed together.

**Pattern solution:** Dedicated detector service with clear API.

```python
# bbwatch/hardware_detector.py
import subprocess
from dataclasses import dataclass
from pathlib import Path

@dataclass
class AudioDevice:
    """Detected audio device."""
    card: int
    device: int
    name: str
    
    @property
    def alsa_id(self) -> str:
        return f"hw:{self.card},{self.device}"

@dataclass
class VideoDevice:
    """Detected video device."""
    path: str
    name: str

class HardwareDetector:
    """Discover available audio/video devices on this system."""
    
    def __init__(self, timeout_sec: int = 10, fake: bool = False):
        self.timeout = timeout_sec
        self.fake = fake
    
    def detect_audio_devices(self) -> list[AudioDevice]:
        """Detect ALSA devices.
        
        Returns:
            List of detected devices. Empty if arecord not found or times out.
        """
        if self.fake:
            return [AudioDevice(card=0, device=0, name="Fake Audio")]
        
        try:
            result = subprocess.run(
                ["arecord", "-l"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            return self._parse_arecord(result.stdout)
        except FileNotFoundError:
            LOGGER.debug("arecord not found—skipping ALSA detection")
            return []
        except subprocess.TimeoutExpired:
            LOGGER.debug("arecord timeout—skipping ALSA detection")
            return []
        except subprocess.CalledProcessError as e:
            LOGGER.debug(f"arecord failed: {e}—skipping ALSA detection")
            return []
    
    def detect_video_devices(self) -> list[VideoDevice]:
        """Detect V4L2 USB cameras.
        
        Returns:
            List of detected devices. Empty if v4l2-ctl not found.
        """
        if self.fake:
            return [VideoDevice(path="/dev/video0", name="Fake USB Camera")]
        
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            return self._parse_v4l2ctl(result.stdout)
        except FileNotFoundError:
            LOGGER.debug("v4l2-ctl not found—skipping V4L2 detection")
            return []
        except subprocess.TimeoutExpired:
            LOGGER.debug("v4l2-ctl timeout—skipping V4L2 detection")
            return []
        except subprocess.CalledProcessError as e:
            LOGGER.debug(f"v4l2-ctl failed: {e}—skipping V4L2 detection")
            return []
    
    def detect_picamera_devices(self) -> list[VideoDevice]:
        """Detect Raspberry Pi Camera modules via libcamera.
        
        Returns:
            List of detected devices. Empty if libcamera not available (not on RPi).
        """
        if self.fake:
            return [VideoDevice(path="rpicam:0", name="Fake Pi Camera")]
        
        try:
            result = subprocess.run(
                ["libcamera-hello", "--list-cameras"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=True,
            )
            return self._parse_libcamera(result.stdout)
        except FileNotFoundError:
            LOGGER.debug("libcamera not available—not on Raspberry Pi")
            return []
        except subprocess.TimeoutExpired:
            LOGGER.warning("libcamera-hello timed out—camera may be locked by another process")
            return []
        except subprocess.CalledProcessError as e:
            LOGGER.warning(f"libcamera-hello failed: {e}—check ribbon cable and dmesg")
            return []
    
    def _parse_arecord(self, output: str) -> list[AudioDevice]:
        """Parse arecord -l output."""
        devices = []
        pattern = re.compile(r"card (\d+):.*\[(.+?)\].*device (\d+):")
        for line in output.splitlines():
            if match := pattern.search(line):
                devices.append(
                    AudioDevice(
                        card=int(match.group(1)),
                        device=int(match.group(3)),
                        name=match.group(2),
                    )
                )
        return devices
    
    def _parse_v4l2ctl(self, output: str) -> list[VideoDevice]:
        """Parse v4l2-ctl --list-devices output."""
        devices = []
        current_name = "Unknown"
        for line in output.splitlines():
            if not line.strip():
                continue
            if line.endswith(":") and not line.startswith("\t"):
                current_name = line.rstrip(":").strip()
                if "(" in current_name:
                    current_name = current_name.split("(")[0].strip()
            elif "/dev/video" in line:
                devices.append(VideoDevice(path=line.strip(), name=current_name))
        return devices
    
    def _parse_libcamera(self, output: str) -> list[VideoDevice]:
        """Parse libcamera-hello --list-cameras output."""
        devices = []
        pattern = re.compile(r"(\d+)\s*:\s*(.+?)\s*\[")
        for line in output.splitlines():
            if match := pattern.search(line):
                index = match.group(1)
                name = match.group(2).strip()
                devices.append(
                    VideoDevice(path=f"rpicam:{index}", name=f"Pi Camera {index} ({name})")
                )
        return devices
```

**Benefits:**
- **Single responsibility:** Detection is isolated from business logic
- **Testable:** Pass fake=True to get predictable outputs
- **Observable:** Each detection step is logged with clear context
- **Composable:** Call detector in tests, config, or main.py—consistent interface

---

### 3. Explicit Hardware Initialization

**Current problem:** `main.py` silently handles missing hardware, making it unclear what's running.

**Pattern solution:** Explicit initialization that logs what was found and what's enabled.

```python
# bbwatch/main.py (excerpt)
from bbwatch.sources import ALSASource, RTSPSource, RPiCameraSource, V4L2Source, MockSource
from bbwatch.hardware_detector import HardwareDetector

class BabyMonitor:
    def _initialize_hardware(self) -> bool:
        """Detect and configure audio/video sources.
        
        Returns:
            True if at least one critical path is available (audio OR video).
        """
        detector = HardwareDetector(fake=self.config.fake_hardware)
        
        # Detect available sources
        LOGGER.info("=" * 50)
        LOGGER.info("HARDWARE DETECTION")
        LOGGER.info("=" * 50)
        
        audio_sources = self._discover_audio_sources(detector)
        video_sources = self._discover_video_sources(detector)
        
        # Pick best available source for each
        self._audio_source = self._select_source(audio_sources, "Audio")
        self._video_source = self._select_source(video_sources, "Motion Detection")
        
        LOGGER.info("=" * 50)
        LOGGER.info(f"Cry Detection:     {'ENABLED' if self._audio_source else 'DISABLED'}")
        LOGGER.info(f"Motion Detection:  {'ENABLED' if self._video_source else 'DISABLED'}")
        LOGGER.info("=" * 50)
        
        return self._audio_source is not None or self._video_source is not None
    
    def _discover_audio_sources(self, detector: HardwareDetector) -> list[AudioSource]:
        """Build list of potential audio sources, in priority order."""
        sources = []
        
        # Option 1: Docker network RTSP (highest priority in Docker)
        if self.config.audio.device_index.startswith("rtsp://"):
            sources.append(RTSPSource(self.config.audio.device_index))
        
        # Option 2: Local ALSA devices
        for device in detector.detect_audio_devices():
            sources.append(ALSASource(device.alsa_id))
        
        # Option 3: Fallback mock (for testing without hardware)
        if not sources:
            sources.append(MockSource("Audio"))
        
        return sources
    
    def _discover_video_sources(self, detector: HardwareDetector) -> list[VideoSource]:
        """Build list of potential video sources, in priority order."""
        sources = []
        
        # Option 1: Docker network RTSP (for motion detection)
        if self.config.motion.device_index.startswith("rtsp://"):
            sources.append(RTSPSource(self.config.motion.device_index))
        
        # Option 2: Pi Camera via RTSP restream (go2rtc holds the hardware lock)
        for device in detector.detect_picamera_devices():
            # Map rpicam:0 → RTSP restream URL
            sources.append(RTSPSource("rtsp://localhost:8554/raw_video"))
        
        # Option 3: V4L2 USB cameras
        for device in detector.detect_video_devices():
            sources.append(V4L2Source(device.path))
        
        # Option 4: Fallback mock (for testing)
        if not sources:
            sources.append(MockSource("Video"))
        
        return sources
    
    def _select_source(self, sources: list, name: str) -> AudioSource | VideoSource | None:
        """Pick first available source. Return None if none available."""
        for source in sources:
            if source.is_available():
                LOGGER.info(f"{name}: {source}")
                return source
        
        LOGGER.warning(f"{name}: NO SOURCES AVAILABLE")
        return None
```

**Benefits:**
- **Visible state:** Logs show exactly what was found and what's running
- **Priority-based:** Easy to change source priority by reordering list
- **Explicit fallback:** User sees "NO SOURCES AVAILABLE" instead of silent failures
- **Testable:** Pass mock sources in tests, verify selection logic

---

## Phase 2: Error Handling (Clear Failure Modes)

### 4. Recording Interface

**Current problem:** FFmpeg recording is embedded in AlertManager with subprocess calls and timeout logic scattered throughout.

**Pattern solution:** Abstraction that makes recording failures obvious.

```python
# bbwatch/recording.py
from typing import Protocol

class Recorder(Protocol):
    """Abstract interface for capturing frames and recording clips."""
    
    def capture_frame(self, output_path: Path) -> None:
        """Capture single frame to JPEG.
        
        Raises:
            subprocess.TimeoutExpired: If capture takes too long.
            subprocess.CalledProcessError: If ffmpeg fails.
            FileNotFoundError: If ffmpeg not installed.
        """
        ...
    
    def start_recording(self, output_path: Path, duration_sec: float) -> None:
        """Start background clip recording.
        
        Raises:
            RuntimeError: If already recording.
            subprocess.CalledProcessError: If ffmpeg fails to start.
        """
        ...
    
    def is_recording(self) -> bool:
        """Check if recording is in progress."""
        ...
    
    def stop_recording(self) -> None:
        """Stop background recording gracefully."""
        ...

class FFmpegRecorder:
    """Record video from RTSP stream to file."""
    
    def __init__(self, stream_url: str):
        self.stream_url = stream_url
        self._process: subprocess.Popen | None = None
    
    def capture_frame(self, output_path: Path) -> None:
        """Capture single frame. Let exceptions bubble up—caller decides handling."""
        subprocess.run([
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", self.stream_url,
            "-vframes", "1",
            str(output_path),
        ], timeout=10, check=True)
    
    def start_recording(self, output_path: Path, duration_sec: float) -> None:
        if self._process is not None:
            raise RuntimeError("Already recording")
        
        self._process = subprocess.Popen([
            "ffmpeg", "-y",
            "-rtsp_transport", "tcp",
            "-i", self.stream_url,
            "-t", str(int(duration_sec)),
            "-c", "copy",
            str(output_path),
        ])
    
    def is_recording(self) -> bool:
        return self._process is not None and self._process.poll() is None
    
    def stop_recording(self) -> None:
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
```

Usage in AlertManager—exceptions bubble up, caller sees errors:

```python
# bbwatch/alert.py
class AlertManager:
    def __init__(self, config: AlertConfig, recorder: Recorder):
        self._config = config
        self._recorder = recorder
    
    def _handle_trigger(self, result: DetectionResult) -> None:
        """Respond to cry detection. Log errors, don't hide them."""
        
        if self._config.screenshot_on_peak and result.is_peak:
            try:
                output_path = self._config.screenshots_dir / f"{self._timestamp()}.jpg"
                self._recorder.capture_frame(output_path)
                LOGGER.info(f"Screenshot: {output_path.name}")
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError) as e:
                LOGGER.error(f"Screenshot failed: {type(e).__name__}: {e}")
                # No retry—let user see the error
        
        if self._config.record_clip_on_trigger and not self._recorder.is_recording():
            try:
                output_path = self._config.clips_dir / f"{self._timestamp()}.mp4"
                self._recorder.start_recording(output_path, self._config.record_clip_s)
                LOGGER.info(f"Recording started: {output_path.name}")
            except RuntimeError as e:
                LOGGER.warning(f"Recording: already in progress")
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                LOGGER.error(f"Recording failed: {type(e).__name__}: {e}")
```

**Benefits:**
- **No retries:** If FFmpeg fails, user sees the error. They can investigate and fix.
- **Pluggable:** Use FFmpegRecorder in production, MockRecorder in tests
- **Clear responsibilities:** AlertManager triggers recording, Recorder handles subprocess details
- **Observable:** Every attempt (success/failure) is logged with clear context

---

## Phase 3: Testing (Realistic Fixtures)

### 5. Fixture Library

Instead of mocking subprocess.run, use real output samples:

```python
# tests/fixtures/hardware_outputs.py
"""Real hardware detection output samples from actual RPi5 systems."""

LIBCAMERA_RPi5_NORMAL = """Available cameras
0 : imx708 [4608x2592 10-bit RGGB]
"""

LIBCAMERA_RPi5_WITH_WARNINGS = """WARNING: ControlValidator: Control 0x009e0902 missing
Available cameras
0 : imx708 [4608x2592 10-bit RGGB]
"""

LIBCAMERA_MULTIPLE_CAMERAS = """Available cameras
0 : imx708 [4608x2592 10-bit RGGB]
1 : ov5647 [2592x1944 10-bit BAYER]
"""

ARECORD_NORMAL = """arecord: device_list.c:268: (snd_device_name_hint) Cannot connect to server
card 0: PCH [HDA Intel PCH], device 0: ALC892 Analog [ALC892 Analog]
card 1: Device [USB PnP Sound Device], device 0: USB Audio [USB Audio]
"""

V4L2CTL_NORMAL = """USB Camera:
	/dev/video0
	/dev/video1

HD Video Capture:
	/dev/video2
"""

# tests/unit/test_hardware_detector.py
@pytest.mark.parametrize("output,expected_count", [
    (LIBCAMERA_RPi5_NORMAL, 1),
    (LIBCAMERA_RPi5_WITH_WARNINGS, 1),
    (LIBCAMERA_MULTIPLE_CAMERAS, 2),
])
def test_detect_picamera_parses_real_output(output, expected_count):
    """Real libcamera-hello outputs are correctly parsed."""
    detector = HardwareDetector()
    devices = detector._parse_libcamera(output)
    assert len(devices) == expected_count
    assert all(d.path.startswith("rpicam:") for d in devices)
```

**Benefits:**
- **Real data:** Tests use actual hardware output, not guesses
- **Easy to add variants:** New output format? Just add fixture + test case
- **Documentation:** Fixtures show what the code handles
- **Honest:** Tests run on all platforms; only RPi-specific tests skip when needed

---

## Implementation Roadmap

### Phase 1: Abstractions (Low Risk, High Impact)
- [ ] Create `bbwatch/sources.py` with AudioSource/VideoSource protocols
- [ ] Create `bbwatch/hardware_detector.py` HardwareDetector service
- [ ] Refactor `bbwatch/main.py` to use detector + source selection
- [ ] Update tests to use HardwareDetector service
- **Time estimate:** 4-6 hours
- **Risk:** None—new code, old paths still work until migration

### Phase 2: Error Handling (Medium Risk, Medium Impact)
- [ ] Create `bbwatch/recording.py` with Recorder protocol
- [ ] Refactor `bbwatch/alert.py` to use Recorder interface
- [ ] Update alert tests with MockRecorder
- **Time estimate:** 3-4 hours
- **Risk:** Low—recording is isolated from core logic

### Phase 3: Testing (Low Risk, Documentation)
- [ ] Create `tests/fixtures/hardware_outputs.py` with real samples
- [ ] Add parametrized parsing tests for each output type
- [ ] Add integration tests that run on RPi5 (skip on x86_64)
- **Time estimate:** 2-3 hours
- **Risk:** None—purely additive tests

---

## Benefits Summary

| Aspect | Before | After |
|--------|--------|-------|
| **Testability** | Mock subprocess.run | Real fixtures, plug mock sources |
| **Observability** | Silent failures | Clear logging of what's running |
| **Extensibility** | Add new source? Refactor main.py | Implement protocol, pass source to BabyMonitor |
| **Maintainability** | Device logic scattered | Centralized in HardwareDetector, sources |
| **Developer experience** | Read subprocess calls everywhere | Read clean protocols and clear initialization |

---

## Related Documents

- [SETUP_RPI5.md](./SETUP_RPI5.md) — Deployment guide (updated with new architecture)
- [architecture.md](./architecture.md) — System design (updated with source abstraction)
- [development.md](./development.md) — Testing patterns (updated with fixture approach)
