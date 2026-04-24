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
make lint                 # ruff check + mypy
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
                                       AlertManager
                                              ↓
USB Cam → go2rtc ← overlay pipe ← OverlayGenerator ← MotionDetector
               ↓
          WebRTC/RTSP → Browser / Phone
```

### Module responsibilities (`bbwatch/`)

| Module | Role |
|---|---|
| `main.py` | `BabyMonitor` orchestrator — wires all components, owns the 10Hz main loop |
| `config.py` | Pydantic models (`BBWatchConfig`) loaded from `config.yaml`; env-var overrides via `BBWATCH_` prefix |
| `capture.py` | `SlidingWindowCapture` — records overlapping WAV segments via FFmpeg or ALSA |
| `detector.py` | `SegmentHandler` (watchdog event handler) + DSP pipeline: Butterworth bandpass → RMS → `DetectionResult` |
| `alert.py` | `AlertManager` — hysteresis state machine (trigger_high / trigger_low thresholds + cooldown) |
| `motion.py` | `MotionDetector` — background thread reading RTSP/V4L2 frames, computes changed-pixel ratio |
| `overlay_generator.py` | `OverlayGenerator` — background thread rendering matplotlib frames into a named pipe at `overlay_fps` |
| `overlay.py` | `OverlayController` — legacy file-symlink overlay (static images); still used for health-check status |
| `storage.py` | `StorageManager` — background thread enforcing `max_size_mb` by deleting oldest WAV segments |
| `hardware.py` | Device discovery (ALSA + V4L2); called by `BabyMonitor._detect_hardware()` |

### Docker / deployment

- `docker/Dockerfile.dev` — x86_64 dev image (used for CI and local testing)
- `docker/Dockerfile.rpi` — ARM64 cross-compiled image for Raspberry Pi deployment
- `docker/docker-compose.yml` — full stack: `bbwatch` + `go2rtc` services; reads `HOST_VIDEO_DEVICE` and `HOST_AUDIO_SOURCE` from `config.yaml` via Makefile
- `docker/go2rtc.yaml` — go2rtc stream config; **`-framerate N` in this file must match `alerts.overlay_fps` in `config.yaml`** or the overlay stream will drift

### Key constraints

- The overlay pipe at `data_dir/overlays/overlay.pipe` is a FIFO; go2rtc reads it as an FFmpeg input. If go2rtc isn't running, `OverlayGenerator` will block on open.
- In Docker Compose mode, `bbwatch` reads audio from the RTSP stream (`BBWATCH_AUDIO__DEVICE_INDEX=rtsp://go2rtc:8554/babycam`) rather than a local ALSA device. Motion detection uses the `raw_video` variant of that URL to avoid consuming the overlaid stream.
- `fake_hardware=True` (CLI flag `--fake-hardware`) bypasses all device detection — useful for unit tests and environments without hardware.

### Test structure

```
tests/
  unit/          # Fast, no hardware — mock everything
  integration/   # capture + detector pipeline with generated audio
  functional/    # End-to-end monitor flow test
```
