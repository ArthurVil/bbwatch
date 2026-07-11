# Testing Standards

## Test Structure

```
tests/
├── unit/          # Fast, no hardware — mock all devices, subprocesses, threads
├── integration/   # capture + detector pipeline with generated audio fixtures
├── functional/    # End-to-end BabyMonitor flow (fake hardware)
└── fixtures/      # Shared test data (generated WAVs, sample device listings)
```

## Commands

```bash
make test                                            # All tests + coverage
make test-unit                                       # Unit only (fast pre-commit check)
poetry run pytest tests/unit/test_detector.py -v     # Single file
poetry run pytest tests/ -k "test_name" -v           # Single test
poetry run pytest tests/ -m "not slow" -v            # Skip slow tests
```

## Rules

- **Unit tests must never touch hardware.** Mock ALSA/V4L2/libcamera subprocess calls, FFmpeg, RTSP, and the filesystem where practical. Use `fake_hardware=True` / `HardwareDetector(fake=True)` for component wiring.
- **Test device-detection parsing against captured real output.** The regexes in `hardware.py` parse `arecord -l`, `v4l2-ctl`, and `libcamera-hello` output — feed them verbatim samples in fixtures, not hand-simplified strings.
- **Integration tests generate audio, never record it.** Synthesize cry-band (250–2000 Hz) and noise signals with numpy/scipy; assert on `DetectionResult`.
- **Every bug fix ships with a regression test** reproducing the original failure.
- **Failure paths are first-class**: test what happens when a device vanishes, FFmpeg exits non-zero, the FIFO has no reader, or RTSP is unreachable (see `rules/reliability.md`).
- Thread-based components (`MotionDetector`, `StorageManager`, `StreamWatchdog`) are tested by calling their internal step methods directly, not by spinning real threads with sleeps. If a real thread is unavoidable, bound waits with events/timeouts — never bare `time.sleep()` assertions.
- Mark tests slower than ~1s with `@pytest.mark.slow`.
