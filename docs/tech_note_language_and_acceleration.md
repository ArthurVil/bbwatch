# Tech Note: Should bbwatch move to C++/Rust? And what can RPi5 hardware offload?

**Status:** exploratory analysis, not a decision (no ADR yet). Written 2026-07-12
in response to a direct question after diagnosing CPU pressure on the deployed
RPi5 (load average 12.3 on a 4-core system, with the babycam encoder,
`bbwatch` python process, `rpicam-vid`, and — that day — three locally-open
Chromium tabs all competing for the same cores).

## Recommendation up front

**Don't rewrite bbwatch in C++ or Rust.** The CPU pressure we measured is not
a language problem — it's an *architecture* problem (the Pi is asked to
decode, diff, and re-encode 1080p video entirely in software because it has
no hardware H.264 encoder), and a language port fixes none of it. The two
levers that actually move the needle are hardware-offload opportunities
specific to this camera/SoC, both reachable without leaving Python. A narrow,
profile-guided Rust extension is worth keeping in the back pocket for a
*specific* hot loop, later, if it's ever actually the bottleneck — not as a
wholesale rewrite.

## Where the CPU is actually going today

From this session's live measurements on the deployed RPi5 (one active
viewer):

| Process | CPU | What it's doing |
|---|---|---|
| `babycam` ffmpeg | ~75–110% of 1 core | 1080p H.264 decode + overlay filter + **software** libx264 encode |
| `bbwatch` python3 | ~50–73% | RTSP decode (OpenCV/FFmpeg), motion crop/resize/diff, overlay render, DSP |
| `rpicam-vid` | ~33–59% | 1080p **software** H.264 encode of the raw camera feed |

Two of these three lines are FFmpeg/libcamera doing **software video
encoding**, because the Raspberry Pi 5 shipped without a hardware H.264 (or
HEVC) *encoder* — confirmed in Raspberry Pi's own performance whitepaper: all
encoding runs on the Cortex-A76 cores via libx264/libav, at roughly
60–150% of one core for 1080p depending on quality preset.[^rpi5-encode] The
Pi 5 *does* have a hardware HEVC **decoder** and a new ISP, but nothing that
helps us produce the H.264 stream go2rtc serves to viewers. **No rewrite of
bbwatch, in any language, changes this** — it's a hardware ceiling that sits
entirely outside bbwatch's own code, in `rpicam-vid` and go2rtc's `ffmpeg`
subprocess.

The one line that *is* bbwatch's own code — the python3 process — is running
inside a comfortable budget today (motion `process` stage measured at
4–23ms against a 66ms/frame budget at 15fps; see `docs/latency.md`), even
accounting for the zoom-crop regression found and being fixed in parallel.
It is not currently the dominant cost.

## What a C++/Rust rewrite would and wouldn't buy

bbwatch's actual per-frame work — `cv2.cvtColor`, `cv2.GaussianBlur`,
`cv2.absdiff`, `cv2.resize`, `scipy.signal.butter`/`sosfilt` — already runs as
**compiled C/C++/Fortran** under a thin Python wrapper. OpenCV and SciPy are
not interpreted; calling `cv2.resize()` from Python and calling it from C++
executes the *same* native code. Porting the code that calls these libraries
to C++ or Rust does not make the libraries faster. What a rewrite *would*
remove is:

- **Python call/dispatch overhead** — real, but small relative to the cost of
  the operations it's dispatching to, for arrays of this size (hundreds of
  thousands to a few million pixels). It matters when you're calling into
  native code millions of times per second on tiny objects; it does not
  dominate a 15fps pipeline doing multi-megapixel array ops.
- **GIL contention across threads** — a genuine cost in bbwatch's design
  (separate capture/process/overlay/main-loop threads all under one
  interpreter lock), but numpy/OpenCV/SciPy release the GIL for the duration
  of their C calls, which is exactly the case that matters here — the GIL is
  held only for the thin Python-side glue between calls, not the array work
  itself.
- **Unnecessary array copies** — this is real and worth fixing, but it's a
  *code* issue (crop-then-resize ordering, full-canvas allocation per overlay
  frame, redundant color conversions), not a *language* issue. A careless
  Rust or C++ port can copy just as wastefully as careless Python; a careful
  Python port that reuses buffers and picks the right cv2 flags fixes it
  without touching the language. (A dedicated audit of these copy patterns is
  in progress as of this note; findings will land as ordinary optimization
  commits, independent of any language question.)

**Rust specifically undercuts its own value proposition here.** Rust's
headline benefit is compile-time memory safety. The moment you wrap OpenCV
via `opencv-rust` to get the actual computer-vision work done, you're calling
into the same unsafe C++ internals bbwatch already calls today — the
project's own documentation is explicit that it "doesn't [provide] Rust's
safety guarantees at this stage," particularly around borrow-checking and
shared ownership at the FFI boundary.[^opencv-rust] You'd get Rust's syntax
without Rust's safety guarantee, while giving up Python's much larger,
more mature CV/DSP ecosystem and — critically, per your own stated
priority — much slower algorithm iteration.

## Where a hybrid approach genuinely helps (and how the ecosystem already does this)

The documented, production-tested pattern for exactly this situation — "95%
orchestration code, cheap to iterate, in a high-level language; a narrow hot
loop in a compiled language when profiling proves it's needed" — is PyO3:
write a small Rust crate for one specific function, expose it as a normal
Python module via `maturin`, and call it from otherwise-unchanged Python. The
`rust-numpy` crate gives **zero-copy** access to NumPy array buffers (no
serialization, no duplication), and `Python::allow_threads` releases the GIL
around the Rust computation so it runs in true parallel with other Python
threads.[^pyo3] This is a scalpel, not a rewrite: you'd port *one* function —
say, the fused crop+resize+diff in `MotionDetector.process_frame` — the day
profiling shows it's the actual bottleneck, and leave everything else
(config, hardware protocols, alerting, tests, the entire orchestration layer)
in Python.

This isn't a novel idea for this exact problem space — it's what
**Frigate**, the most widely deployed RPi-class NVR project, already does.
Frigate's architecture is Python orchestration (multiprocessing for
isolation across cameras) with the actually-heavy compute — object
detection — delegated to accelerator-backed runtimes (TensorRT, OpenVINO,
Coral EdgeTPU) rather than a hand-rolled C++ detection loop.[^frigate] The
core insight that applies directly to bbwatch: **the CPU-bound work in a
camera pipeline is video codec and inference, and both have hardware or
accelerator answers that don't require rewriting the surrounding
application.**

## RPi5 acceleration: what actually exists, and what bbwatch could use

**No on-chip NPU.** Unlike some competing SBCs, the Pi 5's BCM2712 SoC has no
dedicated neural-processing unit — confirmed across multiple sources at
launch and since; if you want on-Pi hardware inference, it comes from an
add-on, not the SoC itself.[^rpi5-hw]

**No hardware video encoder** (H.264 or HEVC) — the ceiling described above.
The Pi 5 does have hardware HEVC *decode* and a new ISP block, neither of
which helps bbwatch's *encode* path.

**Two real, reachable offload opportunities, both usable without leaving Python:**

1. **The ISP's dual-stream ("lores") output — near-zero-cost downscale.**
   libcamera's ISP can produce *two* output images per captured frame from a
   single sensor read: a main high-resolution stream and a second,
   lower-resolution stream generated by the hardware scaler — and
   notably, **only the Pi 5's ISP can deliver that lores stream in RGB**
   (earlier Pi models are YUV-only there).[^lores] `rpicam-vid` already
   exposes this via `--lores-width`/`--lores-height`. Today, bbwatch's
   `MotionDetector` decodes the full 1080p H.264 stream in Python/OpenCV and
   then does the downscale itself (`cv2.resize`, CPU-bound). Requesting a
   second, motion-detection-sized stream directly from the ISP would move
   that downscale into hardware, for free, before any encoding or decoding
   even happens — a genuinely free lunch, no rewrite required, just a
   `rpicam-vid` flag and a second go2rtc stream definition.

2. **The IMX500's on-sensor inference — the bigger structural win.**
   As documented in `docs/camera.md`, the AI Camera's accelerator runs
   quantized neural networks *on the sensor die* and returns detection
   results (bounding boxes, labels, confidence scores) as frame metadata,
   via `picamera2`'s IMX500 integration — no `.rpk` model conversion is
   exotic; Sony and Raspberry Pi ship a documented toolchain for
   packaging PyTorch/TensorFlow detection models this way, and
   `picamera2` exposes the results as ordinary Python objects, filterable
   for e.g. "person" detections.[^imx500] Replacing bbwatch's
   RTSP-decode-and-frame-diff `MotionDetector` with on-sensor person/motion
   detection would eliminate that pipeline's entire CPU cost — freeing the
   core capacity that's actually under pressure (shared with the babycam
   software encoder) — and would likely detect an actual baby better than
   pixel differencing does. **This is an architecture change, not a
   language change**: it requires bbwatch (or a sidecar) to own the camera
   via `picamera2` directly instead of consuming it through go2rtc's
   `rpicam-vid` exec source, since IMX500 inference and go2rtc's current
   RTSP-restream approach don't compose today. It's also **easier in
   Python than in C++/Rust** — `picamera2`'s IMX500 helper API is
   Python-native; there is no C++ ecosystem advantage here, only a Python
   ecosystem advantage.

**If it's ever not enough:** the Raspberry Pi AI HAT+ (Hailo-8L, 13 TOPS, or
Hailo-8, 26 TOPS) is an official, PCIe M.2-connected external accelerator
that `rpicam-apps` natively detects and uses for post-processing — again, no
software rewrite, just a hardware add-on if inference workload ever exceeds
what the IMX500 alone can carry.[^hailo] Not needed for bbwatch's current
scope (motion/cry detection, not multi-object classification), but worth
knowing the ceiling isn't the IMX500 alone.

## Build/deployment overhead, honestly compared

| | Python (current) | C++ | Rust (hybrid, PyO3) |
|---|---|---|---|
| Cross-compile to ARM64 | None needed — `poetry install` on-device or `Dockerfile.rpi` already works | Full toolchain, CMake, manual dependency pinning; `Dockerfile.rpi` would grow substantially | `cross`/`maturin` handle aarch64 reasonably well; adds a Rust toolchain to CI and the build image |
| Memory safety | N/A (managed) | Manual; buffer overflows/use-after-free are exactly the class of bug this project's reliability rules exist to prevent | Strong, for pure-Rust code; weaker at any OpenCV FFI boundary (see above) |
| Algorithm iteration speed | Fast — this project's own stated priority | Slow — recompile/redeploy cycle for every DSP/CV tuning pass | Slow for the Rust portion; unaffected for the 95% that stays Python |
| Ecosystem maturity for CV/DSP | Best-in-class (OpenCV, SciPy, NumPy) | Native OpenCV, but manual everything else | `opencv-rust` explicitly less mature/safe than the C++ library it wraps |

## What I'd actually propose, phased

1. **Now (Python-only, no architecture change):** land the copy/allocation
   cleanup from the in-progress dataflow audit, and add the ISP lores-stream
   offload for motion detection. Both are cheap, low-risk, and address the
   *measured* costs directly.
2. **Current task:** lock the pipeline to 720p end-to-end (already in
   progress) — a straightforward ~44% pixel-count reduction across every
   stage (decode, diff, encode) with no architecture change.
3. **Later, bigger:** evaluate replacing OpenCV frame-differencing with
   IMX500 on-sensor detection. This is the change with the largest potential
   CPU win, but it changes who owns the camera (bbwatch/picamera2 vs.
   go2rtc/rpicam-vid) and deserves its own ADR and a spike before committing.
   Still Python.
4. **Only if profiling ever shows it's needed:** extract one specific,
   stable, hot loop into a PyO3-backed Rust module, following the pattern
   above. Not before there's a profile showing Python-level (not
   OpenCV-level) overhead actually dominates that loop — which isn't the case
   anywhere in bbwatch today.

No step here requires deciding "we are a C++/Rust project now." Each is
independently reversible and individually justified by a measured cost.

## Sources

- [H.264 encoding performance on Raspberry Pi 5-series computers (Raspberry Pi Ltd whitepaper)](https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-010033-WP-1-H.264%20encoding%20performance%20on%20Raspberry%20Pi%205_series%20computers.pdf) — no hardware H.264 encoder, software cost figures
- [Introducing Raspberry Pi 5 (Raspberry Pi Ltd)](https://www.raspberrypi.com/news/introducing-raspberry-pi-5/) — VideoCore VII, HEVC hardware decode, new ISP
- [Raspberry Pi 5 has no hardware video encoding and only HEVC decoding (Hacker News discussion)](https://news.ycombinator.com/item?id=38068801) — community confirmation, no on-chip NPU
- [opencv-rust (GitHub)](https://github.com/twistedfall/opencv-rust) — explicit statement on lack of Rust safety guarantees at the FFI boundary
- [Frigate NVR (frigate.video)](https://frigate.video/) and [Frigate object detectors docs](https://docs.frigate.video/configuration/object_detectors/) — Python orchestration + accelerator-backed detection architecture
- [A Week of PyO3 + rust-numpy](https://terencezl.github.io/blog/2023/06/06/a-week-of-pyo3-rust-numpy/) and [Making Python 100x faster with less than 100 lines of Rust](https://ohadravid.github.io/posts/2023-03-rusty-python/) — the "narrow hot-loop extraction" pattern, zero-copy NumPy interop
- [Raspberry Pi Camera Software docs](https://www.raspberrypi.com/documentation/computers/camera_software.html) and [libcamera dual-stream / lores forum thread](https://forums.raspberrypi.com/viewtopic.php?t=380265) — ISP dual-stream, Pi5-only RGB lores capability
- [IMX500 Integration (picamera2, DeepWiki)](https://deepwiki.com/raspberrypi/picamera2/8.2-imx500-integration) and [picamera2 IMX500 object detection demo (GitHub)](https://github.com/raspberrypi/picamera2/blob/main/examples/imx500/imx500_object_detection_demo.py) — on-sensor inference API, metadata output
- [Raspberry Pi AI HAT+ documentation](https://www.raspberrypi.com/documentation/accessories/ai-kit.html) — Hailo-8/8L external accelerator, native rpicam-apps support

[^rpi5-encode]: Raspberry Pi Ltd, "H.264 encoding performance on Raspberry Pi 5-series computers."
[^opencv-rust]: `twistedfall/opencv-rust` project README/docs.
[^pyo3]: PyO3 + rust-numpy community writeups (see Sources).
[^frigate]: Frigate NVR documentation and architecture overview.
[^rpi5-hw]: Raspberry Pi 5 launch coverage and community hardware threads (see Sources).
[^lores]: Raspberry Pi camera software documentation; libcamera community forum threads on dual-stream/lores behavior.
[^imx500]: `raspberrypi/picamera2` IMX500 integration documentation and example code.
[^hailo]: Raspberry Pi AI HAT+ product documentation.
