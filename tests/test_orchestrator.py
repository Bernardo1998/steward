"""Tests for orchestrator core functions."""

import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from charter_worker import orchestrator


@pytest.fixture
def instance_dir():
    """Create a minimal instance directory."""
    d = tempfile.mkdtemp(prefix="charter_orch_test_")
    root = Path(d)
    (root / "tasks").mkdir()
    yield root
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def setup_orchestrator(instance_dir):
    """Initialize orchestrator paths for the test instance."""
    orchestrator._init_paths(instance_dir)
    yield instance_dir


class TestLoadRegistry:
    def test_empty_registry(self, setup_orchestrator):
        (setup_orchestrator / "tasks" / "registry.yaml").write_text(
            "version: 2\ntasks: []\n"
        )
        tasks = orchestrator.load_registry()
        assert tasks == []

    def test_one_task(self, setup_orchestrator):
        (setup_orchestrator / "tasks" / "registry.yaml").write_text(
            'version: 2\ntasks:\n  - id: "t1"\n    enabled: true\n    path: "tasks/t1"\n'
        )
        tasks = orchestrator.load_registry()
        assert len(tasks) == 1
        assert tasks[0]["id"] == "t1"

    def test_disabled_filtered(self, setup_orchestrator):
        (setup_orchestrator / "tasks" / "registry.yaml").write_text(
            'version: 2\ntasks:\n'
            '  - id: "on"\n    enabled: true\n    path: "tasks/on"\n'
            '  - id: "off"\n    enabled: false\n    path: "tasks/off"\n'
        )
        tasks = orchestrator.load_registry()
        assert len(tasks) == 1
        assert tasks[0]["id"] == "on"

    def test_missing_registry(self, setup_orchestrator):
        tasks = orchestrator.load_registry()
        assert tasks == []


class TestLoadCharter:
    def test_valid_charter(self, setup_orchestrator):
        task_dir = setup_orchestrator / "tasks" / "t1"
        task_dir.mkdir(parents=True)
        (task_dir / "charter.yaml").write_text(
            'task_id: t1\nschedule:\n  frequency: "daily"\n'
            'execution:\n  agent: "direct"\n  entrypoint: "python run.py"\n'
        )
        charter = orchestrator.load_charter("tasks/t1")
        assert charter["task_id"] == "t1"
        assert charter["execution"]["agent"] == "direct"

    def test_missing_charter(self, setup_orchestrator):
        task_dir = setup_orchestrator / "tasks" / "t2"
        task_dir.mkdir(parents=True)
        charter = orchestrator.load_charter("tasks/t2")
        assert charter == {}  # empty dict, treated as falsy by orchestrator


class TestIsDue:
    def test_daily_not_run_today(self):
        assert orchestrator.is_due(
            {"frequency": "daily"},
            datetime(2026, 3, 26, 10, 0),
            "2026-03-25T10:00:00",
        )

    def test_daily_already_run_today(self):
        assert not orchestrator.is_due(
            {"frequency": "daily"},
            datetime(2026, 3, 26, 10, 0),
            "2026-03-26T05:00:00",
        )

    def test_hourly_due(self):
        assert orchestrator.is_due(
            {"frequency": "hourly"},
            datetime(2026, 3, 26, 10, 0),
            "2026-03-26T08:00:00",
        )

    def test_weekly_correct_day(self):
        # March 30, 2026 is a Monday
        assert orchestrator.is_due(
            {"frequency": "weekly", "run_day": "Monday"},
            datetime(2026, 3, 30, 10, 0),
            "2026-03-23T10:00:00",
        )

    def test_weekly_wrong_day(self):
        # March 26, 2026 is Thursday
        assert not orchestrator.is_due(
            {"frequency": "weekly", "run_day": "Monday"},
            datetime(2026, 3, 26, 10, 0),
            "2026-03-23T10:00:00",
        )

    def test_never_run(self):
        assert orchestrator.is_due(
            {"frequency": "daily"},
            datetime(2026, 3, 26, 10, 0),
            "",
        )


class TestLockManagement:
    def test_create_and_check_lock(self, setup_orchestrator):
        """Lock with current process PID should be detected as locked."""
        task_dir = setup_orchestrator / "tasks" / "t1"
        task_dir.mkdir(parents=True)
        # Use current process PID so os.kill(pid, 0) succeeds
        orchestrator.create_lock("tasks/t1", os.getpid(), 60)
        assert orchestrator.is_locked("tasks/t1")

    def test_no_lock(self, setup_orchestrator):
        task_dir = setup_orchestrator / "tasks" / "t1"
        task_dir.mkdir(parents=True)
        assert not orchestrator.is_locked("tasks/t1")

    def test_remove_lock(self, setup_orchestrator):
        task_dir = setup_orchestrator / "tasks" / "t1"
        task_dir.mkdir(parents=True)
        orchestrator.create_lock("tasks/t1", os.getpid(), 60)
        orchestrator.remove_lock("tasks/t1")
        assert not orchestrator.is_locked("tasks/t1")

    def test_stale_lock_removed(self, setup_orchestrator):
        """Lock with dead PID should be treated as stale and removed."""
        task_dir = setup_orchestrator / "tasks" / "t1"
        task_dir.mkdir(parents=True)
        orchestrator.create_lock("tasks/t1", 99999999, 60)  # fake dead PID
        assert not orchestrator.is_locked("tasks/t1")


class TestState:
    def test_save_and_load(self, setup_orchestrator):
        state = {"task_runs": {"t1": {"last_run": "2026-03-26T10:00:00"}}}
        orchestrator.save_state(state)
        loaded = orchestrator.load_state()
        assert loaded["task_runs"]["t1"]["last_run"] == "2026-03-26T10:00:00"

    def test_default_state(self, setup_orchestrator):
        state = orchestrator.load_state()
        assert "task_runs" in state
