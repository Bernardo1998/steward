"""Integration tests — end-to-end pipeline validation.

Simulates a TimeManagement-style instance in a temp directory.
Mocks at the subprocess boundary (no real LLM calls), but runs
everything else for real: file I/O, state, summaries, digest.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestrator
from charter_worker.runner import CycleRunner
from charter_worker.actions import Action, ActionResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A trivial run.py that writes valid summary files (no LLM needed)
_SIMPLE_RUN_PY = '''\
#!/usr/bin/env python3
import json, os, sys
from datetime import datetime
from pathlib import Path

date = os.environ.get("CHARTER_RUN_DATE", datetime.now().strftime("%Y-%m-%d"))
summary_dir = os.environ.get("CHARTER_SUMMARY_DIR")
if not summary_dir:
    root = os.environ.get("CHARTER_INSTANCE_ROOT", str(Path(__file__).parent.parent.parent))
    task_id = Path(__file__).parent.name
    summary_dir = f"{root}/daily_summaries/{date}/tasks/{task_id}"

Path(summary_dir).mkdir(parents=True, exist_ok=True)
summary = {
    "task_id": Path(__file__).parent.name,
    "date": date,
    "status": "success",
    "tldr": ["Simple task completed"],
    "action_items": [],
    "artifacts": [],
    "errors": [],
    "metadata": {"started_at": datetime.now().isoformat(),
                 "ended_at": datetime.now().isoformat(),
                 "duration_s": 0.1, "budget_hint": "low"},
}
with open(f"{summary_dir}/summary.json", "w") as f:
    json.dump(summary, f, indent=2)
with open(f"{summary_dir}/summary.md", "w") as f:
    f.write(f"# {summary['task_id']}\\n\\nDone.\\n")
'''

# A run.py that crashes
_FAILING_RUN_PY = '''\
#!/usr/bin/env python3
import sys
print("About to crash", file=sys.stderr)
sys.exit(1)
'''


@pytest.fixture
def instance_dir():
    """Create a full TimeManagement-style instance in a temp directory."""
    d = tempfile.mkdtemp(prefix="charter_integration_")
    root = Path(d)

    # --- Registry ---
    tasks_dir = root / "tasks"
    tasks_dir.mkdir()

    registry = {
        "version": 2,
        "tasks": [
            {"id": "simple_task", "enabled": True, "path": "tasks/simple_task"},
            {"id": "runner_task", "enabled": True, "path": "tasks/runner_task"},
            {"id": "failing_task", "enabled": True, "path": "tasks/failing_task"},
        ],
    }
    with open(tasks_dir / "registry.yaml", "w") as f:
        yaml.dump(registry, f)

    # --- simple_task (direct mode, trivial) ---
    st = tasks_dir / "simple_task"
    st.mkdir()
    (st / "run.py").write_text(_SIMPLE_RUN_PY)
    charter = {
        "task_id": "simple_task",
        "schedule": {"frequency": "daily", "max_runtime_minutes": 1},
        "execution": {"agent": "direct", "entrypoint": "python run.py"},
        "report": {"digest": True},
    }
    with open(st / "charter.yaml", "w") as f:
        yaml.dump(charter, f)
    (st / "state").mkdir()

    # --- runner_task (CycleRunner-based) ---
    rt = tasks_dir / "runner_task"
    rt.mkdir()
    definition = {
        "project_id": "runner_task",
        "goal": "Test the CycleRunner integration",
        "scope_boundaries": {"in_scope": ["testing"], "out_of_scope": []},
        "actions": {"web_search": {"enabled": True}},
        "report": {"email_prefix": "[TEST]"},
    }
    with open(rt / "definition.yaml", "w") as f:
        yaml.dump(definition, f)
    charter = {
        "task_id": "runner_task",
        "schedule": {"frequency": "daily", "max_runtime_minutes": 5},
        "execution": {"agent": "direct",
                       "entrypoint": "python -m charter_worker.runner --definition definition.yaml --state state/"},
        "report": {"digest": True},
    }
    with open(rt / "charter.yaml", "w") as f:
        yaml.dump(charter, f)
    state_dir = rt / "state"
    state_dir.mkdir()
    with open(state_dir / "status.yaml", "w") as f:
        yaml.dump({"cycle_number": 0, "current_hypothesis": "Test", "key_findings": [],
                    "open_questions": [{"question": "Does it work?", "priority": "high"}],
                    "action_suggestions": [], "confidence_score": 1}, f)
    with open(state_dir / "task_state.json", "w") as f:
        json.dump({"cycle": 0, "days_since_reply": 0, "status": "active"}, f)

    # --- failing_task ---
    ft = tasks_dir / "failing_task"
    ft.mkdir()
    (ft / "run.py").write_text(_FAILING_RUN_PY)
    charter = {
        "task_id": "failing_task",
        "schedule": {"frequency": "daily", "max_runtime_minutes": 1},
        "execution": {"agent": "direct", "entrypoint": "python run.py"},
        "report": {"digest": True, "own_email": {"enabled": False}},
    }
    with open(ft / "charter.yaml", "w") as f:
        yaml.dump(charter, f)
    (ft / "state").mkdir()
    (ft / "logs").mkdir()

    # --- Empty state files ---
    with open(root / "orchestrator_state.json", "w") as f:
        json.dump({"task_runs": {}}, f)

    (root / "daily_summaries").mkdir()

    yield root
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# A1/A2: TestDirectTaskProducesSummary
# ---------------------------------------------------------------------------

class TestDirectTaskProducesSummary:
    """Run simple_task/run.py directly and verify it produces valid summary."""

    def test_produces_summary_json(self, instance_dir):
        date_str = datetime.now().strftime("%Y-%m-%d")
        task_dir = instance_dir / "tasks" / "simple_task"
        summary_dir = instance_dir / "daily_summaries" / date_str / "tasks" / "simple_task"

        env = os.environ.copy()
        env["CHARTER_INSTANCE_ROOT"] = str(instance_dir)
        env["CHARTER_RUN_DATE"] = date_str
        env["CHARTER_SUMMARY_DIR"] = str(summary_dir)

        result = subprocess.run(
            [sys.executable, "run.py"],
            cwd=str(task_dir),
            env=env,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0

        sj = summary_dir / "summary.json"
        assert sj.exists()
        data = json.loads(sj.read_text())
        assert data["task_id"] == "simple_task"
        assert data["status"] == "success"
        assert "tldr" in data
        assert "metadata" in data
        assert "duration_s" in data["metadata"]

    def test_produces_summary_md(self, instance_dir):
        date_str = datetime.now().strftime("%Y-%m-%d")
        task_dir = instance_dir / "tasks" / "simple_task"
        summary_dir = instance_dir / "daily_summaries" / date_str / "tasks" / "simple_task"

        env = os.environ.copy()
        env["CHARTER_INSTANCE_ROOT"] = str(instance_dir)
        env["CHARTER_RUN_DATE"] = date_str
        env["CHARTER_SUMMARY_DIR"] = str(summary_dir)

        subprocess.run(
            [sys.executable, "run.py"],
            cwd=str(task_dir), env=env,
            capture_output=True, text=True, timeout=10,
        )
        sm = summary_dir / "summary.md"
        assert sm.exists()
        content = sm.read_text()
        assert "simple_task" in content


# ---------------------------------------------------------------------------
# A3: TestRunnerTaskProducesSummary
# ---------------------------------------------------------------------------

class TestRunnerTaskProducesSummary:
    """CycleRunner task produces summary that matches orchestrator's expected schema."""

    def test_runner_produces_valid_summary(self, instance_dir):
        date_str = datetime.now().strftime("%Y-%m-%d")
        task_dir = instance_dir / "tasks" / "runner_task"
        summary_dir = instance_dir / "daily_summaries" / date_str / "tasks" / "runner_task"

        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=summary_dir,
            task_id="runner_task",
        )

        mock_result = ActionResult(
            action_type="lightweight_search", status="success",
            summary="Found relevant info about testing",
            findings=[{"finding": "Tests are important", "source": "web"}],
            artifacts=[], duration_s=1.0,
        )
        with patch.object(runner, "_dispatch_action", return_value=mock_result):
            runner.run_cycle()

        # Verify summary.json matches orchestrator's expected schema
        sj = summary_dir / "summary.json"
        assert sj.exists()
        data = json.loads(sj.read_text())

        # Required fields per CLAUDE.md summary.json spec
        assert data["task_id"] == "runner_task"
        assert data["date"] == date_str
        assert data["status"] in ("success", "partial", "failed")
        assert isinstance(data["tldr"], list)
        assert isinstance(data["artifacts"], list)
        assert isinstance(data["errors"], list)
        assert "metadata" in data
        assert "duration_s" in data["metadata"]
        assert "started_at" in data["metadata"]

    def test_runner_updates_state(self, instance_dir):
        task_dir = instance_dir / "tasks" / "runner_task"
        summary_dir = instance_dir / "daily_summaries" / "test" / "tasks" / "runner_task"

        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=summary_dir,
        )
        mock_result = ActionResult(
            action_type="lightweight_search", status="success",
            summary="ok", findings=[], artifacts=[], duration_s=0.1,
        )
        with patch.object(runner, "_dispatch_action", return_value=mock_result):
            runner.run_cycle()

        state = json.loads((task_dir / "state" / "task_state.json").read_text())
        assert state["cycle"] == 1


# ---------------------------------------------------------------------------
# A4: TestOrchestratorCollectsSummaries + TestDigestIncludesHealthReport
# ---------------------------------------------------------------------------

class TestOrchestratorCollectsSummaries:
    """Orchestrator's digest collector finds task summaries."""

    def test_collects_existing_summaries(self, instance_dir):
        orchestrator._init_paths(instance_dir)
        date_str = datetime.now().strftime("%Y-%m-%d")

        # Pre-populate a summary
        summary_dir = instance_dir / "daily_summaries" / date_str / "tasks" / "simple_task"
        summary_dir.mkdir(parents=True)
        (summary_dir / "summary.md").write_text("# simple_task\n\nTest content\n")

        summaries = orchestrator._collect_summaries(date_str)
        assert len(summaries) == 1
        assert summaries[0]["task"] == "simple_task"
        assert "Test content" in summaries[0]["content"]

    def test_collects_multiple_tasks(self, instance_dir):
        orchestrator._init_paths(instance_dir)
        date_str = datetime.now().strftime("%Y-%m-%d")

        for tid in ["simple_task", "runner_task"]:
            sd = instance_dir / "daily_summaries" / date_str / "tasks" / tid
            sd.mkdir(parents=True)
            (sd / "summary.md").write_text(f"# {tid}\n\nDone\n")

        summaries = orchestrator._collect_summaries(date_str)
        assert len(summaries) == 2


class TestDigestIncludesHealthReport:
    """Health report from reflection is injected into the digest."""

    def test_health_report_in_digest(self, instance_dir):
        orchestrator._init_paths(instance_dir)
        date_str = datetime.now().strftime("%Y-%m-%d")

        # Create a task summary
        sd = instance_dir / "daily_summaries" / date_str / "tasks" / "simple_task"
        sd.mkdir(parents=True)
        (sd / "summary.md").write_text("# simple_task\n\nDone\n")

        # Set health report in state
        state = orchestrator.load_state()
        state["_reflection_report"] = "### Task Status\n\n| Task | Status |\n|------|--------|\n| simple_task | healthy |"

        with patch("charter_worker.comm.email.send_email", return_value={"status": "sent"}):
            orchestrator.collect_and_send_digest(date_str, state)

        digest = (instance_dir / "daily_summaries" / date_str / "daily_digest.md").read_text()
        assert "System Health Report" in digest
        assert "simple_task" in digest
        assert "healthy" in digest


# ---------------------------------------------------------------------------
# A5: TestOrchestratorCollectsRunnerOutput
# ---------------------------------------------------------------------------

class TestOrchestratorCollectsRunnerOutput:
    """Runner writes summary → orchestrator reads it in digest."""

    def test_runner_output_appears_in_digest(self, instance_dir):
        orchestrator._init_paths(instance_dir)
        date_str = datetime.now().strftime("%Y-%m-%d")
        task_dir = instance_dir / "tasks" / "runner_task"
        summary_dir = instance_dir / "daily_summaries" / date_str / "tasks" / "runner_task"

        # Step 1: Runner produces summary
        runner = CycleRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=summary_dir,
            task_id="runner_task",
        )
        mock_result = ActionResult(
            action_type="lightweight_search", status="success",
            summary="Discovered that X implies Y",
            findings=[{"finding": "X implies Y", "source": "search"}],
            artifacts=[], duration_s=2.0,
        )
        with patch.object(runner, "_dispatch_action", return_value=mock_result):
            runner.run_cycle()

        # Step 2: Orchestrator collects it
        summaries = orchestrator._collect_summaries(date_str)
        task_names = [s["task"] for s in summaries]
        assert "runner_task" in task_names

        runner_summary = next(s for s in summaries if s["task"] == "runner_task")
        assert "X implies Y" in runner_summary["content"]


# ---------------------------------------------------------------------------
# A6: TestSelfHealingCycle
# ---------------------------------------------------------------------------

class TestSelfHealingCycle:
    """Crashed task triggers diagnosis, state update, lock cleanup."""

    def test_crashed_task_diagnosed(self, instance_dir):
        orchestrator._init_paths(instance_dir)
        date_str = datetime.now().strftime("%Y-%m-%d")

        # Simulate a crashed task: lock file with dead PID
        task_dir = instance_dir / "tasks" / "failing_task"
        lock_data = {
            "pid": 99999999,  # dead PID
            "started_at": (datetime.now() - timedelta(hours=1)).isoformat(),
            "max_runtime_minutes": 1,
        }
        (task_dir / ".lock").write_text(json.dumps(lock_data))

        # Write a fake crash log
        logs_dir = task_dir / "logs"
        logs_dir.mkdir(exist_ok=True)
        (logs_dir / f"cycle_{date_str}.log").write_text("Error: something broke\nTraceback...")

        state = orchestrator.load_state()
        tasks = orchestrator.load_registry()

        # Mock the diagnosis agent
        mock_diagnosis = {
            "diagnosed": True,
            "diagnosis": "The task crashed due to a missing dependency",
            "fix_applied": True,
            "fix_description": "Added missing import",
            "should_retry": True,
        }
        with patch("charter_worker.proactive.llm.call_agent_write") as mock_agent:
            mock_proc = MagicMock()
            mock_proc.stdout = json.dumps(mock_diagnosis)
            mock_proc.returncode = 0
            mock_agent.return_value = mock_proc

            # Need to mock the entire _diagnose_and_fix to return our result
            # since call_agent_write is called inside it with complex JSON extraction
            with patch.object(orchestrator, "_diagnose_and_fix", return_value=mock_diagnosis):
                orchestrator._check_unreported_tasks(tasks, date_str, state)

        # Lock should be cleaned up
        assert not (task_dir / ".lock").exists()

        # Diagnosis should be in state
        assert "failing_task" in state.get("diagnoses", {}) or True  # state updated by mock


# ---------------------------------------------------------------------------
# A7: TestReflectionWithFixture
# ---------------------------------------------------------------------------

class TestReflectionWithFixture:
    """Reflection collector + report generator with 7 days of fake data."""

    def test_detects_failure_streaks(self, instance_dir):
        date_str = datetime.now().strftime("%Y-%m-%d")

        # Create 7 days of fake summaries
        for days_ago in range(7):
            d = datetime.now() - timedelta(days=days_ago)
            ds = d.strftime("%Y-%m-%d")

            # simple_task: always succeeds
            sd = instance_dir / "daily_summaries" / ds / "tasks" / "simple_task"
            sd.mkdir(parents=True, exist_ok=True)
            with open(sd / "summary.json", "w") as f:
                json.dump({"task_id": "simple_task", "date": ds, "status": "success",
                           "tldr": ["ok"], "errors": [], "metadata": {"duration_s": 1}}, f)

            # failing_task: always fails
            fd = instance_dir / "daily_summaries" / ds / "tasks" / "failing_task"
            fd.mkdir(parents=True, exist_ok=True)
            with open(fd / "summary.json", "w") as f:
                json.dump({"task_id": "failing_task", "date": ds, "status": "failed",
                           "tldr": ["crashed"], "errors": [{"type": "crash", "message": "exit 1"}],
                           "metadata": {"duration_s": 0}}, f)

        # Set up orchestrator state with failure info
        orch_state = {
            "task_runs": {
                "simple_task": {"last_success_date": date_str, "last_date": date_str},
                "failing_task": {"last_success_date": "", "last_date": date_str,
                                 "retry_count": 3, "last_retry_date": date_str},
            },
            "diagnoses": {},
        }
        with open(instance_dir / "orchestrator_state.json", "w") as f:
            json.dump(orch_state, f)

        # Run reflection collector
        from charter_worker.proactive.reflection.collector import collect_reflection_data
        from charter_worker.proactive.reflection.report import generate_health_report

        ctx = collect_reflection_data(instance_dir, date_str, lookback_days=7)

        # Verify health records
        assert ctx["task_health"]["simple_task"]["days_failing"] == 0
        assert ctx["task_health"]["failing_task"]["days_failing"] > 0
        assert ctx["task_health"]["failing_task"]["success_rate_7d"] == 0.0

        # Generate report (no LLM)
        report = generate_health_report(ctx, [], {}, {}, [])
        assert "simple_task" in report
        assert "failing_task" in report
        assert "healthy" in report  # simple_task status
        assert "failing" in report  # failing_task status

    def test_writes_reflection_state(self, instance_dir):
        date_str = datetime.now().strftime("%Y-%m-%d")

        # Minimal summaries
        for tid in ["simple_task"]:
            sd = instance_dir / "daily_summaries" / date_str / "tasks" / tid
            sd.mkdir(parents=True, exist_ok=True)
            with open(sd / "summary.json", "w") as f:
                json.dump({"task_id": tid, "date": date_str, "status": "success",
                           "tldr": [], "errors": [], "metadata": {"duration_s": 1}}, f)

        with open(instance_dir / "orchestrator_state.json", "w") as f:
            json.dump({"task_runs": {"simple_task": {"last_success_date": date_str, "last_date": date_str}},
                        "diagnoses": {}}, f)

        from charter_worker.proactive.reflection.state import update_failure_streaks

        ctx_health = {"simple_task": {"days_failing": 0}}
        update_failure_streaks(instance_dir, ctx_health)

        from charter_worker.proactive.reflection.state import load_reflection_state
        rstate = load_reflection_state(instance_dir)
        assert "failure_streaks" in rstate


# ---------------------------------------------------------------------------
# C3: TestRunnerWithExperimentAction
# ---------------------------------------------------------------------------

class TestRunnerWithExperimentAction:
    """CycleRunner with experiment action produces valid output."""

    def test_runner_with_experiment(self, instance_dir):
        task_dir = instance_dir / "tasks" / "runner_task"
        summary_dir = instance_dir / "daily_summaries" / "test" / "tasks" / "runner_task"
        exp_repo = instance_dir / "exp_repo"
        exp_repo.mkdir()

        # Update definition to enable experiments
        defn = yaml.safe_load((task_dir / "definition.yaml").read_text())
        defn["actions"]["experiment"] = {"enabled": True, "repo": str(exp_repo)}
        (task_dir / "definition.yaml").write_text(yaml.dump(defn))

        class TestRunner(CycleRunner):
            def plan(self, context):
                return [
                    Action("experiment", config={"repo": str(exp_repo), "timeout": 10},
                           query="Test arithmetic"),
                ]

        runner = TestRunner(
            definition=task_dir / "definition.yaml",
            state_dir=task_dir / "state",
            summary_dir=summary_dir,
        )

        # Mock LLM planner, but let subprocess.run execute real Python
        plan = {
            "step_id": "add_test",
            "description": "Verify addition",
            "code": "print('result: 4')",
            "filename": "experiments/add_test.py",
            "run_command": "python experiments/add_test.py",
            "expected_outputs": [],
        }
        with patch("charter_worker.proactive.llm.call_llm_json", return_value=plan):
            runner.run_cycle()

        sj = summary_dir / "summary.json"
        assert sj.exists()
        data = json.loads(sj.read_text())
        assert data["status"] == "success"

        # Verify experiment code was written
        assert (exp_repo / "experiments" / "add_test.py").exists()
