# Camera Report — Raspberry Pi AI Camera (Sony IMX500)

The camera connected to this deployment's RPi5 via CSI is the official
**Raspberry Pi AI Camera**, built around the Sony IMX500 Intelligent Vision
Sensor. Confirmed on-device:

```
$ rpicam-hello --list-cameras
0 : imx500 [4056x3040 10-bit RGGB] (/base/axi/pcie@1000120000/rp1/i2c@80000/imx500@1a)
    Modes: 'SRGGB10_CSI2P' : 2028x1520 [30.02 fps]
                             4056x3040 [10.00 fps]
```

## Hardware characteristics

| Property | Value |
|---|---|
| Sensor | Sony IMX500, stacked CMOS + on-die AI DSP |
| Resolution | 12.3 MP (4056 × 3040) |
| Sensor format | Type 1/2.3" (7.857 mm diagonal) |
| Pixel size | 1.55 µm × 1.55 µm |
| Shutter | Rolling |
| Lens | 4.74 mm focal length, **f/1.79** |
| Field of view | 78.3° ± 3° diagonal |
| Focus | Manual (mechanical ring), 20 cm → ∞ |
| IR cut filter | **Integrated, not removable** — no IR sensitivity |
| NoIR variant | **Does not exist** for the AI Camera |
| Native sensor modes | 2028×1520 @ 30 fps (2×2 binned, 10-bit) · 4056×3040 @ 10 fps (10-bit) |
| AI subsystem | On-sensor DSP + 8 MB memory for CNN inference (output tensors, not video) |

Any requested output size (e.g. our 1920×1080) is derived by rpicam/libcamera
from one of the two native modes — 1080p comes from a crop/scale of the binned
2028×1520@30 mode. **1920×1080 @ 10 fps is comfortably within capability** and
is a sensible target for this use case.

## Low-light behavior — the key constraint for a baby monitor

The optics are respectable for a small sensor (f/1.79 is fast; 1.55 µm pixels
are mid-range), so in a **dim** room (night light, hallway light through a
door) it produces usable, if noisy, images: libcamera's auto-exposure will push
analogue gain up and exposure time out, trading noise and motion blur for
brightness. At 10 fps we allow up to ~100 ms exposures, which helps
significantly in low light compared to 30 fps (max ~33 ms) — another argument
for the 1080p10 configuration.

In a **dark** room it cannot see: the integrated IR-cut filter blocks infrared,
there is no NoIR variant of the AI Camera, and standard baby-monitor-style IR
illuminators are therefore useless with this camera. Consequences:

- **Night monitoring requires some visible light** — a dim warm night light is
  the practical answer (warm/red light interferes least with infant sleep).
- If true dark-room night vision becomes a requirement, the hardware answer is
  a different camera (e.g. Camera Module 3 NoIR + IR illuminator), not
  configuration.
- Audio cry detection (the primary alert path) is unaffected by darkness —
  in a fully dark room bbwatch degrades to an audio monitor with a black video
  feed, which is a acceptable failure mode as long as the parent knows it.

Practical low-light tuning available in `docker/go2rtc.yaml`'s rpicam source if
needed: lower fps (longer max exposure), and rpicam options for gain/exposure
ceilings. Motion detection thresholds (`motion.threshold`) may need raising at
night since sensor noise inflates frame-to-frame differences.

**Auto-exposure convergence as a false-motion source.** Frame differencing
can't distinguish "the whole frame got brighter" from real motion — every
pixel shifts together and can spike the changed-pixel percentage well past
`motion_threshold_percent`, exactly when the AE loop is working hardest (a
dimming room at night). Two independent mitigations, addressing it from
opposite ends:

- **Software** (`motion.equalize_luminosity`, in `config.yaml`): applies
  global histogram equalization before differencing, which is mathematically
  invariant to any monotonic brightness shift *provided no pixel
  saturates* — see `bbwatch/motion.py`. A shift large enough to clip pixels
  to 0/255 (a dark room pushed bright — the exact scenario this targets)
  breaks that guarantee: measured ~44% saturated pixels still leaving ~38%
  residual false motion, well above a typical `motion_threshold_percent`.
  Cheap, always available, no video-quality tradeoff, but doesn't stop AE
  from converging slowly (motion blur / gain noise during the transition are
  unaffected), and isn't a hard guarantee in the brightest/darkest extremes.
- **Camera-level** (not currently applied — see the commented example in
  `deploy/go2rtc-host.yaml`'s `device_video` stream): fixing `--shutter`
  (µs) and `--gain` removes AE hunting at the source entirely, at the cost
  of losing auto-adaptation to real brightness changes in the room (a lamp
  turning on/off would then look identical to the camera). Needs a value
  tuned to the actual room and left as an opt-in example rather than a
  default for that reason.

Use the software option first — it's zero-risk. Camera-level locking is worth
revisiting only if a specific room's AE hunting proves severe enough that
equalization's per-frame fix isn't enough (e.g. motion blur during the
exposure transition itself becomes the bigger problem).

## Can the on-board accelerator encode/compress video? No — but it can offload something better

The IMX500's accelerator is a **neural-network inference DSP, not a video
encoder**. It runs quantized CNNs (MobileNet SSD, PoseNet, YOLOv8, or custom
models packaged as `.rpk` via Sony's toolkit) *on the sensor* and returns
**output tensors as frame metadata** alongside the image. It cannot produce
H.264/H.265, and nothing about it reduces encode cost.

This matters because the **Pi 5 has no hardware video encoder at all** (unlike
the Pi 4): all H.264 in our pipeline is software (libx264 / libav on the
Cortex-A76 cores). Raspberry Pi's own measurements put software 1080p30
low-latency encoding at ~60% of one core, scaling roughly with pixel rate.

### Where the CPU actually goes today (and how to reduce it)

Our current video pipeline encodes **twice** and decodes **twice**:

```
IMX500 → rpicam (SW encode #1, 1080p30 H.264)
        → go2rtc RTSP → babycam FFmpeg (decode #1) ─┐
                                                     ├─ overlay filter → libx264 (SW encode #2)
        overlay FIFO (rawvideo) ────────────────────┘
        → raw_video RTSP → MotionDetector OpenCV (decode #2, 15 fps)
```

Ranked offload options:

1. **Drop the sensor stream to 1080p @ 10 fps** (`docker/go2rtc.yaml`:
   `rpicam:0#width=1920&height=1080&fps=10&codec=h264`). Cuts encode #1,
   decode #1, encode #2 and decode #2 by ~3× each — the single biggest lever,
   zero code changes. Align the rest: `motion.fps: 10`, `alerts.overlay_fps: 10`
   **and** the babycam `-framerate 10` (the documented invariant), which also
   cuts overlay render cost by a third.
2. **Use the accelerator for what it *can* do: replace CPU motion detection.**
   Run person/motion-relevant detection on-sensor (picamera2 + IMX500 helper,
   `imx500-models` firmware) and consume detection metadata instead of the
   `raw_video` RTSP stream. This would eliminate decode #2 and the OpenCV
   diff pipeline entirely (~the whole MotionDetector CPU budget) and likely
   detect a moving baby *better* than pixel differencing. Architectural note:
   this needs bbwatch (not go2rtc) to own the camera via picamera2, or a
   sidecar process publishing detections — a significant restructure, tracked
   as future work.
3. **Eliminate the double encode** by moving the overlay composite into the
   single rpicam→viewer path (or accepting a static-overlay-free raw restream
   for viewers wanting minimal latency). More invasive; only worth it if 1)
   is insufficient on thermals.

## Recommended bbwatch configuration for this camera

| Setting | File | Value | Why |
|---|---|---|---|
| rpicam source | `docker/go2rtc.yaml` | `width=1920&height=1080&fps=10` | User requirement; 3× CPU cut; longer exposures in low light |
| babycam `-framerate` | `docker/go2rtc.yaml` | `10` | Must match `overlay_fps` |
| `alerts.overlay_fps` | `config.yaml` | `10` | Invariant with above |
| `motion.fps` | `config.yaml` | `10` | No point sampling faster than the source |
| `motion.threshold` | `config.yaml` | revisit at night | Sensor noise in low light inflates diffs |

## Sources

- [Raspberry Pi AI Camera product page](https://www.raspberrypi.com/products/ai-camera/)
- [Raspberry Pi AI Camera documentation](https://www.raspberrypi.com/documentation/accessories/ai-camera.html) (accelerator = inference tensors, model workflow)
- [AI Camera product brief (PDF)](https://datasheets.raspberrypi.com/camera/ai-camera-product-brief.pdf) (optics, modes, focus)
- [H.264 encoding performance on Raspberry Pi 5 (whitepaper)](https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-010033-WP-1-H.264%20encoding%20performance%20on%20Raspberry%20Pi%205_series%20computers.pdf) (no HW encoder; SW encode costs)
- [Raspberry Pi forums — no NoIR variant of the AI Camera](https://forums.raspberrypi.com/viewtopic.php?t=388506)
