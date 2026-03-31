"""Tests for the CLI-agnostic LLM abstraction layer.

Verifies that build_agent_cmd produces correct commands for both
codex and claude providers, and that the high-level call functions
route through the correct code paths.
"""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from charter_worker.llm import (
    build_agent_cmd,
    detect_provider,
    call_llm,
    call_llm_json,
    call_agent_write,
    extract_json,
    _PROVIDERS,
)


# ---------------------------------------------------------------------------
# build_agent_cmd tests
# ---------------------------------------------------------------------------

class TestBuildAgentCmd:
    """Test command building for both providers and both modes."""

    # --- Codex provider ---

    def test_codex_read_only_basic(self):
        cmd, uses_stdin = build_agent_cmd(mode="read_only", provider="codex")
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--ephemeral" in cmd
        assert "-s" in cmd
        assert "read-only" in cmd
        assert cmd[-1] == "-"  # stdin marker
        assert uses_stdin is True

    def test_codex_write_basic(self):
        cmd, uses_stdin = build_agent_cmd(mode="write", provider="codex")
        assert cmd[0] == "codex"
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert cmd[-1] == "-"
        assert uses_stdin is True

    def test_codex_with_model(self):
        cmd, _ = build_agent_cmd(mode="read_only", provider="codex", model="o3")
        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "o3"

    def test_codex_with_working_dir(self):
        cmd, _ = build_agent_cmd(
            mode="write", provider="codex",
            working_dir=Path("/tmp/test"),
        )
        assert "-C" in cmd
        idx = cmd.index("-C")
        assert cmd[idx + 1] == "/tmp/test"

    def test_codex_with_add_dir(self):
        cmd, _ = build_agent_cmd(
            mode="write", provider="codex",
            add_dir=Path("/repo"),
        )
        assert "--add-dir" in cmd
        idx = cmd.index("--add-dir")
        assert cmd[idx + 1] == "/repo"

    def test_codex_write_full_flags(self):
        cmd, uses_stdin = build_agent_cmd(
            mode="write", provider="codex",
            model="o3",
            working_dir=Path("/workspace"),
            add_dir=Path("/repo"),
        )
        assert uses_stdin is True
        assert "-C" in cmd
        assert "--add-dir" in cmd
        assert "-m" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert cmd[-1] == "-"

    # --- Claude provider ---

    def test_claude_read_only_basic(self):
        cmd, uses_stdin = build_agent_cmd(
            mode="read_only", provider="claude",
            prompt="test prompt",
        )
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "-p" in cmd
        idx = cmd.index("-p")
        assert cmd[idx + 1] == "test prompt"
        assert uses_stdin is False

    def test_claude_write_basic(self):
        cmd, uses_stdin = build_agent_cmd(
            mode="write", provider="claude",
            prompt="fix this",
        )
        assert cmd[0] == "claude"
        assert "--dangerously-skip-permissions" in cmd
        assert "-p" in cmd
        assert uses_stdin is False

    def test_claude_with_model(self):
        cmd, _ = build_agent_cmd(
            mode="read_only", provider="claude",
            model="opus",
            prompt="test",
        )
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "opus"

    def test_claude_with_working_dir(self):
        cmd, _ = build_agent_cmd(
            mode="write", provider="claude",
            working_dir=Path("/workspace"),
            prompt="fix",
        )
        assert "-C" in cmd
        idx = cmd.index("-C")
        assert cmd[idx + 1] == "/workspace"

    def test_claude_with_add_dir(self):
        cmd, _ = build_agent_cmd(
            mode="write", provider="claude",
            add_dir=Path("/repo"),
            prompt="fix",
        )
        assert "--add-dir" in cmd
        idx = cmd.index("--add-dir")
        assert cmd[idx + 1] == "/repo"

    def test_claude_write_full_flags(self):
        cmd, uses_stdin = build_agent_cmd(
            mode="write", provider="claude",
            model="sonnet",
            working_dir=Path("/workspace"),
            add_dir=Path("/repo"),
            prompt="do the thing",
        )
        assert uses_stdin is False
        assert "-C" in cmd
        assert "--add-dir" in cmd
        assert "--model" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "-p" in cmd

    def test_claude_no_stdin_marker(self):
        """Claude commands must NOT have '-' stdin marker."""
        cmd, uses_stdin = build_agent_cmd(
            mode="read_only", provider="claude",
            prompt="test",
        )
        assert "-" not in cmd  # no stdin marker
        assert uses_stdin is False

    def test_codex_has_stdin_marker(self):
        """Codex commands must have '-' stdin marker."""
        cmd, uses_stdin = build_agent_cmd(mode="read_only", provider="codex")
        assert cmd[-1] == "-"
        assert uses_stdin is True

    # --- Cross-provider equivalence ---

    def test_both_providers_produce_working_dir(self):
        """Both providers support -C for working directory."""
        for provider in ["codex", "claude"]:
            cmd, _ = build_agent_cmd(
                mode="write", provider=provider,
                working_dir=Path("/test"),
                prompt="x",
            )
            assert "-C" in cmd

    def test_both_providers_produce_add_dir(self):
        """Both providers support --add-dir."""
        for provider in ["codex", "claude"]:
            cmd, _ = build_agent_cmd(
                mode="write", provider=provider,
                add_dir=Path("/test"),
                prompt="x",
            )
            assert "--add-dir" in cmd

    def test_both_providers_produce_model(self):
        """Both providers support model selection."""
        for provider, flag in [("codex", "-m"), ("claude", "--model")]:
            cmd, _ = build_agent_cmd(
                mode="read_only", provider=provider,
                model="test-model",
                prompt="x",
            )
            assert flag in cmd

    # --- Error cases ---

    def test_unknown_provider_no_template(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            build_agent_cmd(mode="read_only", provider="nonexistent")


# ---------------------------------------------------------------------------
# Custom provider tests
# ---------------------------------------------------------------------------

class TestCustomProvider:
    def test_custom_provider_from_env(self):
        template = json.dumps({
            "read_only": {
                "cmd": ["my-llm", "--safe"],
                "stdin": True,
                "prompt_flag": None,
            },
            "write": {
                "cmd": ["my-llm", "--write"],
                "stdin": False,
                "prompt_flag": "--prompt",
            },
            "model_flag": ["--model"],
            "workdir_flag": ["--dir"],
            "adddir_flag": ["--extra-dir"],
        })
        with patch.dict(os.environ, {"CHARTER_LLM_CMD_TEMPLATE": template}):
            cmd, uses_stdin = build_agent_cmd(
                mode="read_only", provider="my-llm",
            )
            assert cmd[0] == "my-llm"
            assert "--safe" in cmd
            assert uses_stdin is True

    def test_custom_provider_write_mode(self):
        template = json.dumps({
            "read_only": {
                "cmd": ["my-llm", "--safe"],
                "stdin": True,
                "prompt_flag": None,
            },
            "write": {
                "cmd": ["my-llm", "--write"],
                "stdin": False,
                "prompt_flag": "--prompt",
            },
        })
        with patch.dict(os.environ, {"CHARTER_LLM_CMD_TEMPLATE": template}):
            cmd, uses_stdin = build_agent_cmd(
                mode="write", provider="my-llm",
                prompt="fix it",
            )
            assert cmd[0] == "my-llm"
            assert "--write" in cmd
            assert "--prompt" in cmd
            assert "fix it" in cmd
            assert uses_stdin is False


# ---------------------------------------------------------------------------
# detect_provider tests
# ---------------------------------------------------------------------------

class TestDetectProvider:
    def test_env_var_override(self):
        with patch.dict(os.environ, {"CHARTER_LLM_CLI": "claude"}):
            with patch("shutil.which", return_value="/usr/bin/claude"):
                assert detect_provider() == "claude"

    def test_env_var_missing_binary_falls_through(self):
        def which_side_effect(name):
            if name == "nonexistent":
                return None
            if name == "codex":
                return "/usr/bin/codex"
            return None

        with patch.dict(os.environ, {"CHARTER_LLM_CLI": "nonexistent"}):
            with patch("shutil.which", side_effect=which_side_effect):
                assert detect_provider() == "codex"

    def test_auto_detect_codex_first(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CHARTER_LLM_CLI", None)
            with patch("shutil.which", return_value="/usr/bin/codex"):
                assert detect_provider() == "codex"

    def test_auto_detect_claude_fallback(self):
        def which_side_effect(name):
            if name == "claude":
                return "/usr/bin/claude"
            return None

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CHARTER_LLM_CLI", None)
            with patch("shutil.which", side_effect=which_side_effect):
                assert detect_provider() == "claude"

    def test_no_provider_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CHARTER_LLM_CLI", None)
            with patch("shutil.which", return_value=None):
                with pytest.raises(RuntimeError, match="No CLI agent found"):
                    detect_provider()


# ---------------------------------------------------------------------------
# call_llm tests (mocked subprocess)
# ---------------------------------------------------------------------------

class TestCallLlm:
    """Test that call_llm routes through correct code paths for each provider."""

    def test_codex_uses_stdin(self):
        """Codex: prompt piped via stdin, not in command args."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("result text", "")
        mock_proc.returncode = 0
        mock_proc.poll.return_value = 0

        with patch("charter_worker.llm.detect_provider", return_value="codex"):
            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                result = call_llm("test prompt", provider="codex")
                assert result == "result text"
                # Verify prompt passed via communicate (stdin)
                mock_proc.communicate.assert_called_once()
                call_args = mock_proc.communicate.call_args
                assert call_args[0][0] == "test prompt"  # prompt as stdin

    def test_claude_uses_flag(self):
        """Claude: prompt passed via -p flag, not stdin."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("result text", "")
        mock_proc.returncode = 0
        mock_proc.poll.return_value = 0

        with patch("charter_worker.llm.detect_provider", return_value="claude"):
            with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                result = call_llm("test prompt", provider="claude")
                assert result == "result text"
                # Verify -p flag in command
                cmd = mock_popen.call_args[0][0]
                assert "-p" in cmd
                assert "test prompt" in cmd

    def test_empty_output_raises(self):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "some error")
        mock_proc.returncode = 1
        mock_proc.poll.return_value = 1

        with patch("subprocess.Popen", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="LLM returned empty output"):
                call_llm("test", provider="codex")

    def test_timeout_kills_process(self):
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(["codex"], 10),
            ("", ""),  # after kill
        ]
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345

        with patch("subprocess.Popen", return_value=mock_proc):
            with patch("os.killpg"):
                with pytest.raises(subprocess.TimeoutExpired):
                    call_llm("test", provider="codex", timeout=10)


# ---------------------------------------------------------------------------
# call_agent_write tests (mocked subprocess)
# ---------------------------------------------------------------------------

class TestCallAgentWrite:
    """Test write-mode agent calls for both providers."""

    def test_codex_write_uses_stdin(self):
        mock_result = MagicMock()
        mock_result.stdout = '```json\n{"diagnosis": "test"}\n```'
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = call_agent_write(
                "fix this",
                working_dir=Path("/task"),
                add_dir=Path("/repo"),
                provider="codex",
            )
            assert result.stdout == mock_result.stdout
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            assert "codex" in cmd[0]
            assert "--dangerously-bypass-approvals-and-sandbox" in cmd
            # Prompt via input= kwarg (stdin)
            assert call_args[1]["input"] == "fix this"

    def test_claude_write_uses_flag(self):
        mock_result = MagicMock()
        mock_result.stdout = '```json\n{"diagnosis": "test"}\n```'
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = call_agent_write(
                "fix this",
                working_dir=Path("/task"),
                provider="claude",
            )
            call_args = mock_run.call_args
            cmd = call_args[0][0]
            assert "claude" in cmd[0]
            assert "--dangerously-skip-permissions" in cmd
            assert "-p" in cmd
            # Prompt NOT via input= (no stdin)
            assert call_args[1].get("input") is None

    def test_write_with_working_dir(self):
        mock_result = MagicMock()
        mock_result.stdout = "ok"
        mock_result.returncode = 0

        for provider in ["codex", "claude"]:
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                call_agent_write(
                    "test",
                    working_dir=Path("/my/dir"),
                    provider=provider,
                )
                call_args = mock_run.call_args
                cmd = call_args[0][0]
                assert "-C" in cmd
                idx = cmd.index("-C")
                assert cmd[idx + 1] == "/my/dir"


# ---------------------------------------------------------------------------
# extract_json tests
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_fenced_json(self):
        text = 'Some text\n```json\n{"key": "value"}\n```\nmore text'
        assert extract_json(text) == {"key": "value"}

    def test_raw_json(self):
        text = '{"key": "value"}'
        assert extract_json(text) == {"key": "value"}

    def test_no_json_raises(self):
        with pytest.raises(ValueError, match="No JSON found"):
            extract_json("just plain text")


# ---------------------------------------------------------------------------
# Behavioral equivalence: codex vs claude produce same logical result
# ---------------------------------------------------------------------------

class TestProviderEquivalence:
    """Verify that switching provider preserves the same logical behavior."""

    def test_read_only_both_produce_valid_commands(self):
        """Both providers produce commands that can be executed."""
        for provider in ["codex", "claude"]:
            cmd, uses_stdin = build_agent_cmd(
                mode="read_only",
                provider=provider,
                model="test-model",
                prompt="analyze this",
            )
            # All commands should be non-empty lists starting with the CLI name
            assert len(cmd) >= 2
            assert cmd[0] == provider

    def test_write_both_produce_valid_commands(self):
        """Both providers produce write-mode commands with all flags."""
        for provider in ["codex", "claude"]:
            cmd, uses_stdin = build_agent_cmd(
                mode="write",
                provider=provider,
                working_dir=Path("/workspace"),
                add_dir=Path("/repo"),
                model="test",
                prompt="fix it",
            )
            assert len(cmd) >= 4
            assert cmd[0] == provider
            assert "-C" in cmd
            assert "--add-dir" in cmd

    def test_call_llm_json_parses_identically(self):
        """Both providers parse the same JSON output format."""
        json_output = '```json\n{"diagnosis": "root cause", "fix_applied": true}\n```'

        for provider in ["codex", "claude"]:
            mock_proc = MagicMock()
            mock_proc.communicate.return_value = (json_output, "")
            mock_proc.returncode = 0
            mock_proc.poll.return_value = 0

            with patch("subprocess.Popen", return_value=mock_proc):
                result = call_llm_json("test", provider=provider)
                assert result == {"diagnosis": "root cause", "fix_applied": True}
