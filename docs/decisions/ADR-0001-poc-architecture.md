# ADR-0001 — PoC Architecture: DSP-based cry detection on Raspberry Pi

**Date:** 2024 (initial design)
**Status:** Accepted — PoC complete, core architecture retained in production

---

## Context

We needed a FOSS baby monitor that could:
- Stream live video + audio to an Android phone over local Wi-Fi
- Detect baby cry sounds and show a visual alert on the stream
- Run entirely on-device (no cloud, no custom app)
- Be buildable quickly with high confidence

Hardware target: Raspberry Pi 4 (later 5), USB webcam, USB microphone, Android phone as viewer.

Three approaches were considered for cry detection:

| Approach | Latency | Accuracy | Complexity | Offline? |
|---|---|---|---|---|
| **DSP: bandpass + RMS** | Low | Good enough for PoC | Low | Yes |
| ML inference (ONNX/TFLite) | Medium | High | High | Yes |
| Cloud API (Google Speech) | High | High | Low | No |

ML was ruled out for the PoC because labelled baby-cry training data and model integration work would take weeks. Cloud was ruled out because the whole point is local-only operation.

Three approaches were considered for video streaming and overlay composition:

| Approach | Complexity | WebRTC | Overlay support |
|---|---|---|---|
| **go2rtc + FFmpeg overlay** | Low | Built-in | Via FFmpeg exec source |
| Custom GStreamer pipeline | High | Manual | Native |
| nginx-rtmp + custom player | Medium | No | Difficult |

---

## Decision

**Audio detection:** Butterworth bandpass filter (250–2000 Hz, targeting baby cry fundamental) + RMS energy in 100ms windows. Alert triggers when ≥3% of windows in a segment exceed RMS threshold. Hysteresis (trigger_high / trigger_low) prevents oscillation.

**Audio capture:** FFmpeg `-f segment` writing overlapping `.wav` files to disk. Multiple staggered FFmpeg processes implement a sliding window — this gives new segments to analyse every ~230ms while each segment contains 330ms of context (see `bbwatch/capture.py`).

**Detection pipeline:** File-system event-driven (`watchdog` inotify) — a `SegmentHandler` fires on each new `.wav` close event, runs DSP, updates `AlertManager`. Disk I/O is acceptable at this scale and simplifies debugging.

**Video streaming:** go2rtc handles WebRTC/RTSP without custom signalling. Overlay is composited by an FFmpeg exec source in go2rtc that reads from a named FIFO pipe (`overlay.pipe`). A Python thread writes RGBA frames to this pipe at 15fps.

**Alert state machine:** Three-state hysteresis (IDLE → TRIGGERED → COOLDOWN → IDLE) with configurable thresholds and cooldown. Side effects (screenshot via FFmpeg, clip recording) are triggered on state entry and run on daemon threads to keep the detection hot path non-blocking.

**Why a status file (`status.json`)?** Decouples the detection process from overlay rendering. The overlay generator reads alert state from memory (updated by the same process), but a JSON file also allows external tools (Home Assistant, scripts) to read monitor state without inter-process communication.

---

## Consequences

### What this enables

- Entire stack runs in Docker Compose — reproducible on any Linux host and ARM64 RPi
- Detection latency: ~400–600ms end-to-end (segment close → alert active)
- No labelled data required; thresholds are tunable in `config.yaml`
- go2rtc WebRTC means any browser or VLC client works without installing anything

### Known trade-offs accepted

- **DSP accuracy:** Bandpass + RMS will false-positive on adult speech or TV audio at similar frequencies. Accepted for PoC; ML upgrade path is documented below.
- **Disk writes:** Short WAV segments are written to disk at ~4 files/second. At 16kHz mono, this is ~64KB/s — negligible. `StorageManager` enforces a configurable cap (default 1 GB).
- **Blocking FIFO open:** The named pipe open blocks until go2rtc connects. The overlay thread therefore starts only after go2rtc is running. Fixed in `bbwatch/overlay_generator.py` with `O_NONBLOCK` on the fd.

### What was explicitly ruled out for the PoC

- ROS2 (no need for publish/subscribe at this scale)
- Machine learning models
- Cloud connectivity
- WAN/NAT traversal
- Custom mobile app (browser + VLC suffice)
- SQLite timeline storage (planned for a future phase)
- Audio/video synchronization beyond go2rtc defaults

---

## Upgrade path (post-PoC)

1. **ML cry detector:** Replace `detect_cry()` in `bbwatch/detector.py` with an ONNX/TFLite model. The `SegmentHandler` interface stays the same — swap the function, not the pipeline.
2. **Zero-disk audio:** Replace FFmpeg file segments with GStreamer `appsink` feeding audio buffers directly into Python. Eliminates the inotify hop.
3. **Night vision:** RPi NoIR or USB IR camera — go2rtc and `MotionDetector` are camera-agnostic.
4. **Notifications:** `bbwatch/notifier.py` + `StreamWatchdog` already implement ntfy and webhook delivery.
5. **Activity timeline:** Add SQLite writer in `AlertManager._write_status()` — schema: `(timestamp, intensity, state, latency_ms)`.

---

## Implementation references

| Concern | Module |
|---|---|
| DSP detection | `bbwatch/detector.py` — `detect_cry()`, `butter_bandpass()` |
| Alert hysteresis | `bbwatch/alert.py` — `AlertManager`, `AlertState` |
| Audio capture | `bbwatch/capture.py` — `SlidingWindowCapture` |
| Video overlay | `bbwatch/overlay_generator.py` — `OverlayGenerator.run_loop()` |
| Hardware detection | `bbwatch/hardware_detector.py` — `HardwareDetector` |
| Config | `bbwatch/config.py` — `BBWatchConfig` (Pydantic, env-var overrides) |
| Latency analysis | `docs/latency_improvements.md` |
| RPi5 setup | `docs/SETUP_RPI5.md` |
