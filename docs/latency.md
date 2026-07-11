# Latency Report

End-to-end latency analysis of bbwatch, mapped to the per-stage instrumentation
added in `bbwatch/latency.py`.

**Measured 2026-07-11 on the production RPi5** (AI Camera/IMX500 at 1080p@10fps
via host-native go2rtc, motion and overlay at 15 fps, one RTSP viewer
consuming `babycam`). Audio pipeline numbers are pending — no microphone was
connected at measurement time.

Related: [latency_improvements.md](latency_improvements.md) (improvement plan),
[decisions/](decisions/) (ADRs).

## How to read the logs

Every pipeline pass creates a `StageTimer` (`bbwatch/latency.py`) whose named
stage durations feed a per-pipeline `LatencyTracker`:

- **DEBUG** — one line per pass: `latency audio total=12.3ms pickup=4.1ms dsp=6.0ms ...`
- **INFO** — aggregated summary every `latency_report_interval_s` (default 10 s,
  `config.yaml`): `LATENCY audio n=42 pickup=avg 4.2/max 11.0ms | dsp=avg 5.8/max 9.1ms ...`

Collect on the Pi:

```bash
docker logs -f bbwatch | grep LATENCY          # summaries (INFO)
docker logs -f bbwatch | grep "latency "       # per-event detail (DEBUG; compose sets BBWATCH_LOG_LEVEL=DEBUG)
```

---

## Pipeline 1 — Audio cry detection

The critical path: sound in the room → alert on the parent's stream.

```
Mic (PulseAudio) ──go2rtc `device_audio`──▶ RTSP (AAC) ──▶ babycam (audio track)
                                                │
                     bbwatch SlidingWindowCapture (FFmpeg subprocess)
                                                │  segment muxer → WAV files
                                                ▼
    inotify close event ──▶ SegmentHandler.on_closed ──▶ DSP ──▶ AlertManager ──▶ overlay state
```

| Hop | Function / entry point | Library / mechanism | Instrumented stage |
|---|---|---|---|
| Mic capture + encode | go2rtc `device_audio` exec source (`docker/go2rtc.yaml:7`) | FFmpeg `-f pulse` → AAC 128k, `-fflags nobuffer -flags low_delay` | — (external) |
| Segment recording | `SlidingWindowCapture` → `AudioCapture._build_command` (`capture.py`) | FFmpeg subprocess, `segment` muxer; N staggered processes give one WAV every *stride* = `segment_duration_s − overlap_s` | structural (see below) |
| File-close notification | `Observer` → `SegmentHandler.on_closed` (`detector.py`) | `watchdog` (inotify `IN_CLOSE_WRITE`) | **pickup** (file mtime → handler entry) |
| Silence gate | `is_segment_empty` (`detector.py`) | `soundfile` read + numpy RMS | **empty_check** |
| Cry DSP | `detect_cry` → `butter_bandpass` (`detector.py`) | `scipy.signal.butter`/`sosfilt` (Butterworth band-pass 250–2000 Hz) + numpy windowed RMS | **dsp** |
| Alert decision | `AlertManager.process_intensity` (`alert.py`) | Pure-Python hysteresis (trigger_high/low + cooldown); on trigger: `FFmpegRecorder` clip/screenshot (background) | **alert** |
| Overlay state push | `OverlayGenerator.update_state` (`overlay_generator.py`) | Lock-guarded NamedTuple swap + deque append | **overlay_state** |

**Structural latency (not in the stage logs, dominates the total):** a cry is
0–`segment_duration_s` old when its WAV closes (average ≈ segment/2), and
segments land every stride. With the shipped tuning
(`segment_duration_s: 0.33`, `overlap_s: 0.1` → stride 0.23 s) the average
capture-to-file age is ≈ **165 ms**, worst case ≈ 330 ms. The `AlertManager`
also computes end-to-end `capture_ts → alert` as `last_latency_ms`, displayed
on the overlay itself (`Detect: NNNms`).

| Stage | Expected (x86 dev) | Measured RPi5 avg | Measured RPi5 max |
|---|---|---|---|
| pickup | < 10 ms | *TBD* | *TBD* |
| empty_check | 1–5 ms | *TBD* | *TBD* |
| dsp | 2–10 ms | *TBD* | *TBD* |
| alert | < 1 ms (ms–tens on trigger: recorder spawn) | *TBD* | *TBD* |
| overlay_state | < 1 ms | *TBD* | *TBD* |
| **structural (segment age)** | ~165 ms avg / 330 ms max | — | — |

## Pipeline 2 — Video / motion detection

```
CSI camera (imx708) ──libcamera──▶ go2rtc `rpicam:0` (1080p30 H.264)
        ├──▶ `babycam` composite ──▶ WebRTC/RTSP viewer
        └──▶ `raw_video` RTSP ──▶ MotionDetector (OpenCV)
```

| Hop | Function / entry point | Library / mechanism | Instrumented stage |
|---|---|---|---|
| Sensor → H.264 | go2rtc `rpicam:0#…codec=h264` (`docker/go2rtc.yaml:4`) | libcamera; hardware ISP encode on RPi5 | — (external) |
| RTSP decode | `MotionDetector._open_capture` / `_capture_loop` (`motion.py`) | `cv2.VideoCapture(CAP_FFMPEG)`, open/read timeouts 5 s, `CAP_PROP_BUFFERSIZE 1`; capture thread drains as fast as possible | — (freshness via frame_age) |
| Frame hand-off | capture thread → `_process_loop` (`motion.py`) | Lock-guarded latest-frame buffer, processed at `motion.fps` (15) | **frame_age** (capture ts → processing start) |
| Motion DSP | `MotionDetector.process_frame` (`motion.py`) | `cv2.cvtColor`/`GaussianBlur`/`absdiff`/`threshold`/`dilate` + numpy changed-pixel ratio | **process** |

| Stage | Expected (x86 dev) | Measured RPi5 avg | Measured RPi5 max |
|---|---|---|---|
| frame_age | ≤ ~70 ms (one 15 fps period) | **46–49 ms** | **104–115 ms** |
| process | 5–20 ms @ decode resolution | **10–12 ms** | **20–26 ms** |

Measured at 15 fps processing of the 1080p10 stream (n≈150 per 10 s window):
comfortably within budget. frame_age max ~110 ms reflects the 10 fps source —
a frame can be up to one source period (100 ms) old before processing.

`frame_age` is the one to watch: sustained growth means the process loop or the
RTSP decode can't keep up (CPU saturation or network stall).

## Pipeline 3 — Overlay compositing

```
main loop (20 Hz) ──update_state──▶ OverlayGenerator.run_loop (15 fps)
      generate_frame ──▶ FIFO overlay.pipe ──▶ go2rtc `babycam` FFmpeg rawvideo input
                                     ──▶ overlay filter ──▶ libx264 ultrafast/zerolatency ──▶ viewer
```

| Hop | Function / entry point | Library / mechanism | Instrumented stage |
|---|---|---|---|
| State ingestion | `BabyMonitor.run` main loop (`main.py`, 20 Hz) | Reads motion/audio/alert state, calls `update_state` | — |
| Frame render | `OverlayGenerator.generate_frame` (`overlay_generator.py`) | numpy RGBA canvas + `cv2.putText`/`rectangle`/`polylines`, `cv2.cvtColor` BGRA→RGBA | **render** |
| FIFO write | `OverlayGenerator._write_frame` | `os.write` loop on `O_NONBLOCK` fd with `select` backpressure; 1.2 MB frame vs 64 KiB pipe buffer | **write** |
| Composite + encode | go2rtc `babycam` exec (`docker/go2rtc.yaml:14-22`) | FFmpeg `overlay` filter, `libx264 -preset ultrafast -tune zerolatency -g 30` | — (external) |

| Stage | Expected (x86 dev) | Measured RPi5 avg | Measured RPi5 max |
|---|---|---|---|
| render | 3–15 ms | **1.3–2.1 ms** | 8.2 ms (12.8 ms first frame) |
| write | ~1 ms when reader keeps up; grows under backpressure | **1.0–12 ms** | 12–58 ms steady; **624 ms spike** while go2rtc's FFmpeg starts up |

The 624 ms write max occurred in the window where the babycam consumer FFmpeg
was starting (pipe buffer full until its reader began draining); steady state
settles to avg ~1 ms / max ~12 ms. render+write ≈ 3 ms against the 66 ms frame
budget — the overlay path has ample headroom.

`write` doubles as a **backpressure gauge**: it includes waiting for go2rtc's
FFmpeg to drain the pipe. If `render + write` exceeds the 66 ms frame budget
(15 fps), the loop skips frames and the overlay lags the video.

## External (uninstrumented) contributors

Measured only end-to-end (viewer-side), not in bbwatch logs:

- go2rtc H.264 encode/remux and WebRTC jitter buffer (typically the largest
  share of glass-to-glass latency; `-g 30` keyframe interval bounds join time).
- AAC audio encode + the `shortest=0` A/V sync in the babycam filter graph.
- Network (LAN Wi-Fi vs Ethernet) and browser decode.

## Measured CPU budget (RPi5, one babycam viewer, 2026-07-11)

| Process | % of one core | Role |
|---|---|---|
| babycam FFmpeg | ~75% | decode 1080p10 + overlay filter + libx264 encode (runs only while viewed) |
| bbwatch python | ~50% | motion RTSP decode + diff, overlay render, main loop |
| rpicam-vid | ~33% | 1080p10 software H.264 encode (Pi 5 has no HW encoder) |
| go2rtc | ~0% | pure remux |

≈1.6 of 4 cores with an active viewer; ≈0.85 idle (babycam's FFmpeg starts on
demand). See [camera.md](camera.md) for the offload roadmap (on-sensor
inference replacing the motion decode path).

## Config knobs that move these numbers

| Knob | File | Effect |
|---|---|---|
| `audio.segment_duration_s` / `overlap_s` | `config.yaml` | Structural detection latency (dominant term) |
| `alerts.overlay_fps` ⟷ `-framerate` | `config.yaml` / `docker/go2rtc.yaml` | Overlay frame budget; **must match** |
| `motion.fps` | `config.yaml` | frame_age budget and CPU cost |
| `latency_report_interval_s` | `config.yaml` | Summary log cadence |
| rpicam `width/height/fps` | `docker/go2rtc.yaml` | Decode cost in MotionDetector + encode cost |
