# Architecture Rules

bbwatch is a single Python package (`bbwatch/`) orchestrated by `BabyMonitor` in `main.py`. Keep the boundaries below intact.

## Protocol-Based Hardware Boundaries

Hardware access goes through `typing.Protocol` interfaces with swappable implementations:

| Protocol | Module | Implementations |
|---|---|---|
| `AudioSource` / `VideoSource` | `sources.py` | ALSA, RTSP, V4L2, RPiCamera, Mock |
| `Recorder` | `recording.py` | `FFmpegRecorder` |
| `Notifier` | `notifier.py` | `NtfyNotifier`, none |

**Rules:**

1. New hardware or I/O backends implement an existing Protocol (or add a new one) — never scatter `subprocess`/`cv2.VideoCapture` calls through business logic.
2. DSP and decision logic (`detector.py` signal pipeline, `AlertManager` hysteresis) must stay pure: numpy/scipy in, dataclasses out, no device or subprocess imports.
3. Every Protocol gets a Mock implementation so unit tests and `--fake-hardware` mode work without devices.
4. Components communicate through `BabyMonitor` wiring, not by importing each other's internals. If module A needs module B's state, pass it via a callback or a shared value object set up in `main.py`.

## Configuration

- All tunables live in `config.py` Pydantic models with `Field` constraints (ranges, defaults) — never hardcode thresholds, FPS values, or paths in component code.
- Remember the cross-file invariant: `alerts.overlay_fps` in `config.yaml` must match `-framerate` in `docker/go2rtc.yaml`.

## Threading Model

- Long-running work runs in dedicated background threads owned by their component (`start()` / `stop()` with a `threading.Event`). The main loop stays a fast 20Hz poll — never do blocking I/O in it.
- Shared state between threads is limited to simple atomic reads (floats, bools, timestamps) or explicitly locked structures. Document any new shared state at its declaration.
