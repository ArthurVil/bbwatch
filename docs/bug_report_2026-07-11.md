# Bug Report — 2026-07-11 Full Review

Findings from a four-agent audit (code review, quality, test coverage, documentation)
of branch `feat/pi-camera-rpi5`, independently verified before fixing. All five bugs
below are **fixed, each in an atomic commit with a regression test**. Baseline before
fixes: lint clean, 151 tests passing, 80% coverage — none of these bugs were visible
in CI, which is itself the central lesson (see "Common thread").

| # | Severity | Component | Fix commit |
|---|----------|-----------|------------|
| 1 | Critical | overlay_generator | `50e70e7` |
| 2 | High | config | `d97cf3c` |
| 3 | Critical | main (source selection) | `e08a9aa` |
| 4 | Critical | capture (FFmpeg) | `dd423a0` |
| 5 | Critical | motion | `1e61b2f` |

---

## BUG-1 — Dynamic overlay never wrote a single frame

**Location:** `bbwatch/overlay_generator.py:258` (introduced by commit `b430bf3`,
"non-blocking FIFO write")

**Root cause:** `select.select(rlist, wlist, xlist)` returns its three lists in the
same order. The pipe fd was registered in the *write* list, but the result was
unpacked as `ready, _, _` — the *read* list, which is always empty. The
`os.write()` call was therefore unreachable code.

A second, latent defect sat behind it: a 640×480 RGBA frame is 1,228,800 bytes,
while a Linux pipe buffer holds 64 KiB. Even with the unpack fixed, the single
unchecked `os.write()` on a non-blocking fd would have written at most 64 KiB and
dropped the rest. FFmpeg's rawvideo demuxer has no framing markers, so every
short write would permanently desync the stream (rolling/garbled overlay).

**Consequences:** The composited `babycam` stream — the product's main output —
received no overlay input at all. go2rtc's FFmpeg reader starved on the FIFO.
No error was logged anywhere; the generator loop ran "successfully" at 15 fps.

**Why tests missed it:** the existing unit test mocked `select.select` to return
the fd **in both lists**, so the wrong-list unpack still passed. The mock encoded
the same misunderstanding as the code under test.

**Fix:** unpack the write list; new `_write_frame()` completes every started frame
(waiting for the reader to drain between chunks) while still dropping whole frames
up-front when the reader lags. **Verification:** new end-to-end test drives a real
FIFO and asserts two complete frames (2 × 1,228,800 bytes) arrive; the old code
hangs/fails this test (confirmed by running it against the pre-fix code).

---

## BUG-2 — Dead config key: operator's alert cooldown silently ignored

**Location:** `config.yaml:28` vs `bbwatch/config.py:82`

**Root cause:** `config.yaml` set `alert_cooldown_s: 1.0`, but the `AlertConfig`
field is named `cooldown_s`. Pydantic's default `extra="ignore"` dropped the
unknown key without a warning.

**Consequences:** the operator's 1.0 s cooldown never applied; alerts used the
5.0 s default. More broadly, *any* typo'd key in any config section was silently
ignored — a whole class of invisible misconfiguration. The same dead key was also
found lurking in the shared `test_config` fixture (`tests/conftest.py`), along
with a `StorageConfig` field being splatted into `DetectionConfig`.

**Fix:** key renamed to `cooldown_s`; all config section models now inherit
`StrictModel` (`extra="forbid"`), so an unknown key fails at startup with a
`ValidationError` naming the bad key. **Verification:** new tests assert the old
key is rejected, the new key reaches `AlertConfig`, and the shipped `config.yaml`
loads validly. Enabling strict mode immediately caught the fixture contamination
(6 integration tests failed until the fixture was cleaned) — the mechanism works.

---

## BUG-3 — Monitor pretended to work with no hardware at all

**Location:** `bbwatch/main.py` `_discover_audio_sources` / `_discover_video_sources`

**Root cause:** outside `--fake-hardware`, when no device was found, the discovery
methods appended `MockAudioSource`/`MockVideoSource` as a fallback. Mock sources
report `is_available() == True` unconditionally, so `_detect_hardware()` returned
success and startup proceeded.

**Consequences:** the worst failure mode this product can have — a parent sees a
running monitor (container up, stream nominally alive) while it is listening to
and watching **nothing**. In practice it then crashed downstream anyway
(`Mock.open()` returns `None`, which `AudioCapture._build_command` cannot handle),
but with a confusing traceback far from the real cause. Fixing the fallback also
exposed a contradiction: `_detect_hardware()` accepted video-only operation while
`start()` unconditionally raised without audio.

**Fix:** no mock fallback in production paths — missing hardware now aborts
startup with `RuntimeError("Required hardware not found")`. Video-only operation
is now honored: `start()` skips the audio pipeline and logs
`CRY DETECTION IS DISABLED` at ERROR. **Verification:** two tests that explicitly
pinned the buggy fallback behavior were inverted; new tests cover startup refusal
and video-only operation.

---

## BUG-4 — Undrained FFmpeg stderr could wedge audio capture silently

**Location:** `bbwatch/capture.py` `_start_process` / `_monitor_loop`

**Root cause:** FFmpeg was started with `stderr=subprocess.PIPE`, but the pipe was
only read *after* the process died. A chatty input (flaky ALSA device, unstable
RTSP at `-loglevel warning`) fills the 64 KiB pipe buffer; FFmpeg then blocks on
its next stderr write and stops processing audio — while `poll()` still reports it
alive.

**Consequences:** `is_running()` stayed `True`, the restart monitor never fired,
WAV segments stopped appearing, and cry detection died with zero signal to the
user. This is the "the monitor looks alive but is deaf" scenario.

**Fix:** a daemon thread drains stderr into a bounded deque (last 100 lines);
the monitor loop reports that tail when the process dies; stdout goes to
`DEVNULL` (never read — the segment muxer writes files). **Verification:**
regression test launches a real child process writing 200 KB to stderr and
asserts it runs to completion — under the old code that child blocks forever
once the pipe fills.

---

## BUG-5 — Motion detection died silently and served stale data forever

**Location:** `bbwatch/motion.py` `_capture_loop`; `bbwatch/main.py` main loop

**Root cause:** if `cv2.VideoCapture` failed to open (go2rtc restarting, RTSP
unreachable, camera unplugged), `_capture_loop` logged one ERROR and `return`ed —
permanently killing the capture thread. Nothing polled thread liveness:
`get_current_motion()` kept returning the last computed value indefinitely.
Additionally, RTSP reads had no timeout, so a *hung* (rather than dead) stream
blocked `cap.read()` past `stop()`'s 1 s join.

**Consequences:** the overlay kept rendering a plausible, frozen motion level; a
parent glancing at the stream sees "no motion" — indistinguishable from a calm
baby. A transient go2rtc restart at startup was enough to disable motion
detection for the whole run.

**Fix:** the capture loop now reconnects forever (5 s interval on open failure,
reopen on read failure, handle always released); RTSP opens set
`CAP_PROP_OPEN_TIMEOUT_MSEC`/`CAP_PROP_READ_TIMEOUT_MSEC` (5 s); MotionDetector
exposes `last_frame_age_s()`/`is_healthy()`; the 20 Hz main loop polls motion and
audio-capture health every 10 s and logs ERROR while either is down. (The dead
`storage.get_usage()` call running 20×/s for a commented-out log line was removed
from the same block.) **Verification:** new tests prove the loop retries instead
of dying, reopens on read failure, updates the liveness timestamp, and that
`is_healthy()` reflects thread death and frame staleness.

---

## Common thread and follow-ups

All five bugs share one shape: **a component fails while every surface signal
says "healthy."** None were catchable by lint or the existing tests, because the
tests either mocked the failure surface away (BUG-1) or pinned the buggy behavior
as expected (BUG-3).

Remaining related work (identified in the same review, not yet done):

1. Surface health failures to the *user-visible* channels (overlay `ALERT_ERROR`
   state on the dynamic pipe, `StreamWatchdog` notifier), not just ERROR logs —
   the go2rtc composite does not show the legacy status overlay.
2. Test files for `watchdog.py` (31% coverage), `notifier.py` (45%),
   `recording.py`, `sources.py`.
3. Restart-counter decay in `capture.py` (10 transient failures over weeks still
   permanently stop restarts).
4. Documentation staleness batch (README framerate value, SETUP_RPI5 flow,
   user_guide defaults).

## Final verification

- `make test`: **161 passed** (was 151), including 10 new regression tests.
- `make lint`: ruff + mypy strict clean.
- BUG-1 regression test confirmed failing against pre-fix code.
