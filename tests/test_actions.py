"""Tests for the ActionResult dataclass and built-in action types."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from charter_worker.actions import ActionResult, Action, WebSearchAction, LightweightSearchAction, ExperimentAction


class TestActionResult:
    """ActionResult is the universal return format for all actions."""

    def test_create_success(self):
        r = ActionResult(
            action_type="web_search",
            status="success",
            summary="Searched for X, found 3 results",
            findings=[{"finding": "A is B", "source": "http://example.com"}],
            artifacts=[{"path": "out/results.json", "description": "raw results", "type": "data"}],
            duration_s=12.5,
        )
        assert r.status == "success"
        assert r.error is None
        assert len(r.findings) == 1
        assert len(r.artifacts) == 1

    def test_create_failed(self):
        r = ActionResult(
            action_type="experiment",
            status="failed",
            summary="Experiment crashed",
            findings=[],
            artifacts=[],
            duration_s=5.0,
            error="subprocess returned exit code 1",
        )
        assert r.status == "failed"
        assert r.error is not None

    def test_to_dict(self):
        r = ActionResult(
            action_type="custom",
            status="success",
            summary="did a thing",
            findings=[],
            artifacts=[],
            duration_s=1.0,
        )
        d = r.to_dict()
        assert isinstance(d, dict)
        assert d["action_type"] == "custom"
        assert d["status"] == "success"
        assert "findings" in d
        assert "artifacts" in d
        assert "duration_s" in d

    def test_default_metadata(self):
        r = ActionResult(
            action_type="test",
            status="success",
            summary="ok",
            findings=[],
            artifacts=[],
            duration_s=0.1,
        )
        assert r.metadata == {}

    def test_custom_metadata(self):
        r = ActionResult(
            action_type="experiment",
            status="success",
            summary="ok",
            findings=[],
            artifacts=[],
            duration_s=0.1,
            metadata={"exit_code": 0, "queries_run": 3},
        )
        assert r.metadata["exit_code"] == 0


class TestAction:
    """Action is the task descriptor passed to execute()."""

    def test_create_basic(self):
        a = Action("web_search")
        assert a.action_type == "web_search"
        assert a.config == {}

    def test_create_with_config(self):
        a = Action("experiment", config={"repo": "/tmp/repo", "timeout": 300})
        assert a.config["repo"] == "/tmp/repo"

    def test_create_with_query(self):
        a = Action("web_search", query="what is X?")
        assert a.query == "what is X?"


class TestWebSearchAction:
    """WebSearchAction wraps the existing search/engine fan-out pipeline."""

    def test_execute_returns_action_result(self):
        action = Action("web_search", query="test query")
        ws = WebSearchAction()

        # Mock the underlying research engine
        mock_result = {
            "synthesis": {
                "key_findings": ["Finding 1"],
                "executive_summary": "Found stuff",
            },
            "sources": [{"url": "http://example.com", "title": "Example"}],
        }
        with patch("charter_worker.search.engine.run_research", return_value=mock_result):
            result = ws.execute(action, context={})

        assert isinstance(result, ActionResult)
        assert result.action_type == "web_search"
        assert result.status == "success"
        assert len(result.summary) > 0

    def test_execute_handles_failure(self):
        action = Action("web_search", query="test")
        ws = WebSearchAction()

        with patch("charter_worker.search.engine.run_research", side_effect=RuntimeError("search failed")):
            result = ws.execute(action, context={})

        assert result.status == "failed"
        assert "search failed" in result.error

    def test_execute_with_timeout(self):
        action = Action("web_search", query="test", config={"timeout": 30})
        ws = WebSearchAction()

        with patch("charter_worker.search.engine.run_research", return_value={"synthesis": {}, "sources": []}):
            result = ws.execute(action, context={})
            assert result.status == "success"


class TestLightweightSearchAction:
    """LightweightSearchAction uses call_llm(search=True) for quick queries."""

    def test_execute_returns_action_result(self):
        action = Action("lightweight_search", query="what is attention?")
        ls = LightweightSearchAction()

        mock_output = "Attention is a mechanism that allows models to focus on relevant parts..."
        with patch("charter_worker.llm.call_llm", return_value=mock_output):
            result = ls.execute(action, context={})

        assert isinstance(result, ActionResult)
        assert result.action_type == "lightweight_search"
        assert result.status == "success"
        assert len(result.findings) >= 1

    def test_execute_handles_failure(self):
        action = Action("lightweight_search", query="test")
        ls = LightweightSearchAction()

        with patch("charter_worker.llm.call_llm", side_effect=RuntimeError("timeout")):
            result = ls.execute(action, context={})

        assert result.status == "failed"
        assert "timeout" in result.error


class TestExperimentAction:
    """ExperimentAction plans code via LLM, writes it, runs it, parses output."""

    def test_successful_experiment(self, tmp_path):
        repo = tmp_path / "experiment_repo"
        repo.mkdir()

        action = Action("experiment", config={
            "repo": str(repo),
            "suggestion": "Test basic arithmetic",
            "planning_timeout": 30,
            "timeout": 10,
        })
        ea = ExperimentAction()

        # Mock LLM to return a simple plan
        plan = {
            "step_id": "test_arithmetic",
            "description": "Verify 2+2=4",
            "code": "import json\nresult = {'answer': 2+2}\nprint(json.dumps(result))\n",
            "filename": "experiments/test_arithmetic.py",
            "run_command": "python experiments/test_arithmetic.py",
            "expected_outputs": [],
        }
        context = {"definition": {"goal": "test"}, "status": {"current_hypothesis": "test"}}

        with patch("charter_worker.llm.call_llm_json", return_value=plan):
            result = ea.execute(action, context)

        assert result.action_type == "experiment"
        assert result.status == "success"
        assert "test_arithmetic" in result.summary
        assert len(result.artifacts) >= 1  # at least the code file
        assert result.metadata["step_id"] == "test_arithmetic"

    def test_missing_repo_fails(self):
        action = Action("experiment", config={})
        ea = ExperimentAction()
        result = ea.execute(action, context={})
        assert result.status == "failed"
        assert "repo" in result.error

    def test_planning_failure(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        action = Action("experiment", config={"repo": str(repo)})
        ea = ExperimentAction()
        context = {"definition": {"goal": "test"}, "status": {}}

        with patch("charter_worker.llm.call_llm_json", side_effect=RuntimeError("LLM down")):
            result = ea.execute(action, context)

        assert result.status == "failed"
        assert "LLM down" in result.error

    def test_experiment_script_failure(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        action = Action("experiment", config={"repo": str(repo), "timeout": 5})
        ea = ExperimentAction()
        context = {"definition": {"goal": "test"}, "status": {}}

        plan = {
            "step_id": "bad_step",
            "description": "This will crash",
            "code": "import sys; sys.exit(1)",
            "filename": "experiments/bad.py",
            "run_command": "python experiments/bad.py",
            "expected_outputs": [],
        }
        with patch("charter_worker.llm.call_llm_json", return_value=plan):
            result = ea.execute(action, context)

        assert result.status == "failed"
        assert result.metadata["step_id"] == "bad_step"
