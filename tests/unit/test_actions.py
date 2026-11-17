"""Unit tests for AlertActions: alert side effects chosen at startup."""

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bbwatch.actions import AlertActions
from bbwatch.alert import AlertManager, AlertState
from bbwatch.config import AlertActionsConfig, AlertConfig, AlertDestination, BBWatchConfig
from bbwatch.recording import MockRecorder

ROOT = Path(__file__).resolve().parents[2]


def make_config(tmp_path: Path, **actions: object) -> AlertConfig:
    return AlertConfig(
        status_file=tmp_path / "status.json",
        clips_dir=tmp_path / "clips",
        screenshots_dir=tmp_path / "screenshots",
        outbox_dir=tmp_path / "outbox",
        trigger_high=0.1,
        trigger_low=0.05,
        cooldown_s=0.0,
        record_min_s=0.0,
        actions=AlertActionsConfig(**actions),
    )


class FileWritingRecorder(MockRecorder):
    """MockRecorder that also writes the files, so links/copies can be checked."""

    def capture_frame(self, output_path: Path) -> None:
        super().capture_frame(output_path)
        output_path.write_bytes(b"jpg")

    def start_recording(self, output_path: Path, max_duration_s: float, on_saved=None) -> None:
        super().start_recording(output_path, max_duration_s, on_saved=on_saved)
        output_path.write_bytes(b"mp4")


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


class TestDefaults:
    def test_defaults_are_overlay_only(self) -> None:
        actions = AlertActionsConfig()
        assert actions.screenshot is False
        assert actions.clip is False
        assert actions.destination == AlertDestination.LOCAL

    def test_shipped_config_is_overlay_only(self) -> None:
        cfg = BBWatchConfig.from_yaml(ROOT / "config.yaml")
        assert cfg.alerts.actions == AlertActionsConfig()

    def test_env_override_at_startup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BBWATCH_ALERTS__ACTIONS__CLIP", "true")
        monkeypatch.setenv("BBWATCH_ALERTS__ACTIONS__DESTINATION", "cloud")
        cfg = BBWatchConfig()
        assert cfg.alerts.actions.clip is True
        assert cfg.alerts.actions.destination == AlertDestination.CLOUD

    def test_unknown_destination_rejected(self) -> None:
        with pytest.raises(ValueError):
            AlertActionsConfig(destination="dropbox")  # type: ignore[arg-type]


class TestOverlayOnly:
    def test_no_recorder_created(self, tmp_path: Path) -> None:
        actions = AlertActions(make_config(tmp_path))
        assert actions._recorder is None
        assert actions.describe() == "overlay only (no screenshots, no clips)"

    def test_alert_still_triggers_but_writes_nothing(self, tmp_path: Path) -> None:
        recorder = MockRecorder()
        manager = AlertManager(make_config(tmp_path), recorder=recorder)

        status = manager.process_intensity(0.2)
        manager.process_intensity(0.0)
        manager.process_intensity(0.0)
        manager.shutdown()

        assert status.alert_active is True  # overlay still shows the alert
        assert manager.current_state == AlertState.IDLE
        assert recorder.start_recording_calls == []
        assert recorder.capture_frame_calls == []
        assert recorder.stop_recording_calls == 0
        assert files(tmp_path / "clips") == files(tmp_path / "screenshots") == []

    def test_recording_error_ignored_when_clips_off(self, tmp_path: Path) -> None:
        recorder = MockRecorder()
        recorder.error = "stale"
        actions = AlertActions(make_config(tmp_path, screenshot=True), recorder=recorder)
        assert actions.recording_error is None


class TestScreenshotOnly:
    def test_screenshot_without_clip(self, tmp_path: Path) -> None:
        recorder = FileWritingRecorder()
        manager = AlertManager(make_config(tmp_path, screenshot=True), recorder=recorder)

        manager.process_intensity(0.2)

        assert wait_for(lambda: len(recorder.capture_frame_calls) == 1)
        assert recorder.start_recording_calls == []


class TestDestination:
    def test_local(self, tmp_path: Path) -> None:
        recorder = FileWritingRecorder()
        manager = AlertManager(make_config(tmp_path, screenshot=True, clip=True), recorder=recorder)

        manager.process_intensity(0.2)
        assert wait_for(lambda: len(files(tmp_path / "screenshots")) == 1)
        manager.shutdown()

        clip, _ = recorder.start_recording_calls[0]
        assert clip.parent.parent == tmp_path / "clips"
        assert files(tmp_path / "outbox") == []

    def test_cloud_writes_only_to_outbox(self, tmp_path: Path) -> None:
        recorder = FileWritingRecorder()
        manager = AlertManager(
            make_config(tmp_path, screenshot=True, clip=True, destination="cloud"), recorder=recorder
        )

        manager.process_intensity(0.2)
        assert wait_for(lambda: len(files(tmp_path / "outbox" / "screenshots")) == 1)
        manager.shutdown()

        clip, _ = recorder.start_recording_calls[0]
        assert clip.parent.parent == tmp_path / "outbox" / "clips"
        assert files(tmp_path / "clips") == files(tmp_path / "screenshots") == []

    def test_both_keeps_local_and_links_into_outbox(self, tmp_path: Path) -> None:
        recorder = FileWritingRecorder()
        manager = AlertManager(make_config(tmp_path, screenshot=True, clip=True, destination="both"), recorder=recorder)

        manager.process_intensity(0.2)
        assert wait_for(lambda: len(files(tmp_path / "outbox" / "screenshots")) == 1)
        clip, _ = recorder.start_recording_calls[0]
        assert files(tmp_path / "outbox" / "clips") == []  # not queued while still recording
        manager.shutdown()  # clip finalized -> on_saved

        queued = tmp_path / "outbox" / "clips" / clip.relative_to(tmp_path / "clips")
        assert queued.exists()
        assert queued.stat().st_ino == clip.stat().st_ino  # hard link, no extra space
        queued.unlink()  # what the uploader's rclone move does
        assert clip.exists()

        shot = files(tmp_path / "screenshots")[0]
        assert files(tmp_path / "outbox" / "screenshots")[0].stat().st_ino == shot.stat().st_ino

    def test_describe(self, tmp_path: Path) -> None:
        actions = AlertActions(
            make_config(tmp_path, screenshot=True, clip=True, destination="both"), recorder=MockRecorder()
        )
        assert actions.describe() == "overlay + screenshots + clips -> both"


class TestFailures:
    def test_failed_screenshot_is_not_queued(self, tmp_path: Path) -> None:
        recorder = MagicMock()
        recorder.capture_frame.side_effect = FileNotFoundError("ffmpeg")
        actions = AlertActions(make_config(tmp_path, screenshot=True, destination="both"), recorder=recorder)

        actions.on_trigger(0.0)

        assert wait_for(lambda: recorder.capture_frame.called)
        time.sleep(0.05)
        assert files(tmp_path / "outbox") == []

    def test_queue_failure_is_logged_not_raised(self, tmp_path: Path) -> None:
        actions = AlertActions(make_config(tmp_path, clip=True, destination="both"), recorder=MockRecorder())
        missing = tmp_path / "clips" / "2026-11-17" / "gone.mp4"
        actions._link_clip_to_outbox(missing)  # must not raise
        assert files(tmp_path / "outbox") == []
