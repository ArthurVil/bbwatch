# Latency Improvements Plan

## Goal

Near real-time audio alerting and smooth video overlay for a baby monitor. Alerting must be async. End-to-end latency must be measured and displayed on the overlay.

---

## Current Latency Budget (Measured from audit)

| Stage | Min | Max | Source |
|---|---|---|---|
| Audio segment capture | 330ms | 330ms | `capture.py` `segment_duration_s=3.0` with `overlap_s=2.0` → stride 1s? No — default is 3s/1s=3 procs, stride=2s, new seg every **2s**. See note. |
| inotify watchdog delay | 50ms | 200ms | `watchdog` library, kernel notification |
| DSP detection | 10ms | 50ms | `detector.py` Butterworth + RMS |
| Alert state update | <1ms | <1ms | pure Python |
| **Audio critical path** | **390ms** | **580ms** | sum |
| Video frame latency | 10ms | 600ms | `motion.py` parallel threads; pipe write blocks |
| Overlay pipe write block | 0ms | 500ms | `overlay_generator.py:235-236` synchronous write |

> **Note on stride**: `SlidingWindowCapture` with `segment_duration_s=3.0, overlap_s=1.0` → stride=2s, 3 processes, new segment every **2s**. With `segment_duration_s=0.5, overlap_s=0.25` → stride=0.25s. This is the single highest-impact knob.

---

## Changes (Ordered by Priority)

### 1 — Reduce audio segment duration (`config.yaml`)

**File:** `config.yaml`

```yaml
audio:
  segment_duration_s: 0.5   # was 3.0 — reduces audio latency from ~2s to ~250ms
  overlap_s: 0.25            # was 1.0 — stride = 0.25s, new segment every 250ms
```

**Effect:** Audio critical path drops from ~2.3s to ~300ms. No code changes.

**Trade-off:** More inotify events and more DSP calls per second. At 16kHz mono with 500ms segments, each WAV is 16KB — negligible I/O.

---

### 2 — Fix blocking FIFO write (`overlay_generator.py`)

**File:** `bbwatch/overlay_generator.py:235-236`

**Problem:** `pipe.write(frame_data)` blocks if go2rtc's reader is consuming slowly. The write blocks the entire overlay thread, causing frame drops and video stutter.

**Fix:** Open the FIFO with `O_NONBLOCK`, catch `BlockingIOError`, and drop the frame rather than stalling.

```python
import os
import fcntl

# Inside run_loop, replace `open(self.pipe_path, "wb") as pipe:` with:
fd = os.open(str(self.pipe_path), os.O_WRONLY | os.O_NONBLOCK)
pipe = os.fdopen(fd, "wb", buffering=0)
```

Then in the write section:
```python
try:
    pipe.write(frame_data)
    # No flush() needed with buffering=0
except BlockingIOError:
    # Reader is behind — drop this frame, do not block
    pass
```

**Why no flush():** `buffering=0` means every `write()` goes directly to the kernel buffer (unbuffered binary mode). A flush on a FIFO is a no-op; buffering=0 is the correct fix.

**Effect:** Overlay thread never blocks on a slow reader. Video smoothness becomes a function of the go2rtc/FFmpeg reader speed, not our writer.

---

### 3 — Add `capture_ts` timestamps and propagate through pipeline

**Purpose:** Enable end-to-end latency calculation and display on overlay.

**Files changed:**
- `bbwatch/detector.py` — `SegmentHandler.on_closed()` records `capture_ts = time.time()` and passes it to `alert_manager.process_intensity(rms, capture_ts=capture_ts)`
- `bbwatch/alert.py` — `process_intensity()` accepts optional `capture_ts: float | None = None`; stores `self._last_capture_ts = capture_ts` on trigger
- `bbwatch/overlay_generator.py` — `update_state()` accepts `latency_ms: float | None = None`; `generate_frame()` renders it

**Latency calculation:**
```python
# In SegmentHandler.on_closed():
capture_ts = time.time()  # "now" = segment just closed ≈ segment end time
# Pass downstream so overlay can display how old the detection is
```

This measures **detection latency** (time from segment close to alert), not absolute end-to-end. True end-to-end would require embedding a timestamp in the WAV filename, which is Phase 2 of this change.

**WAV filename timestamp (Phase 2 — optional):**
FFmpeg `-strftime 1` flag renames segments to include UTC timestamp. `SegmentHandler` would parse this to compute true segment-to-alert delay.

```python
# capture.py _build_command():
"-strftime", "1",
output_pattern = str(self.output_dir / f"{self.prefix}_%Y%m%d_%H%M%S.wav")
```

---

### 4 — Tune motion detector FPS and main loop rate (`config.yaml` + `main.py`)

**File:** `config.yaml`

```yaml
motion:
  fps: 15     # was 5 — smoother motion detection
  
alerts:
  overlay_fps: 15   # was 5 — must match go2rtc.yaml -framerate N
```

**File:** `main.py:365`

```python
time.sleep(0.05)   # was 0.1 — 20Hz main loop for more responsive overlay state updates
```

**Important:** `overlay_fps` in `config.yaml` **must** match `-framerate N` in `docker/go2rtc.yaml`. If they diverge, the overlay pipe accumulates frames and goes2rtc desynchronizes. The current CLAUDE.md already documents this constraint.

**File:** `docker/go2rtc.yaml` — update `-framerate` to match.

---

### 5 — Make screenshot async (verify `alert.py`)

**File:** `bbwatch/alert.py`

The screenshot path already runs on a daemon thread (from previous session's implementation). Verify the clip recording path also runs non-blocking. Both `start_recording()` and `capture_frame()` in `FFmpegRecorder` use `subprocess.Popen` (non-blocking), so the `AlertManager.process_intensity()` hot path is non-blocking.

No code change needed — verify in review.

---

## Implementation Order

1. `config.yaml` — reduce `segment_duration_s` + `overlap_s`, bump `motion.fps` + `overlay_fps` (5 min)
2. `overlay_generator.py` — fix blocking pipe write (20 min)
3. `detector.py` + `alert.py` + `overlay_generator.py` — timestamp propagation (45 min)
4. `main.py` — 20Hz loop (2 min)
5. `docker/go2rtc.yaml` — match framerate (2 min)

---

## Files to Modify

| File | Change |
|---|---|
| `config.yaml` | `segment_duration_s: 0.5`, `overlap_s: 0.25`, `motion.fps: 15`, `alerts.overlay_fps: 15` |
| `bbwatch/overlay_generator.py` | Non-blocking FIFO open; `update_state()` accepts `latency_ms`; render latency on frame |
| `bbwatch/detector.py` | Record `capture_ts` in `on_closed()`; pass to `alert_manager.process_intensity()` |
| `bbwatch/alert.py` | `process_intensity()` accepts `capture_ts`; exposes `last_latency_ms` property |
| `bbwatch/main.py` | 20Hz sleep; pass `latency_ms` to `overlay_generator.update_state()` |
| `docker/go2rtc.yaml` | `-framerate 15` |

---

## Acceptance Criteria

- New segment arrives every ~250ms (verify via `ls -lrt wav_segments/` — timestamps 250ms apart)
- Overlay pipe never blocks (verify by killing go2rtc while bbwatch runs — overlay thread stays alive)
- Overlay displays latency text like `"Detect: 42ms"` on each frame
- All 151 existing tests still pass
