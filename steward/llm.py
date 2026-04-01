"""Thin LLM abstraction — all calls go through here so provider is swappable.

Supports multiple CLI agents (codex, claude, or custom) for two modes:
- read-only: analysis prompts, no filesystem access
- write:     agent sessions with filesystem write access

Provider is selected via:
1. Explicit `provider` parameter
2. STEWARD_LLM_CLI env var (e.g. "codex", "claude", "my-custom-cli")
3. Auto-detect: first available on PATH from [codex, claude]

For custom CLIs, set STEWARD_LLM_CLI to the binary name and
STEWARD_LLM_CMD_TEMPLATE to a JSON config (see build_agent_cmd docs).
"""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Provider detection and command building
# ---------------------------------------------------------------------------

# Known providers and their command templates for each mode.
# Each template is a dict with:
#   cmd:         base command list
#   stdin:       whether prompt is piped via stdin (True) or passed as arg (False)
#   prompt_flag: CLI flag for prompt (used when stdin=False)
#   extra:       additional flags appended to cmd
_PROVIDERS = {
    "codex": {
        "read_only": {
            "cmd": ["codex", "exec", "--ephemeral", "-s", "read-only"],
            "stdin": True,
            "prompt_flag": None,
        },
        "write": {
            "cmd": ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
            "stdin": True,
            "prompt_flag": None,
        },
    },
    "claude": {
        "read_only": {
            "cmd": ["claude", "--dangerously-skip-permissions"],
            "stdin": False,
            "prompt_flag": "-p",
        },
        "write": {
            "cmd": ["claude", "--dangerously-skip-permissions"],
            "stdin": False,
            "prompt_flag": "-p",
        },
    },
}

# Provider-specific flag mappings
_MODEL_FLAG = {
    "codex": ["-m"],
    "claude": ["--model"],
}

_WORKDIR_FLAG = {
    "codex": ["-C"],
    "claude": ["-C"],
}

_ADDDIR_FLAG = {
    "codex": ["--add-dir"],
    "claude": ["--add-dir"],
}


def detect_provider() -> str:
    """Detect which CLI agent is available.

    Checks STEWARD_LLM_CLI env var first, then auto-detects.
    """
    env_cli = os.environ.get("STEWARD_LLM_CLI", "").strip()
    if env_cli:
        if shutil.which(env_cli):
            return env_cli
        # Env var set but binary not found — fall through to auto-detect

    for candidate in ["codex", "claude"]:
        if shutil.which(candidate):
            return candidate

    raise RuntimeError(
        "No CLI agent found on PATH. Install codex (npm i -g @openai/codex) "
        "or claude (npm i -g @anthropic-ai/claude-code), "
        "or set STEWARD_LLM_CLI to your custom CLI binary."
    )


def _load_custom_template() -> Optional[dict]:
    """Load custom CLI template from STEWARD_LLM_CMD_TEMPLATE env var.

    Expected JSON format:
    {
      "read_only": {
        "cmd": ["my-cli", "--read-only"],
        "stdin": true,
        "prompt_flag": null
      },
      "write": {
        "cmd": ["my-cli", "--full-access"],
        "stdin": false,
        "prompt_flag": "--prompt"
      },
      "model_flag": ["--model"],
      "workdir_flag": ["-C"],
      "adddir_flag": ["--add-dir"]
    }
    """
    raw = os.environ.get("STEWARD_LLM_CMD_TEMPLATE", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def build_agent_cmd(
    mode: str = "read_only",
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    working_dir: Optional[Path] = None,
    add_dir: Optional[Path] = None,
    prompt: Optional[str] = None,
) -> tuple[list[str], bool]:
    """Build the CLI command array for a given mode and provider.

    Args:
        mode: "read_only" or "write"
        provider: CLI provider name (default: auto-detect)
        model: model name/alias to pass
        working_dir: working directory for the agent
        add_dir: additional directory to give the agent access to
        prompt: the prompt text (appended as arg if stdin=False)

    Returns:
        (cmd_list, uses_stdin) — the command array and whether to pipe
        prompt via stdin (True) or it's already in the command (False).
    """
    if provider is None:
        provider = detect_provider()

    # Load template: known provider or custom
    custom = _load_custom_template()
    if custom and provider not in _PROVIDERS:
        template = custom.get(mode)
        if not template:
            raise ValueError(
                f"Custom template missing mode '{mode}'. "
                f"Set STEWARD_LLM_CMD_TEMPLATE with both 'read_only' and 'write' keys."
            )
        model_flag = custom.get("model_flag", ["-m"])
        workdir_flag = custom.get("workdir_flag", ["-C"])
        adddir_flag = custom.get("adddir_flag", ["--add-dir"])
    elif provider in _PROVIDERS:
        template = _PROVIDERS[provider][mode]
        model_flag = _MODEL_FLAG.get(provider, ["-m"])
        workdir_flag = _WORKDIR_FLAG.get(provider, ["-C"])
        adddir_flag = _ADDDIR_FLAG.get(provider, ["--add-dir"])
    else:
        raise ValueError(
            f"Unknown provider '{provider}' and no custom template set. "
            f"Known providers: {list(_PROVIDERS.keys())}. "
            f"For custom CLIs, set STEWARD_LLM_CMD_TEMPLATE env var."
        )

    cmd = list(template["cmd"])
    uses_stdin = template.get("stdin", True)
    prompt_flag = template.get("prompt_flag")

    # Add optional flags
    if model:
        cmd.extend(model_flag + [model])

    if working_dir:
        cmd.extend(workdir_flag + [str(working_dir)])

    if add_dir:
        cmd.extend(adddir_flag + [str(add_dir)])

    # Add prompt
    if uses_stdin:
        cmd.append("-")  # stdin marker for codex-style CLIs
    elif prompt_flag and prompt is not None:
        cmd.extend([prompt_flag, prompt])

    return cmd, uses_stdin


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def _kill_process_group(proc: subprocess.Popen) -> None:
    """Terminate a CLI process and any descendants it spawned."""
    if proc.poll() is not None:
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return

    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            return


# ---------------------------------------------------------------------------
# High-level API
# ---------------------------------------------------------------------------

def call_llm(
    prompt: str,
    *,
    search: bool = False,
    timeout: int = 900,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> str:
    """Call a CLI agent in read-only mode and return raw output.

    Uses stdin piping for providers that support it (codex),
    or -p flag for those that don't (claude).
    """
    # Always pass prompt — build_agent_cmd includes it only for non-stdin providers
    cmd, uses_stdin = build_agent_cmd(
        mode="read_only",
        provider=provider,
        model=model,
        prompt=prompt,
    )

    # For codex with --search flag, inject before "exec"
    if search and "codex" in cmd[0]:
        idx = cmd.index("exec") if "exec" in cmd else 1
        cmd.insert(idx, "--search")

    if uses_stdin:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(proc)
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(
                cmd, timeout, output=stdout, stderr=stderr
            ) from exc
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(proc)
            stdout, stderr = proc.communicate()
            raise subprocess.TimeoutExpired(
                cmd, timeout, output=stdout, stderr=stderr
            ) from exc

    output = stdout.strip()
    if not output:
        stderr_hint = stderr.strip()[:300] if stderr else ""
        raise RuntimeError(
            f"LLM returned empty output (exit {proc.returncode}). "
            f"stderr: {stderr_hint}"
        )
    return output


def call_llm_json(prompt: str, **kwargs) -> dict:
    """Call LLM and extract JSON from ```json fenced block."""
    output = call_llm(prompt, **kwargs)
    return extract_json(output)


def call_agent_write(
    prompt: str,
    *,
    working_dir: Path,
    add_dir: Optional[Path] = None,
    timeout: int = 600,
    model: Optional[str] = None,
    provider: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Call a CLI agent in write mode with filesystem access.

    Used by reactive diagnosis and reflection durable fix agents.
    Returns the CompletedProcess for the caller to parse output.
    """
    # Always pass prompt — build_agent_cmd includes it only for non-stdin providers
    cmd, uses_stdin = build_agent_cmd(
        mode="write",
        provider=provider,
        model=model,
        working_dir=working_dir,
        add_dir=add_dir,
        prompt=prompt,
    )

    if uses_stdin:
        return subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(working_dir),
        )
    else:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(working_dir),
        )


def extract_json(text: str) -> dict:
    """Extract JSON from ```json fenced block or raw JSON."""
    m = re.search(r'```json\s*\n(.*?)\n\s*```', text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    # Fallback: try parsing the whole output as JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ValueError(f"No JSON found in LLM output:\n{text[:500]}")
