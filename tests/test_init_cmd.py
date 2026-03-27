"""Tests for charter-init bootstrap command."""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml

# Ensure the repo root is importable
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from init_cmd import init_instance, list_templates


@pytest.fixture
def tmp_dir():
    """Create a temporary directory for test instances."""
    d = tempfile.mkdtemp(prefix="charter_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


class TestListTemplates:
    def test_returns_list(self):
        templates = list_templates()
        assert isinstance(templates, list)
        assert len(templates) >= 1

    def test_contains_hello_world(self):
        assert "hello_world" in list_templates()

    def test_contains_ltt_thinker(self):
        assert "ltt_thinker" in list_templates()

    def test_sorted(self):
        templates = list_templates()
        assert templates == sorted(templates)


class TestInitInstance:
    def test_creates_directory_structure(self, tmp_dir):
        target = tmp_dir / "my-instance"
        result = init_instance(target)

        assert target.is_dir()
        assert (target / "tasks").is_dir()
        assert (target / "daily_summaries").is_dir()
        assert (target / "logs").is_dir()
        assert len(result["created_dirs"]) == 3

    def test_copies_hello_world_template(self, tmp_dir):
        target = tmp_dir / "inst"
        init_instance(target)

        task_dir = target / "tasks" / "hello_world"
        assert task_dir.is_dir()
        assert (task_dir / "charter.yaml").is_file()
        assert (task_dir / "run.py").is_file()
        assert (task_dir / "state").is_dir()

    def test_creates_registry(self, tmp_dir):
        target = tmp_dir / "inst"
        init_instance(target)

        reg = target / "tasks" / "registry.yaml"
        assert reg.is_file()
        data = yaml.safe_load(reg.read_text())
        assert data["version"] == 2
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == "hello_world"
        assert data["tasks"][0]["enabled"] is True

    def test_creates_gitignore(self, tmp_dir):
        target = tmp_dir / "inst"
        init_instance(target)

        gi = target / ".gitignore"
        assert gi.is_file()
        content = gi.read_text()
        assert "email_config.yaml" in content
        assert "openai_api_key.json" in content
        assert "orchestrator_state.json" in content

    def test_creates_run_sh(self, tmp_dir):
        target = tmp_dir / "inst"
        init_instance(target)

        run_sh = target / "run.sh"
        assert run_sh.is_file()
        assert "charter-orchestrator" in run_sh.read_text()
        # Check executable permission
        assert os.access(run_sh, os.X_OK)

    def test_custom_template(self, tmp_dir):
        target = tmp_dir / "inst"
        result = init_instance(target, template="ltt_thinker")

        assert (target / "tasks" / "ltt_thinker").is_dir()
        reg = yaml.safe_load((target / "tasks" / "registry.yaml").read_text())
        assert reg["tasks"][0]["id"] == "ltt_thinker"

    def test_invalid_template_raises(self, tmp_dir):
        target = tmp_dir / "inst"
        with pytest.raises(FileNotFoundError, match="not_a_real_template"):
            init_instance(target, template="not_a_real_template")

    def test_idempotent_no_overwrite(self, tmp_dir):
        target = tmp_dir / "inst"
        init_instance(target)

        # Modify registry to prove it doesn't get overwritten
        reg_path = target / "tasks" / "registry.yaml"
        reg_path.write_text("version: 2\ntasks: []\n")

        result = init_instance(target)
        assert any("already exists" in w for w in result["warnings"])
        # Registry should NOT be overwritten
        assert "tasks: []" in reg_path.read_text()

    def test_hello_world_task_runs(self, tmp_dir):
        """End-to-end: init + run hello_world task."""
        target = tmp_dir / "inst"
        init_instance(target)

        import subprocess
        proc = subprocess.run(
            [sys.executable, "run.py"],
            cwd=str(target / "tasks" / "hello_world"),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0
        assert "hello_world" in proc.stdout

        # Check summary was created
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        summary_dir = target / "daily_summaries" / today / "tasks" / "hello_world"
        assert (summary_dir / "summary.json").is_file()
        data = json.loads((summary_dir / "summary.json").read_text())
        assert data["status"] == "success"
        assert data["task_id"] == "hello_world"
