# Reliability & Security Rules

bbwatch is a **baby monitor**. A silent failure means parents believe they are monitoring their child when they are not. Reliability requirements here are stricter than for ordinary software.

## Fail Loudly, Never Silently

- Background threads (`MotionDetector`, `OverlayGenerator`, `StorageManager`, `StreamWatchdog`, `SlidingWindowCapture`) must never die silently. A crashed thread must be logged at ERROR and surfaced to the user (overlay error state and/or notifier).
- Never swallow exceptions with a bare `except: pass`. If an error is recoverable, log it with context and recover explicitly; if not, propagate so the health check sees it.
- Degrading to a `Mock*` source is acceptable only under `--fake-hardware` or in tests. In production, losing a hardware source must produce a visible alert, not a quiet fallback.
- The overlay's error state (`ALERT_ERROR`) and the `StreamWatchdog` → `Notifier` path are the user-facing "the monitor is broken" channels. Any new failure mode must feed one of them.

## Watchdog Coverage

- Every long-running component must be health-checkable: expose a liveness signal (last-frame timestamp, last-segment timestamp, thread `is_alive()`) that the main loop can poll.
- If you add a new background thread, wire its health into `BabyMonitor`'s main loop and document what happens when it stalls.
- Prefer timeouts on all blocking I/O (subprocess calls, RTSP reads, FIFO opens). An unbounded block is a silent failure.

## Security Posture: Local-Only

- bbwatch is local-network only. Never add code that sends audio, video, or images to third-party services. Push notifications (ntfy) carry text only — no media.
- Never commit secrets (ntfy auth tokens, go2rtc credentials) to `config.yaml` or anywhere in the repo; they belong in environment variables (`BBWATCH_` prefix).
- New network listeners or exposed ports require explicit justification and must default to binding local/LAN interfaces only.
- Validate all external input at the boundary (config values via Pydantic constraints, subprocess output via the parsing regexes in `hardware.py`).

## Testing the Failure Paths

- Every failure-handling branch (device disappears, FFmpeg dies, pipe reader gone, RTSP unreachable) needs a test. Happy-path-only coverage is insufficient for this codebase.
