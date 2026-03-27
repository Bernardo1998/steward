"""Tests for charter-status command."""

import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import pytest
import yaml

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from status_cmd import collect_status, format_table, format_markdown


@pytest.fixture
def instance_dir():
    """Create a minimal instance directory with one task."""
    d = tempfile.mkdtemp(prefix="charter_status_test_")
    root = Path(d)

    # Registry
    tasks_dir = root / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "registry.yaml").write_text(
        'version: 2\ntasks:\n'
        '  - id: "test_task"\n    enabled: true\n    path: "tasks/test_task"\n'
        '  - id: "disabled_task"\n    enabled: false\n    path: "tasks/disabled_task"\n'
    )

    # Task with charter
    task_dir = tasks_dir / "test_task"
    task_dir.mkdir()
    (task_dir / "state").mkdir()
    (task_dir / "charter.yaml").write_text(
        'task_id: test_task\nname: "Test Task"\n'
        'schedule:\n  frequency: "daily"\n  max_runtime_minutes: 5\n'
        'execution:\n  agent: "direct"\n  entrypoint: "python run.py"\n'
    )

    # Disabled task (no charter needed — just registry entry)
    dis_dir = tasks_dir / "disabled_task"
    dis_dir.mkdir()
    (dis_dir / "charter.yaml").write_text(
        'task_id: disabled_task\nname: "Disabled"\n'
        'schedule:\n  frequency: "daily"\n'
        'execution:\n  agent: "codex"\n'
    )

    yield root
    shutil.rmtree(d, ignore_errors=True)


class TestCollectStatus:
    def test_basic_status(self, instance_dir):
        status = collect_status(instance_dir)
        assert status["instance_root"] == str(instance_dir)
        assert len(status["tasks"]) == 2
        assert "timestamp" in status

    def test_task_fields(self, instance_dir):
        status = collect_status(instance_dir)
        test_task = next(t for t in status["tasks"] if t["id"] == "test_task")
        assert test_task["enabled"] is True
        assert test_task["mode"] == "direct"
        assert test_task["frequency"] == "daily"
        assert test_task["status"] == "unknown"  # no summary yet
        assert test_task["locked"] is False

    def test_disabled_task(self, instance_dir):
        status = collect_status(instance_dir)
        dis = next(t for t in status["tasks"] if t["id"] == "disabled_task")
        assert dis["enabled"] is False
        assert dis["status"] == "disabled"

    def test_with_summary(self, instance_dir):
        today = datetime.now().strftime("%Y-%m-%d")
        summary_dir = instance_dir / "daily_summaries" / today / "tasks" / "test_task"
        summary_dir.mkdir(parents=True)
        (summary_dir / "summary.json").write_text(json.dumps({
            "task_id": "test_task",
            "date": today,
            "status": "success",
            "tldr": ["Found 42 items"],
            "errors": [],
        }))

        status = collect_status(instance_dir)
        test_task = next(t for t in status["tasks"] if t["id"] == "test_task")
        assert test_task["status"] == "success"
        assert test_task["summary_date"] == today
        assert test_task["tldr"] == ["Found 42 items"]

    def test_with_errors_in_summary(self, instance_dir):
        today = datetime.now().strftime("%Y-%m-%d")
        summary_dir = instance_dir / "daily_summaries" / today / "tasks" / "test_task"
        summary_dir.mkdir(parents=True)
        (summary_dir / "summary.json").write_text(json.dumps({
            "task_id": "test_task",
            "date": today,
            "status": "partial",
            "tldr": [],
            "errors": [{"type": "research", "message": "Timeout after 900s"}],
        }))

        status = collect_status(instance_dir)
        test_task = next(t for t in status["tasks"] if t["id"] == "test_task")
        assert test_task["status"] == "partial"
        assert len(test_task["errors"]) == 1

    def test_with_lock(self, instance_dir):
        lock_path = instance_dir / "tasks" / "test_task" / ".lock"
        lock_path.write_text(json.dumps({
            "pid": 12345,
            "started_at": "2026-03-26T10:00:00",
        }))

        status = collect_status(instance_dir)
        test_task = next(t for t in status["tasks"] if t["id"] == "test_task")
        assert test_task["locked"] is True
        assert test_task["status"] == "running"

    def test_with_orchestrator_state(self, instance_dir):
        (instance_dir / "orchestrator_state.json").write_text(json.dumps({
            "task_runs": {
                "test_task": {
                    "last_run": "2026-03-26T05:00:00",
                    "last_success_date": "2026-03-26",
                }
            },
            "last_digest_date": "2026-03-26",
        }))

        status = collect_status(instance_dir)
        test_task = next(t for t in status["tasks"] if t["id"] == "test_task")
        assert test_task["last_run"] == "2026-03-26T05:00"
        assert test_task["last_success"] == "2026-03-26"
        assert status["orchestrator_state"]["last_digest_date"] == "2026-03-26"

    def test_missing_registry(self, tmp_path):
        status = collect_status(tmp_path)
        assert "error" in status
        assert "No registry" in status["error"]


class TestFormatTable:
    def test_basic_output(self, instance_dir):
        status = collect_status(instance_dir)
        output = format_table(status)
        assert "test_task" in output
        assert "disabled_task" in output
        assert "Charter-worker status" in output

    def test_error_display(self, instance_dir):
        status = collect_status(instance_dir)
        # Inject an error
        status["tasks"][0]["errors"] = [{"message": "Something broke"}]
        output = format_table(status)
        assert "Something broke" in output

    def test_no_tasks(self, instance_dir):
        status = {"instance_root": "/tmp", "tasks": [], "timestamp": "now"}
        output = format_table(status)
        assert "No tasks registered" in output


class TestFormatMarkdown:
    def test_markdown_output(self, instance_dir):
        status = collect_status(instance_dir)
        md = format_markdown(status)
        assert md.startswith("# Charter-worker Status")
        assert "| test_task" in md
        assert "| disabled_task" in md

    def test_markdown_has_table_header(self, instance_dir):
        status = collect_status(instance_dir)
        md = format_markdown(status)
        assert "| Task | Mode | Freq | Status |" in md
