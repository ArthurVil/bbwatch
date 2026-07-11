"""Pipeline latency instrumentation.

Each pipeline pass creates a StageTimer, marks named stages as it goes,
and hands the result to a LatencyTracker. The tracker logs every pass at
DEBUG and an aggregated summary (avg/max per stage) at INFO on a fixed
interval, so production logs show latency without per-event spam.
"""

import logging
import time

LOGGER = logging.getLogger(__name__)


class StageTimer:
    """Measures named stage durations within one pipeline pass.

    Args:
        origin_ts: Optional wall-clock start of the measured event (e.g.
            the segment file's mtime). When set, a "pickup" stage holding
            the delay between origin and timer creation is recorded first.
    """

    def __init__(self, origin_ts: float | None = None) -> None:
        if origin_ts is not None:
            self.stages: dict[str, float] = {"pickup": (time.time() - origin_ts) * 1000.0}
        else:
            self.stages = {}
        self._last = time.perf_counter()

    def mark(self, stage: str) -> None:
        """Record the time elapsed since the previous mark as `stage`."""
        now = time.perf_counter()
        self.stages[stage] = (now - self._last) * 1000.0
        self._last = now

    def total_ms(self) -> float:
        """Sum of all recorded stage durations."""
        return sum(self.stages.values())


class LatencyTracker:
    """Aggregates stage latencies for one pipeline and logs summaries.

    Not thread-safe by design: each pipeline owns one tracker and records
    from a single thread.
    """

    def __init__(self, pipeline: str, report_interval_s: float = 10.0) -> None:
        """Initialize tracker.

        Args:
            pipeline: Name used in log lines (e.g. "audio", "motion").
            report_interval_s: Seconds between aggregated INFO summaries.
        """
        self.pipeline = pipeline
        self.report_interval_s = report_interval_s
        self._window: dict[str, list[float]] = {}
        self._count = 0
        self._last_report = time.time()

    def record(self, timer: StageTimer) -> None:
        """Add one pass's stage timings; emit logs as configured."""
        for stage, ms in timer.stages.items():
            self._window.setdefault(stage, []).append(ms)
        self._count += 1

        if LOGGER.isEnabledFor(logging.DEBUG):
            detail = " ".join(f"{k}={v:.1f}ms" for k, v in timer.stages.items())
            LOGGER.debug(f"latency {self.pipeline} total={timer.total_ms():.1f}ms {detail}")

        self._maybe_report()

    def _maybe_report(self) -> None:
        now = time.time()
        if now - self._last_report < self.report_interval_s or not self._window:
            return

        parts = []
        for stage, values in self._window.items():
            avg = sum(values) / len(values)
            parts.append(f"{stage}=avg {avg:.1f}/max {max(values):.1f}ms")

        LOGGER.info(f"LATENCY {self.pipeline} n={self._count} " + " | ".join(parts))
        self._window.clear()
        self._count = 0
        self._last_report = now
