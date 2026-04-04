"""Tests for the generic CycleRunner."""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steward.actions import ActionResult, Action
from steward.runner import CycleRunner


@pytest.fixture
def task_dir():
    """Create a minimal task directory with definition.yaml and state/."""
    d = tempfile.mkdtemp(prefix="runner_test_")
    root = Path(d)

    # Minimal definition
    definition = {
        "project_id": "test_project",
        "goal": "Test whether X causes Y",
        "scope_boundaries": {
            "in_scope": ["topic A", "topic B"],
            "out_of_scope": ["topic C"],
        },
        "actions": {
            "web_search": {"enabled": True},
        },
        "report": {
            "email_prefix": "[TEST]",
        },
    }
    (root / "definition.yaml").write_text(yaml.dump(definition))

    # State directory
    state_dir = root / "state"
    state_dir.mkdir()

    # Minimal status
    status = {
        "cycle_number": 0,
        "confidence_score": 1,
        "current_hypothesis": "X might cause Y",
        "key_findings": [],
        "open_questions": [{"question": "Does X cause Y?", "priority": "high"}],
        "action_suggestions": [],
    }
    with open(state_dir / "status.yaml", "w") as f:
        yaml.dump(status, f)

    # Task state
    task_state = {"cycle": 0, "days_since_reply": 0, "status": "active"}
    with open(state_dir / "task_state.json", "w") as f:
        json.dump(task_state, f)

    # Summary output directory
    summary_dir = root / "summaries"
    summary_dir.mkdir()

    yield root
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestRunnerConstruction:
    def test_create_with_paths(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        assert runner.definition["project_id"] == "test_project"
        assert runner.state_dir.exists()

    def test_create_loads_definition(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        assert runner.definition["goal"] == "Test whether X causes Y"

    def test_missing_definition_raises(self, task_dir):
        with pytest.raises(FileNotFoundError):
            CycleRunner(
                definition=task_dir / "nonexistent.yaml",
                state_dir=task_dir / "state",
                summary_dir=task_dir / "summaries",
            )


# ---------------------------------------------------------------------------
# Phase 1: load_state
# ---------------------------------------------------------------------------

class TestLoadState:
    def test_loads_yaml_files(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        assert "status.yaml" in context["state_files"]
        assert context["state_files"]["status.yaml"]["current_hypothesis"] == "X might cause Y"

    def test_loads_json_files(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        assert "task_state.json" in context["state_files"]
        assert context["state_files"]["task_state.json"]["cycle"] == 0

    def test_loads_jsonl_files(self, task_dir):
        log_path = task_dir / "state" / "exploration_log.jsonl"
        log_path.write_text('{"query": "test", "cycle": 1}\n{"query": "test2", "cycle": 2}\n')

        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        assert "exploration_log.jsonl" in context["state_files"]
        assert len(context["state_files"]["exploration_log.jsonl"]) == 2

    def test_loads_text_files(self, task_dir):
        (task_dir / "state" / "notes.md").write_text("# Some notes\nHello")

        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        assert "notes.md" in context["state_files"]
        assert "Hello" in context["state_files"]["notes.md"]

    def test_context_has_definition(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        assert context["definition"]["project_id"] == "test_project"

    def test_context_has_meta(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        assert "meta" in context
        assert "cycle" in context["meta"]

    def test_empty_state_dir(self, task_dir):
        empty_state = task_dir / "empty_state"
        empty_state.mkdir()

        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=empty_state,
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        assert context["state_files"] == {}


# ---------------------------------------------------------------------------
# Phase 2: plan
# ---------------------------------------------------------------------------

class TestPlan:
    def test_default_plan_returns_actions(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        actions = runner.plan(context)
        assert isinstance(actions, list)
        assert all(isinstance(a, Action) for a in actions)

    def test_default_plan_includes_web_search(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        actions = runner.plan(context)
        types = [a.action_type for a in actions]
        assert "web_search" in types or "lightweight_search" in types

    def test_custom_plan_override(self, task_dir):
        class MyRunner(CycleRunner):
            def plan(self, context):
                return [Action("custom", config={"key": "value"})]

        runner = MyRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        actions = runner.plan(context)
        assert len(actions) == 1
        assert actions[0].action_type == "custom"

    def test_disabled_actions_not_planned(self, task_dir):
        # Override definition to disable web_search
        defn = yaml.safe_load((task_dir / "definition.yaml").read_text())
        defn["actions"]["web_search"]["enabled"] = False
        (task_dir / "definition.yaml").write_text(yaml.dump(defn))

        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        actions = runner.plan(context)
        types = [a.action_type for a in actions]
        assert "web_search" not in types


# ---------------------------------------------------------------------------
# Phase 3: execute
# ---------------------------------------------------------------------------

class TestExecute:
    def test_execute_dispatches_to_built_in(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()

        mock_result = ActionResult(
            action_type="web_search", status="success",
            summary="found stuff", findings=[{"finding": "X"}],
            artifacts=[], duration_s=1.0,
        )
        with patch.object(runner, "_dispatch_action", return_value=mock_result):
            results = runner.execute([Action("web_search", query="test")], context)

        assert len(results) == 1
        assert results[0].status == "success"

    def test_execute_handles_action_failure(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()

        def failing_dispatch(action, context):
            raise RuntimeError("action exploded")

        with patch.object(runner, "_dispatch_action", side_effect=failing_dispatch):
            results = runner.execute([Action("web_search", query="test")], context)

        assert len(results) == 1
        assert results[0].status == "failed"
        assert "action exploded" in results[0].error

    def test_execute_custom_action(self, task_dir):
        class MyAction:
            action_type = "my_custom"
            def execute(self, action, context):
                return ActionResult(
                    action_type="my_custom", status="success",
                    summary="custom done", findings=[], artifacts=[],
                    duration_s=0.5,
                )

        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        runner.custom_actions["my_custom"] = MyAction()
        context = runner.load_state()

        results = runner.execute([Action("my_custom")], context)
        assert len(results) == 1
        assert results[0].action_type == "my_custom"
        assert results[0].status == "success"

    def test_execute_multiple_actions(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()

        call_count = 0
        def mock_dispatch(action, ctx):
            nonlocal call_count
            call_count += 1
            return ActionResult(
                action_type=action.action_type, status="success",
                summary=f"action {call_count}", findings=[], artifacts=[],
                duration_s=0.1,
            )

        with patch.object(runner, "_dispatch_action", side_effect=mock_dispatch):
            results = runner.execute(
                [Action("web_search"), Action("lightweight_search")], context
            )

        assert len(results) == 2
        assert call_count == 2


# ---------------------------------------------------------------------------
# Phase 5: report (summary writing)
# ---------------------------------------------------------------------------

class TestReport:
    def test_writes_summary_files(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        results = [
            ActionResult(
                action_type="web_search", status="success",
                summary="Found 3 papers on X", findings=[{"finding": "A is B"}],
                artifacts=[], duration_s=10.0,
            ),
        ]

        runner.report(context, results)

        summary_json = task_dir / "summaries" / "summary.json"
        summary_md = task_dir / "summaries" / "summary.md"
        assert summary_json.exists()
        assert summary_md.exists()

        data = json.loads(summary_json.read_text())
        assert data["status"] == "success"
        assert data["task_id"] == "test_project"
        assert len(data["tldr"]) > 0

    def test_summary_includes_artifacts(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        results = [
            ActionResult(
                action_type="experiment", status="success",
                summary="Ran probe", findings=[],
                artifacts=[{"path": "results/probe.json", "description": "probe data", "type": "data"}],
                duration_s=5.0,
            ),
        ]

        runner.report(context, results)
        data = json.loads((task_dir / "summaries" / "summary.json").read_text())
        assert len(data["artifacts"]) == 1
        assert data["artifacts"][0]["path"] == "results/probe.json"

    def test_failed_action_reflected_in_summary(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        results = [
            ActionResult(
                action_type="web_search", status="failed",
                summary="Search timed out", findings=[], artifacts=[],
                duration_s=120.0, error="timeout after 120s",
            ),
        ]

        runner.report(context, results)
        data = json.loads((task_dir / "summaries" / "summary.json").read_text())
        assert data["status"] == "failed"  # single failed action = overall failed
        assert len(data["errors"]) > 0

    def test_neutral_skip_does_not_degrade_summary(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        results = [
            ActionResult(
                action_type="bootstrap_check", status="skipped",
                summary="Nothing to do yet", findings=[], artifacts=[],
                duration_s=0.0,
            ),
            ActionResult(
                action_type="web_search", status="success",
                summary="Found 3 papers on X", findings=[], artifacts=[],
                duration_s=10.0,
            ),
        ]

        runner.report(context, results)
        data = json.loads((task_dir / "summaries" / "summary.json").read_text())
        assert data["status"] == "success"
        assert data["action_results"][0]["blocking"] is False

    def test_blocking_skip_still_degrades_summary(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        results = [
            ActionResult(
                action_type="manual_gate", status="skipped",
                summary="Waiting on required input", findings=[], artifacts=[],
                duration_s=0.0, metadata={"blocking": True},
            ),
            ActionResult(
                action_type="web_search", status="success",
                summary="Found 3 papers on X", findings=[], artifacts=[],
                duration_s=10.0,
            ),
        ]

        runner.report(context, results)
        data = json.loads((task_dir / "summaries" / "summary.json").read_text())
        assert data["status"] == "partial"
        assert data["action_results"][0]["blocking"] is True


# ---------------------------------------------------------------------------
# Phase 6: save_state
# ---------------------------------------------------------------------------

class TestSaveState:
    def test_increments_cycle(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()
        assert context["meta"]["cycle"] == 0

        runner.save_state(context, [])
        # Reload and check
        context2 = runner.load_state()
        assert context2["meta"]["cycle"] == 1

    def test_appends_to_log(self, task_dir):
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )
        context = runner.load_state()

        results = [
            ActionResult(
                action_type="web_search", status="success",
                summary="found stuff", findings=[{"finding": "A"}],
                artifacts=[], duration_s=1.0,
            ),
        ]
        runner.save_state(context, results)

        log_path = task_dir / "state" / "exploration_log.jsonl"
        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().strip().split("\n") if l.strip()]
        assert len(lines) >= 1


# ---------------------------------------------------------------------------
# Full cycle (integration)
# ---------------------------------------------------------------------------

class TestRunCycle:
    def test_full_cycle_with_mocked_actions(self, task_dir):
        """End-to-end: load → plan → execute → summarize → report → save."""
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=task_dir / "summaries",
        )

        mock_result = ActionResult(
            action_type="web_search", status="success",
            summary="Found 2 results",
            findings=[{"finding": "X causes Y", "source": "http://example.com"}],
            artifacts=[], duration_s=5.0,
        )

        with patch.object(runner, "_dispatch_action", return_value=mock_result):
            runner.run_cycle()

        # Verify outputs
        assert (task_dir / "summaries" / "summary.json").exists()
        assert (task_dir / "summaries" / "summary.md").exists()

        data = json.loads((task_dir / "summaries" / "summary.json").read_text())
        assert data["status"] == "success"

        # Verify state was updated
        context = runner.load_state()
        assert context["meta"]["cycle"] == 1
