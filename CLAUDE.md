# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
make install-dev          # Install all deps (including dev)

# Testing
make test                 # Run all tests with coverage
make test-unit            # Run unit tests only (tests/unit/)
poetry run pytest tests/unit/test_detector.py -v   # Run a single test file
poetry run pytest tests/ -k "test_name" -v         # Run a single test by name

# Linting & Formatting
make lint                 # ruff check + mypy (strict: disallow_untyped_defs)
make format               # Auto-fix with ruff

# Hardware discovery
make devices              # List available audio/video devices

# Full stack (requires webcam + mic)
make up                   # Start all services via Docker Compose
make down / make logs

# Docker dev
make docker-test          # Run tests inside Docker
make docker-demo          # Audio demo with mic passthrough
make docker-shell         # Interactive shell in dev container
make docker-build-rpi     # Cross-compile ARM64 image for Raspberry Pi
```

Config can be overridden at runtime via `BBWATCH_`-prefixed environment variables using `__` as nested delimiter (e.g. `BBWATCH_AUDIO__DEVICE_INDEX=rtsp://...`). Environment variables take precedence over `config.yaml`.

## Architecture

bbwatch is a local-only baby monitor: audio cry detection runs in Python, video streaming is handled by go2rtc, and a dynamic overlay is composited over the video stream via a named pipe.

### Data flow

```
USB Mic → FFmpeg/SlidingWindowCapture → WAV segments (disk)
                                              ↓
                                       SegmentHandler (watchdog)
                                              ↓
                                        Detector (DSP)
                                              ↓
                                       AlertManager → FFmpegRecorder (clips + screenshots)
                                              ↓
USB Cam → go2rtc ← overlay pipe ← OverlayGenerator ← MotionDetector
               ↓
          WebRTC/RTSP → Browser / Phone
               ↑
        StreamWatchdog (go2rtc REST API) → Notifier (ntfy) when viewers drop to 0
```

### Module responsibilities (`bbwatch/`)

| Module | Role |
|---|---|
| `main.py` | `BabyMonitor` orchestrator — wires all components, owns the 20Hz main loop |
| `config.py` | Pydantic models (`BBWatchConfig`) loaded from `config.yaml`; env-var overrides via `BBWATCH_` prefix |
| `sources.py` | `AudioSource`/`VideoSource` Protocols + implementations (ALSA, RTSP, V4L2, RPiCamera, Mock) |
| `hardware_detector.py` | `HardwareDetector` — discovers ALSA, V4L2, and Pi Camera devices |
| `hardware.py` | `AudioDevice`/`VideoDevice` dataclasses + output-parsing regexes shared by the detector |
| `capture.py` | `SlidingWindowCapture` — records overlapping WAV segments via FFmpeg or ALSA |
| `detector.py` | `SegmentHandler` (watchdog event handler) + DSP pipeline: Butterworth bandpass → RMS → `DetectionResult` |
| `alert.py` | `AlertManager` — hysteresis state machine (trigger_high / trigger_low + cooldown); triggers clip recording and screenshot capture on alert |
| `recording.py` | `Recorder` Protocol + `FFmpegRecorder` — captures frames/clips from the RTSP stream |
| `motion.py` | `MotionDetector` — background thread reading RTSP/V4L2 frames, computes changed-pixel ratio |
| `overlay_generator.py` | `OverlayGenerator` — background thread rendering matplotlib frames into a named pipe at `overlay_fps` |
| `overlay.py` | `OverlayController` — legacy file-symlink overlay (static images); still used for health-check status |
| `watchdog.py` | `StreamWatchdog` — background thread polling go2rtc REST API; alerts when all viewers disconnect |
| `notifier.py` | `Notifier` Protocol + `NtfyNotifier` — push notifications for the watchdog |
| `storage.py` | `StorageManager` — background thread enforcing `max_size_mb` by deleting oldest WAV segments |

### Source selection

`BabyMonitor._discover_audio_sources()` / `_discover_video_sources()` build priority-ordered lists of `sources.py` implementations; `_select_source()` picks the first whose `is_available()` succeeds:

- **Audio:** RTSP (when `audio.device_index` is an `rtsp://` URL, i.e. Docker mode) > local ALSA devices > mock
- **Video:** `RPiCameraSource` > `V4L2Source` > RTSP (only in Docker mode) > mock
- `fake_hardware=True` (CLI flag `--fake-hardware`) short-circuits everything to mocks — used by unit tests and hosts without hardware.

### Docker / deployment

- `docker/Dockerfile.dev` — x86_64 dev image (used for CI and local testing)
- `docker/Dockerfile.rpi` — ARM64 cross-compiled image for Raspberry Pi deployment
- `docker/docker-compose.yml` — full stack: `bbwatch` + `go2rtc` services; reads `HOST_VIDEO_DEVICE` and `HOST_AUDIO_SOURCE` from `config.yaml` via Makefile
- `docker/go2rtc.yaml` — go2rtc stream config; **`-framerate N` in this file must match `alerts.overlay_fps` in `config.yaml`** (currently 15) or the overlay stream will drift

### Key constraints

- The overlay pipe at `data_dir/overlays/overlay.pipe` is a FIFO; go2rtc reads it as an FFmpeg input. If go2rtc isn't running, `OverlayGenerator` will block on open.
- In Docker Compose mode, `bbwatch` reads audio from the RTSP stream (`BBWATCH_AUDIO__DEVICE_INDEX=rtsp://go2rtc:8554/babycam`) rather than a local ALSA device. Motion detection uses the `raw_video` variant of that URL to avoid consuming the overlaid stream.

### Raspberry Pi Camera Integration (RPi5)

bbwatch supports native Raspberry Pi Camera Module 3 on RPi5 via libcamera (not deprecated V4L2 compat):

- `HardwareDetector.detect_picamera_devices()` runs `libcamera-hello --list-cameras` and parses output like `0 : imx708 [4608x2592]` into `VideoDevice(path="rpicam:0")`.
- **Why RTSP restream?** OpenCV cannot open `rpicam:N` directly — go2rtc holds the camera hardware lock. `RPiCameraSource.open()` therefore returns `rtsp://localhost:8554/raw_video` instead of the raw device; the MotionDetector consumes that restream.
- **Deployment:** See [docs/SETUP_RPI5.md](docs/SETUP_RPI5.md) for full walkthrough (system packages, libcamera verification, Docker build, stream access).

### Test structure

```
tests/
  unit/          # Fast, no hardware — mock everything
  integration/   # capture + detector pipeline with generated audio
  functional/    # End-to-end monitor flow test
```
