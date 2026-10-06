"""Unit tests for pipeline latency instrumentation."""

import logging
import time

import pytest

from bbwatch.latency import LatencyTracker, StageTimer


@pytest.fixture
def log_capture():
    """Capture bbwatch.latency records with a plain handler.

    pytest's caplog fixture is unreliable in this environment (third-party
    pytest plugins interfere with the logging plugin), so attach our own.
    """
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("bbwatch.latency")
    handler = _Collector(level=logging.DEBUG)
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield records
    logger.removeHandler(handler)
    logger.setLevel(old_level)


class TestStageTimer:
    def test_marks_record_positive_durations_in_order(self):
        timer = StageTimer()
        time.sleep(0.01)
        timer.mark("first")
        timer.mark("second")

        assert list(timer.stages) == ["first", "second"]
        assert timer.stages["first"] >= 10.0  # slept 10ms
        assert timer.stages["second"] < timer.stages["first"]
        assert timer.total_ms() == sum(timer.stages.values())

    def test_origin_ts_records_pickup_delay(self):
        origin = time.time() - 0.05  # event happened 50ms ago
        timer = StageTimer(origin_ts=origin)

        assert "pickup" in timer.stages
        assert 40.0 <= timer.stages["pickup"] <= 500.0

    def test_no_origin_no_pickup_stage(self):
        assert "pickup" not in StageTimer().stages


class TestLatencyTracker:
    def test_debug_line_per_record(self, log_capture):
        tracker = LatencyTracker("audio", report_interval_s=3600.0)
        timer = StageTimer()
        timer.mark("dsp")

        tracker.record(timer)

        messages = [r.getMessage() for r in log_capture if r.levelno == logging.DEBUG]
        assert any("latency audio" in m and "dsp=" in m for m in messages)

    def test_summary_logged_after_interval(self, log_capture):
        tracker = LatencyTracker("motion", report_interval_s=0.0)  # report immediately
        tracker._last_report = time.time() - 1.0

        timer = StageTimer()
        timer.mark("process")

        tracker.record(timer)

        summaries = [r.getMessage() for r in log_capture if "LATENCY motion" in r.getMessage()]
        assert len(summaries) == 1
        assert "n=1" in summaries[0]
        assert "process=avg" in summaries[0]
        # Window resets after the summary
        assert tracker._window == {}
        assert tracker._count == 0

    def test_no_summary_before_interval(self, log_capture):
        tracker = LatencyTracker("overlay", report_interval_s=3600.0)
        timer = StageTimer()
        timer.mark("render")

        tracker.record(timer)

        assert not any("LATENCY" in r.getMessage() for r in log_capture)
        assert tracker._count == 1  # still accumulating

    def test_track_drops_reports_count_and_rate(self, log_capture):
        """A tracker opted into drop tracking must surface it in the summary."""
        tracker = LatencyTracker("overlay", report_interval_s=3600.0, track_drops=True)

        # Accumulate 3 passes without triggering a report yet.
        for dropped in (False, True, True):
            timer = StageTimer()
            timer.mark("render")
            tracker.record(timer, dropped=dropped)

        # Force the next record() to cross the report interval.
        tracker._last_report = time.time() - 4000.0
        timer = StageTimer()
        timer.mark("render")
        tracker.record(timer, dropped=False)

        summaries = [r.getMessage() for r in log_capture if "LATENCY overlay" in r.getMessage()]
        assert len(summaries) == 1
        assert "dropped=2/4 (50.0%)" in summaries[0]
        # Drop count resets alongside the window after reporting.
        assert tracker._dropped == 0

    def test_track_drops_false_never_emits_dropped_segment(self, log_capture):
        """Pipelines that never opt in must not grow an always-zero segment,
        even if a caller mistakenly passes dropped=True.
        """
        tracker = LatencyTracker("motion", report_interval_s=0.0)
        tracker._last_report = time.time() - 1.0
        timer = StageTimer()
        timer.mark("process")

        tracker.record(timer, dropped=True)

        summaries = [r.getMessage() for r in log_capture if "LATENCY motion" in r.getMessage()]
        assert len(summaries) == 1
        assert "dropped=" not in summaries[0]
