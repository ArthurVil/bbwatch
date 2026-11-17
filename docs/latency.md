# Latency Report

End-to-end latency analysis of bbwatch, mapped to the per-stage instrumentation
added in `bbwatch/latency.py`.

**Measured 2026-07-11 on the production RPi5** (AI Camera/IMX500 at 1080p@10fps
via host-native go2rtc, motion and overlay at 15 fps, one RTSP viewer
consuming `babycam`). Audio pipeline numbers are pending — no microphone was
connected at measurement time.

**Updated 2026-07-12**: pipeline migrated to **720p (1280x720) end-to-end**
(capture, overlay canvas, `babycam` composite — see
`docs/tech_note_language_and_acceleration.md` and the dataflow audit that
drove this change), plus copy/allocation cleanups in `overlay_generator.py`
and `motion.py` (RGBA-native color constants skip a per-frame `cvtColor`
pass, `_write_frame` streams the array directly instead of via
`.tobytes()`, `cv2.countNonZero` replaces a full boolean-array `sum`, the
dilate kernel is built once). `motion.process_width` was deliberately left
at its default (640) rather than raised to 1280 to match the new capture
resolution — the live numbers below show why that was the right call.
Re-measured under the same conditions (one active viewer) but with the
Pi otherwise idle (no other load).

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

| Stage | 2026-07-11 @ 1080p10 (avg/max) | **2026-07-12 @ 720p10 (avg/max)** |
|---|---|---|
| frame_age | 46–49 ms / 104–115 ms | **43–54 ms / 84–102 ms** |
| process | 10–12 ms / 20–26 ms | **0.7–0.8 ms / 1.2–4.0 ms** |

`process` dropped roughly an order of magnitude — the `countNonZero`/hoisted-
kernel cleanup accounts for some of it, but the bulk is that a `zoom: 2.0`
crop of a 1280x720 native frame lands almost exactly at
`motion.process_width`'s default (640), so `_resize_for_processing` becomes
a no-op (see the dataflow audit's note on this). `frame_age` stayed flat
across four consecutive 10 s windows over ~40 s of continuous viewing — the
`fps_mode=cfr` fix (below) holds under sustained load, not just briefly.

**On `process_width`**: with `process` costing well under 1 ms today, there
is ample headroom to raise `motion.process_width` toward 1280 (full native
width, no downscale) if finer-grained motion analysis is ever wanted —
even a ~4x pixel-count increase would land in the low single-digit
milliseconds. Left at 640 for now since nothing currently needs the extra
resolution; revisit if `zoom` changes or detection quality demands it.

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

| Stage | 2026-07-11 @ 640x480 canvas (avg/max) | **2026-07-12 @ 1280x720 canvas (avg/max)** |
|---|---|---|
| render | 1.3–2.1 ms / 8.2 ms (12.8 ms first frame) | **0.7–1.4 ms / 1.0–11.0 ms** |
| write | 1.0–12 ms / 12–58 ms steady, 624 ms cold-start spike | **1.8–2.0 ms / 5.7–17.3 ms** |

Despite a 4x larger canvas (1280x720 vs 640x480), both stages got *faster*:
the RGBA-native color constants (no more per-frame `cvtColor` pass) and the
direct-memoryview write (no more `.tobytes()` copy) more than offset the
larger frame size. render+write ≈ 2–4 ms against the 66 ms frame budget —
even more headroom than before.

`write` doubles as a **backpressure gauge**: it includes waiting for go2rtc's
FFmpeg to drain the pipe. If `render + write` exceeds the 66 ms frame budget
(15 fps), the delivery policy below decides what happens next.

### Frame delivery policy: `alerts.overlay_drop_stale_frames`

Controls what `run_loop` does when the pipe reader falls behind:

- **`true` (default)**: a 100 ms readiness check gates each frame; if the pipe
  isn't writable in time, that frame is dropped and the next pass sends
  fresher state instead. Bounded latency, some loss under load. The periodic
  `LATENCY overlay` summary reports `dropped=N/total (pct%)` so drops are
  visible rather than silent.
- **`false`**: the readiness gate is skipped — every frame is delivered,
  waiting on `_write_frame`'s backpressure loop for as long as it takes.
  No loss, but `write` (and therefore end-to-end latency) grows unbounded if
  the reader can't keep up; watch the `write=avg/max` figures in the summary
  for signs of a growing backlog. `dropped` is always `0/N (0.0%)` in this
  mode by construction — it does *not* mean the reader is keeping up.

## External (uninstrumented) contributors

Measured only end-to-end (viewer-side), not in bbwatch logs:

- go2rtc H.264 encode/remux and WebRTC jitter buffer (typically the largest
  share of glass-to-glass latency; `-g 30` keyframe interval bounds join time).
- AAC audio encode + the `shortest=0` A/V sync in the babycam filter graph.
- Network (LAN Wi-Fi vs Ethernet) and browser decode.

## Measured CPU budget (RPi5, one babycam viewer)

| Process | 2026-07-11 @ 1080p10 | **2026-07-12 @ 720p10** | Role |
|---|---|---|---|
| babycam FFmpeg | ~75% (up to 110% observed under contention) | **~39%** | decode + overlay filter + libx264 encode (runs only while viewed) |
| bbwatch python | ~50% (up to 73% with zoom enabled) | **~23%** | motion RTSP decode + crop/diff, overlay render, main loop |
| rpicam-vid | ~33% | **~17%** | software H.264 capture encode (Pi 5 has no HW encoder) |
| go2rtc | ~0% | ~1% | pure remux |

**~0.8 of 4 cores with an active viewer** (was ≈1.6), measured with the Pi
otherwise idle. Load average dropped from a peak of 12.33 (during an earlier
overload — three local Chromium tabs plus the encoder plus a since-fixed
`-fps_mode` bug that made `babycam` restart-loop) to a steady **2.64**.
Thermal: 58°C, `throttled=0x0` (was 67°C climbing under the earlier overload).
See [camera.md](camera.md) for the offload roadmap (on-sensor inference
replacing the motion decode path) and
[tech_note_language_and_acceleration.md](tech_note_language_and_acceleration.md)
for the full resolution-migration reasoning.

**`fps_mode=cfr` backlog fix confirmed holding**: `frame_age` stayed flat
(43–54 ms) across four consecutive 10 s windows of continuous viewing, no
upward drift — the growing-latency bug from 2026-07-11 (glass-to-glass delay
creeping to ~15 s after hours of runtime, traced to an encoder with no
frame-rate policy queuing every input frame regardless of how far behind it
fell) has not recurred under this measurement.

## Config knobs that move these numbers

| Knob | File | Effect |
|---|---|---|
| `audio.segment_duration_s` / `overlap_s` | `config.yaml` | Structural detection latency (dominant term) |
| `alerts.overlay_fps` ⟷ `-framerate` | `config.yaml` / `docker/go2rtc.yaml` | Overlay frame budget; **must match** |
| `motion.fps` | `config.yaml` | frame_age budget and CPU cost |
| `latency_report_interval_s` | `config.yaml` | Summary log cadence |
| rpicam `width/height/fps` | `docker/go2rtc.yaml` | Decode cost in MotionDetector + encode cost |
