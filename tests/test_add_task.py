"""Tests for charter-add-task: natural-language task creation."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from charter_worker.add_task import (
    classify_task,
    create_task,
    register_task,
    list_templates,
)


@pytest.fixture
def instance_dir():
    """Create a minimal instance with registry."""
    d = tempfile.mkdtemp(prefix="addtask_test_")
    root = Path(d)
    tasks_dir = root / "tasks"
    tasks_dir.mkdir()

    registry = {"version": 2, "tasks": []}
    with open(tasks_dir / "registry.yaml", "w") as f:
        yaml.dump(registry, f)

    yield root
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# TestClassifyTask
# ---------------------------------------------------------------------------

class TestClassifyTask:
    def test_returns_classification(self):
        mock_result = {
            "template": "ltt_thinker",
            "task_id": "paper_watch_llm_eval",
            "name": "Paper Watch: LLM Eval",
            "schedule": "daily",
            "execution_mode": "agent",
            "definition": {
                "project_id": "paper_watch_llm_eval",
                "goal": "Track new papers on LLM agent evaluation",
                "scope_boundaries": {
                    "in_scope": ["LLM evaluation", "agent benchmarks"],
                    "out_of_scope": ["training methods"],
                },
                "actions": {"web_search": {"enabled": True}},
            },
            "task_md_content": "# Paper Watch\n\n1. Search arXiv\n2. Summarize\n",
        }

        templates = ["hello_world", "ltt_thinker", "experiment_task"]
        with patch("charter_worker.add_task.call_llm_json", return_value=mock_result):
            result = classify_task("Track new papers on LLM agent eval", templates)

        assert result["template"] == "ltt_thinker"
        assert result["task_id"] == "paper_watch_llm_eval"
        assert result["schedule"] == "daily"

    def test_handles_llm_failure(self):
        templates = ["hello_world"]
        with patch("charter_worker.add_task.call_llm_json", side_effect=RuntimeError("LLM down")):
            with pytest.raises(RuntimeError):
                classify_task("Do something", templates)


# ---------------------------------------------------------------------------
# TestCreateTask
# ---------------------------------------------------------------------------

class TestCreateTask:
    def test_creates_task_directory(self, instance_dir):
        classification = {
            "template": "hello_world",
            "task_id": "test_task",
            "name": "Test Task",
            "schedule": "daily",
            "execution_mode": "direct",
            "definition": {
                "project_id": "test_task",
                "goal": "Test something",
            },
        }

        path = create_task(instance_dir, classification)
        assert path.exists()
        assert (path / "charter.yaml").exists()
        assert (path / "state").is_dir()

    def test_writes_charter_yaml(self, instance_dir):
        classification = {
            "template": "hello_world",
            "task_id": "my_task",
            "name": "My Task",
            "schedule": "weekly",
            "execution_mode": "direct",
            "definition": {},
        }

        path = create_task(instance_dir, classification)
        charter = yaml.safe_load((path / "charter.yaml").read_text())
        assert charter["task_id"] == "my_task"
        assert charter["schedule"]["frequency"] == "weekly"
        assert charter["execution"]["agent"] == "direct"

    def test_writes_definition_yaml(self, instance_dir):
        classification = {
            "template": "hello_world",
            "task_id": "research_task",
            "name": "Research Task",
            "schedule": "daily",
            "execution_mode": "agent",
            "definition": {
                "goal": "Find stuff about X",
                "scope_boundaries": {"in_scope": ["X"], "out_of_scope": ["Y"]},
            },
        }

        path = create_task(instance_dir, classification)
        assert (path / "definition.yaml").exists()
        defn = yaml.safe_load((path / "definition.yaml").read_text())
        assert defn["goal"] == "Find stuff about X"
        assert defn["project_id"] == "research_task"

    def test_writes_task_md(self, instance_dir):
        classification = {
            "template": "hello_world",
            "task_id": "agent_task",
            "name": "Agent Task",
            "schedule": "daily",
            "execution_mode": "agent",
            "task_md_content": "# Instructions\n\n1. Do the thing\n",
            "definition": {},
        }

        path = create_task(instance_dir, classification)
        assert (path / "task.md").exists()
        assert "Do the thing" in (path / "task.md").read_text()

    def test_agent_mode_sets_codex(self, instance_dir):
        classification = {
            "template": "hello_world",
            "task_id": "codex_task",
            "name": "Codex Task",
            "schedule": "daily",
            "execution_mode": "agent",
            "definition": {},
        }

        path = create_task(instance_dir, classification)
        charter = yaml.safe_load((path / "charter.yaml").read_text())
        assert charter["execution"]["agent"] == "codex"

    def test_duplicate_raises(self, instance_dir):
        classification = {
            "template": "hello_world",
            "task_id": "dup_task",
            "name": "Dup",
            "schedule": "daily",
            "execution_mode": "direct",
            "definition": {},
        }
        create_task(instance_dir, classification)
        with pytest.raises(FileExistsError):
            create_task(instance_dir, classification)


# ---------------------------------------------------------------------------
# TestRegisterTask
# ---------------------------------------------------------------------------

class TestRegisterTask:
    def test_appends_to_registry(self, instance_dir):
        register_task(instance_dir, "new_task", "tasks/new_task")

        with open(instance_dir / "tasks" / "registry.yaml") as f:
            registry = yaml.safe_load(f)

        ids = [t["id"] for t in registry["tasks"]]
        assert "new_task" in ids

    def test_no_duplicate(self, instance_dir):
        register_task(instance_dir, "task_a", "tasks/task_a")
        register_task(instance_dir, "task_a", "tasks/task_a")  # should not duplicate

        with open(instance_dir / "tasks" / "registry.yaml") as f:
            registry = yaml.safe_load(f)

        count = sum(1 for t in registry["tasks"] if t["id"] == "task_a")
        assert count == 1

    def test_creates_registry_if_missing(self, instance_dir):
        # Remove existing registry
        (instance_dir / "tasks" / "registry.yaml").unlink()

        register_task(instance_dir, "first_task", "tasks/first_task")

        assert (instance_dir / "tasks" / "registry.yaml").exists()
        with open(instance_dir / "tasks" / "registry.yaml") as f:
            registry = yaml.safe_load(f)
        assert len(registry["tasks"]) == 1


# ---------------------------------------------------------------------------
# TestAddTaskEndToEnd
# ---------------------------------------------------------------------------

class TestAddTaskEndToEnd:
    def test_full_flow(self, instance_dir):
        """Classify → create → register, verify task is complete."""
        mock_classification = {
            "template": "hello_world",
            "task_id": "reading_tracker",
            "name": "Reading Tracker",
            "schedule": "daily",
            "execution_mode": "direct",
            "definition": {
                "project_id": "reading_tracker",
                "goal": "Track my reading list and summarize weekly",
                "scope_boundaries": {"in_scope": ["reading"], "out_of_scope": []},
                "actions": {"web_search": {"enabled": False}},
            },
        }

        with patch("charter_worker.add_task.call_llm_json", return_value=mock_classification):
            classification = classify_task("Track my reading list", ["hello_world", "ltt_thinker"])

        task_path = create_task(instance_dir, classification)
        register_task(instance_dir, "reading_tracker", "tasks/reading_tracker")

        # Verify complete task structure
        assert (task_path / "charter.yaml").exists()
        assert (task_path / "definition.yaml").exists()
        assert (task_path / "state").is_dir()

        # Verify registered
        with open(instance_dir / "tasks" / "registry.yaml") as f:
            registry = yaml.safe_load(f)
        ids = [t["id"] for t in registry["tasks"]]
        assert "reading_tracker" in ids

        # Verify charter
        charter = yaml.safe_load((task_path / "charter.yaml").read_text())
        assert charter["execution"]["agent"] == "direct"
        assert charter["schedule"]["frequency"] == "daily"
