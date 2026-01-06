"""Unit tests for AlertManager state machine."""

import json
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from bbwatch.alert import AlertManager, AlertState, AlertStatus
from bbwatch.config import AlertConfig


class TestAlertManager:
    """Tests for AlertManager state logic."""

    @pytest.fixture
    def config(self, tmp_path):
        """Test alert configuration."""
        return AlertConfig(
            status_file=tmp_path / "status.json",
            trigger_high=0.1,
            trigger_low=0.05,
            cooldown_s=1.0,  # Short cooldown for testing
        )

    @pytest.fixture
    def manager(self, config):
        """AlertManager instance."""
        return AlertManager(config)

    def test_initial_state(self, manager):
        """Should start in IDLE state."""
        assert manager.current_state == AlertState.IDLE

    def test_trigger_logic(self, manager):
        """Should trigger when intensity exceeds high threshold."""
        # Below threshold -> IDLE
        status = manager.process_intensity(0.05)
        assert manager.current_state == AlertState.IDLE
        assert status.alert_active is False

        # Above threshold -> TRIGGERED
        status = manager.process_intensity(0.12)
        assert manager.current_state == AlertState.TRIGGERED
        assert status.alert_active is True
        assert status.intensity == 0.12

    def test_hysteresis_logic(self, manager):
        """Should stay triggered until intensity drops below low threshold."""
        # Trigger
        manager.process_intensity(0.12)
        assert manager.current_state == AlertState.TRIGGERED

        # Between thresholds -> Still TRIGGERED
        manager.process_intensity(0.08)
        assert manager.current_state == AlertState.TRIGGERED

        # Below low threshold -> COOLDOWN
        status = manager.process_intensity(0.04)
        assert manager.current_state == AlertState.COOLDOWN
        assert status.alert_active is False  # Alert clears visually in cooldown

    def test_cooldown_logic(self, manager):
        """Should return to IDLE after cooldown."""
        # Enter cooldown
        manager.process_intensity(0.12)  # Trigger
        manager.process_intensity(0.04)  # Cooldown
        assert manager.current_state == AlertState.COOLDOWN

        # Wait for cooldown
        time.sleep(1.1)

        # Process low intensity -> IDLE
        manager.process_intensity(0.02)
        assert manager.current_state == AlertState.IDLE

    def test_retrigger_during_cooldown(self, manager):
        """Should re-trigger immediately if high intensity occurs during cooldown."""
        # Enter cooldown
        manager.process_intensity(0.12)
        manager.process_intensity(0.04)
        assert manager.current_state == AlertState.COOLDOWN

        # High intensity -> TRIGGERED
        manager.process_intensity(0.15)
        assert manager.current_state == AlertState.TRIGGERED

    def test_status_file_written(self, manager, config):
        """Status file should be updated on every process call."""
        manager.process_intensity(0.12)

        assert config.status_file.exists()
        data = json.loads(config.status_file.read_text())
        assert data["alert_state"] == "triggered"
        assert data["intensity"] == 0.12
