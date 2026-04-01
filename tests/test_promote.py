"""Tests for steward-promote: workflow crystallizer."""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steward.promote import (
    collect_promotion_data,
    analyze_promotion_readiness,
    generate_promoted_workflow,
    write_promotion_artifacts,
    apply_promotion,
    record_promotion_feedback,
    is_promotion_paused,
    get_prior_rejection,
    parse_promotion_feedback_from_reply,
    generate_promotion_digest_section,
)


@pytest.fixture
def task_instance():
    """Create a temp instance with an agent-mode task and fake cycle history."""
    d = tempfile.mkdtemp(prefix="promote_test_")
    root = Path(d)

    # Registry
    tasks_dir = root / "tasks"
    tasks_dir.mkdir()
    registry = {
        "version": 2,
        "tasks": [{"id": "my_agent_task", "enabled": True, "path": "tasks/my_agent_task"}],
    }
    with open(tasks_dir / "registry.yaml", "w") as f:
        yaml.dump(registry, f)

    # Task directory
    task_dir = tasks_dir / "my_agent_task"
    task_dir.mkdir()
    (task_dir / "state").mkdir()

    # Charter (agent mode)
    charter = {
        "task_id": "my_agent_task",
        "schedule": {"frequency": "daily", "max_runtime_minutes": 15},
        "execution": {"agent": "codex"},
        "report": {"digest": True},
    }
    with open(task_dir / "charter.yaml", "w") as f:
        yaml.dump(charter, f)

    # Task instructions
    (task_dir / "task.md").write_text(
        "# My Agent Task\n\n"
        "1. Read the inbox file\n"
        "2. Categorize items by priority\n"
        "3. Generate a summary report\n"
        "4. Write summary.md and summary.json\n"
    )

    # Fake cycle logs
    logs_dir = task_dir / "logs"
    logs_dir.mkdir()
    for days_ago in range(5):
        d_str = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        (logs_dir / f"cycle_{d_str}.log").write_text(
            f"[my_agent_task] Starting cycle {d_str}\n"
            f"Reading inbox.txt...\n"
            f"Found 3 items\n"
            f"Categorizing by priority...\n"
            f"Writing summary...\n"
            f"Done.\n"
        )
        (logs_dir / f"prompt_{d_str}.txt").write_text(
            f"You are a charter worker executing task my_agent_task. "
            f"Read task.md and execute one cycle. Today is {d_str}."
        )

    # Fake summaries
    for days_ago in range(5):
        d_str = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        summary_dir = root / "daily_summaries" / d_str / "tasks" / "my_agent_task"
        summary_dir.mkdir(parents=True)
        summary = {
            "task_id": "my_agent_task",
            "date": d_str,
            "status": "success",
            "tldr": ["Processed 3 inbox items"],
            "errors": [],
            "metadata": {"duration_s": 8.5},
        }
        with open(summary_dir / "summary.json", "w") as f:
            json.dump(summary, f)

    # Orchestrator state
    with open(root / "orchestrator_state.json", "w") as f:
        json.dump({
            "task_runs": {
                "my_agent_task": {
                    "last_success_date": datetime.now().strftime("%Y-%m-%d"),
                    "retry_count": 0,
                }
            }
        }, f)

    yield root
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# TestCollectPromotionData
# ---------------------------------------------------------------------------

class TestCollectPromotionData:
    def test_collects_charter(self, task_instance):
        data = collect_promotion_data(task_instance, "my_agent_task", last_n=3)
        assert data["charter"]["execution"]["agent"] == "codex"
        assert data["current_mode"] == "codex"

    def test_collects_task_md(self, task_instance):
        data = collect_promotion_data(task_instance, "my_agent_task")
        assert "Read the inbox file" in data["task_md"]

    def test_collects_cycle_logs(self, task_instance):
        data = collect_promotion_data(task_instance, "my_agent_task", last_n=3)
        assert len(data["cycle_logs"]) == 3
        assert "Reading inbox" in data["cycle_logs"][0]["content"]

    def test_collects_prompts(self, task_instance):
        data = collect_promotion_data(task_instance, "my_agent_task", last_n=3)
        assert len(data["prompts"]) == 3

    def test_collects_summaries(self, task_instance):
        data = collect_promotion_data(task_instance, "my_agent_task", last_n=3)
        assert len(data["summaries"]) == 3
        assert data["summaries"][0]["status"] == "success"

    def test_collects_retry_history(self, task_instance):
        data = collect_promotion_data(task_instance, "my_agent_task")
        assert data["retry_history"]["retry_count"] == 0

    def test_missing_task_raises(self, task_instance):
        with pytest.raises(FileNotFoundError):
            collect_promotion_data(task_instance, "nonexistent_task")

    def test_caps_log_content(self, task_instance):
        # Write a very large log
        logs_dir = task_instance / "tasks" / "my_agent_task" / "logs"
        today = datetime.now().strftime("%Y-%m-%d")
        (logs_dir / f"cycle_{today}.log").write_text("x" * 20000)

        data = collect_promotion_data(task_instance, "my_agent_task", last_n=1)
        assert len(data["cycle_logs"][0]["content"]) < 10000


# ---------------------------------------------------------------------------
# TestWritePromotionArtifacts
# ---------------------------------------------------------------------------

class TestWritePromotionArtifacts:
    def test_writes_report(self, task_instance):
        task_dir = task_instance / "tasks" / "my_agent_task"
        workflow = {
            "report_md": "# Report\n\nTest report content",
            "run_py_code": "print('hello')",
            "charter_yaml": "task_id: test\nexecution:\n  agent: direct\n",
        }
        write_promotion_artifacts(task_dir, workflow)

        assert (task_dir / "promotion_report.md").exists()
        assert "Test report content" in (task_dir / "promotion_report.md").read_text()

    def test_writes_generated_run(self, task_instance):
        task_dir = task_instance / "tasks" / "my_agent_task"
        workflow = {
            "report_md": "# Report",
            "run_py_code": "#!/usr/bin/env python3\nprint('hello')\n",
            "charter_yaml": None,
        }
        write_promotion_artifacts(task_dir, workflow)

        assert (task_dir / "run.py.generated").exists()
        assert "hello" in (task_dir / "run.py.generated").read_text()

    def test_writes_promoted_charter(self, task_instance):
        task_dir = task_instance / "tasks" / "my_agent_task"
        workflow = {
            "report_md": "# Report",
            "run_py_code": None,
            "charter_yaml": "task_id: test\nexecution:\n  agent: direct\n",
        }
        write_promotion_artifacts(task_dir, workflow)

        assert (task_dir / "charter.promoted.yaml").exists()

    def test_skips_none_values(self, task_instance):
        task_dir = task_instance / "tasks" / "my_agent_task"
        workflow = {"report_md": "# Report", "run_py_code": None, "charter_yaml": None}
        write_promotion_artifacts(task_dir, workflow)

        assert (task_dir / "promotion_report.md").exists()
        assert not (task_dir / "run.py.generated").exists()
        assert not (task_dir / "charter.promoted.yaml").exists()


# ---------------------------------------------------------------------------
# TestApplyPromotion
# ---------------------------------------------------------------------------

class TestApplyPromotion:
    def test_swaps_files(self, task_instance):
        task_dir = task_instance / "tasks" / "my_agent_task"

        # Create promotion artifacts
        (task_dir / "charter.promoted.yaml").write_text(
            "task_id: my_agent_task\nexecution:\n  agent: direct\n  entrypoint: python run.py\n"
        )
        (task_dir / "run.py.generated").write_text("print('promoted')\n")

        apply_promotion(task_dir)

        # Originals backed up
        assert (task_dir / "charter.yaml.pre-promote").exists()

        # Promoted files in place
        charter = yaml.safe_load((task_dir / "charter.yaml").read_text())
        assert charter["execution"]["agent"] == "direct"

        run_content = (task_dir / "run.py").read_text()
        assert "promoted" in run_content

        # Generated files cleaned up
        assert not (task_dir / "charter.promoted.yaml").exists()
        assert not (task_dir / "run.py.generated").exists()

    def test_missing_promoted_charter_raises(self, task_instance):
        task_dir = task_instance / "tasks" / "my_agent_task"
        with pytest.raises(FileNotFoundError):
            apply_promotion(task_dir)

    def test_backup_preserves_original(self, task_instance):
        task_dir = task_instance / "tasks" / "my_agent_task"
        original_charter = (task_dir / "charter.yaml").read_text()

        (task_dir / "charter.promoted.yaml").write_text("execution:\n  agent: direct\n")
        apply_promotion(task_dir)

        backup = (task_dir / "charter.yaml.pre-promote").read_text()
        assert backup == original_charter


# ---------------------------------------------------------------------------
# TestPromoteEndToEnd
# ---------------------------------------------------------------------------

class TestPromoteEndToEnd:
    def test_full_promote_with_mocked_llm(self, task_instance):
        """End-to-end: collect → analyze → generate → write."""
        data = collect_promotion_data(task_instance, "my_agent_task", last_n=3)

        # Mock analysis result
        mock_analysis = {
            "readiness": "ready",
            "task_pattern": "pipeline",
            "deterministic_steps": [
                {"step": "Read inbox.txt", "can_script": True, "how": "Path.read_text()"},
                {"step": "Write summary", "can_script": True, "how": "json.dump()"},
            ],
            "llm_required_steps": [
                {"step": "Categorize items", "why": "Needs judgment to assign priority"},
            ],
            "dependencies": ["inbox.txt"],
            "failure_risks": ["inbox.txt missing"],
            "recommended_mode": "direct",
            "estimated_cost_reduction": "90%",
            "estimated_attention_reduction": "No more agent drift; deterministic output",
            "rationale": "Task repeats the same 4 steps every cycle.",
        }

        # Mock workflow generation
        mock_workflow = {
            "run_py_code": "#!/usr/bin/env python3\nprint('generated')\n",
            "charter_yaml": "task_id: my_agent_task\nexecution:\n  agent: direct\n  entrypoint: python run.py\n",
            "notes": "Simple pipeline task.",
        }

        with patch("steward.promote.call_llm_json", side_effect=[mock_analysis, mock_workflow]):
            analysis = analyze_promotion_readiness(data)
            workflow = generate_promoted_workflow(data, analysis)

        assert analysis["readiness"] == "ready"
        assert workflow["run_py_code"] is not None
        assert "Promotion Report" in workflow["report_md"]
        assert "90%" in workflow["report_md"]

        # Write artifacts
        task_dir = Path(data["task_dir"])
        write_promotion_artifacts(task_dir, workflow)

        assert (task_dir / "promotion_report.md").exists()
        assert (task_dir / "run.py.generated").exists()
        assert (task_dir / "charter.promoted.yaml").exists()

    def test_not_ready_produces_report_only(self, task_instance):
        data = collect_promotion_data(task_instance, "my_agent_task", last_n=3)

        mock_analysis = {
            "readiness": "not_ready",
            "task_pattern": "pipeline",
            "deterministic_steps": [],
            "llm_required_steps": [
                {"step": "Everything", "why": "Task is too variable"},
            ],
            "dependencies": [],
            "failure_risks": [],
            "recommended_mode": "agent",
            "estimated_cost_reduction": "0%",
            "estimated_attention_reduction": "None",
            "rationale": "Every cycle does something different.",
        }

        with patch("steward.promote.call_llm_json", return_value=mock_analysis):
            analysis = analyze_promotion_readiness(data)

        workflow = generate_promoted_workflow(data, analysis)

        assert workflow["run_py_code"] is None
        assert workflow["charter_yaml"] is None
        assert "NOT READY" in workflow["report_md"]


# ---------------------------------------------------------------------------
# TestPromotionFeedback
# ---------------------------------------------------------------------------

class TestPromotionFeedback:
    def test_record_rejection(self, task_instance):
        record_promotion_feedback(task_instance, "my_agent_task", "reject",
                                  "Agent adds inbox interpretation that script misses")
        reason = get_prior_rejection(task_instance, "my_agent_task")
        assert "inbox interpretation" in reason

    def test_record_pause(self, task_instance):
        record_promotion_feedback(task_instance, "my_agent_task", "pause")
        assert is_promotion_paused(task_instance, "my_agent_task")

    def test_not_paused_by_default(self, task_instance):
        assert not is_promotion_paused(task_instance, "my_agent_task")

    def test_rejection_included_in_collected_data(self, task_instance):
        record_promotion_feedback(task_instance, "my_agent_task", "reject",
                                  "Need LLM for inbox parsing")
        data = collect_promotion_data(task_instance, "my_agent_task", last_n=1)
        assert "inbox parsing" in data["prior_rejection_reason"]

    def test_feedback_history_accumulates(self, task_instance):
        record_promotion_feedback(task_instance, "my_agent_task", "reject", "reason 1")
        record_promotion_feedback(task_instance, "my_agent_task", "reject", "reason 2")

        from steward.promote import load_promotion_state
        promo = load_promotion_state(task_instance)
        history = promo["my_agent_task"]["feedback_history"]
        assert len(history) == 2


class TestParsePromotionReply:
    def test_parse_approve(self):
        cmds = parse_promotion_feedback_from_reply("approve daily_planner")
        assert len(cmds) == 1
        assert cmds[0]["action"] == "approve"
        assert cmds[0]["task_id"] == "daily_planner"

    def test_parse_reject_with_reason(self):
        cmds = parse_promotion_feedback_from_reply(
            "reject daily_planner: the agent interprets ambiguous inbox items"
        )
        assert len(cmds) == 1
        assert cmds[0]["action"] == "reject"
        assert "ambiguous inbox" in cmds[0]["reason"]

    def test_parse_pause(self):
        cmds = parse_promotion_feedback_from_reply("pause promotion daily_planner")
        assert len(cmds) == 1
        assert cmds[0]["action"] == "pause"

    def test_parse_resume(self):
        cmds = parse_promotion_feedback_from_reply("resume promotion daily_planner")
        assert len(cmds) == 1
        assert cmds[0]["action"] == "resume"

    def test_parse_multiple_commands(self):
        text = "approve daily_planner\nreject weekly_planner: not stable yet\npause ltt_agent"
        cmds = parse_promotion_feedback_from_reply(text)
        assert len(cmds) == 3

    def test_ignore_non_commands(self):
        cmds = parse_promotion_feedback_from_reply("Thanks, looks good! Keep up the work.")
        assert len(cmds) == 0


class TestDigestSection:
    def test_generates_markdown(self, task_instance):
        analysis = {
            "readiness": "ready",
            "recommended_mode": "direct",
            "estimated_cost_reduction": "90%",
            "estimated_attention_reduction": "No more agent drift",
            "rationale": "Task is fully deterministic.",
        }
        section = generate_promotion_digest_section(
            task_instance, "my_agent_task", analysis
        )
        assert "Promotion Suggestion" in section
        assert "my_agent_task" in section
        assert "90%" in section
        assert "approve my_agent_task" in section
        assert "reject my_agent_task" in section
        assert "pause promotion my_agent_task" in section
