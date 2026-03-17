"""CLI agent session launcher.

Delegates tasks to a full CLI agent (codex, claude) that can read files,
edit code, run commands, and iterate. This is NOT a single LLM prompt —
it's a full agent session with workspace access.

The agent gets:
- A workspace directory (full read/write access)
- A task description (what to do)
- Access to plan files, logs, code, outputs

The agent returns:
- A result JSON written to a specified output file
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


def detect_agent() -> str:
    """Detect which CLI agent is available."""
    if shutil.which("codex"):
        return "codex"
    if shutil.which("claude"):
        return "claude"
    raise RuntimeError("No CLI agent found. Install codex or claude.")


def run_agent_session(
    workspace: Path,
    task_prompt: str,
    *,
    agent: str = "auto",
    result_file: str = ".agent_result.json",
    timeout: int = 1800,
    approval_mode: str = "full-auto",
) -> dict:
    """Launch a full CLI agent session in a workspace.

    The agent gets full access to the workspace and is instructed to write
    its result to result_file. The runner reads this file to understand
    what the agent did.

    Args:
        workspace: Directory the agent works in.
        task_prompt: Natural language task description.
        agent: "codex", "claude", or "auto" (detect).
        result_file: Where the agent writes its structured result.
        timeout: Max seconds for the session.
        approval_mode: Agent autonomy level.

    Returns:
        Dict with {status, result, agent_output, duration_s}.
    """
    if agent == "auto":
        agent = detect_agent()

    result_path = workspace / result_file

    # Clean previous result
    if result_path.exists():
        result_path.unlink()

    # Build the full prompt — tell the agent what to do AND where to write results
    full_prompt = f"""{task_prompt}

IMPORTANT: When you are done, write a JSON summary of what you did to:
  {result_file}

The JSON must contain:
{{
  "status": "success" or "failed" or "needs_human",
  "summary": "1-2 sentence summary of what you did",
  "actions_taken": ["action 1", "action 2"],
  "files_modified": ["file1", "file2"],
  "errors": ["error1"] or [],
  "metrics": {{}} or null,
  "next_suggestion": "what to do next" or null
}}
"""

    # Build command based on agent type
    if agent == "codex":
        cmd = [
            "codex", "--approval-mode", approval_mode,
            "--quiet",
            prompt_to_codex_arg(full_prompt),
        ]
    elif agent == "claude":
        cmd = [
            "claude", "--print", "-p", full_prompt,
            "--allowedTools", "Bash(command:*)", "Read", "Write", "Edit", "Glob", "Grep",
        ]
    else:
        raise ValueError(f"Unknown agent: {agent}")

    print(f"  [agent] Launching {agent} session in {workspace}", file=sys.stderr)
    print(f"  [agent] Task: {task_prompt[:100]}...", file=sys.stderr)

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.time() - start
        agent_output = proc.stdout

        print(f"  [agent] Session completed ({duration:.1f}s, exit {proc.returncode})", file=sys.stderr)

        # Read the result file the agent was supposed to write
        if result_path.exists():
            with open(result_path) as f:
                result = json.load(f)
        else:
            # Agent didn't write result file — extract what we can
            result = {
                "status": "success" if proc.returncode == 0 else "failed",
                "summary": "Agent completed but did not write result file",
                "actions_taken": [],
                "errors": [proc.stderr[:500]] if proc.returncode != 0 else [],
            }

        return {
            "status": result.get("status", "unknown"),
            "result": result,
            "agent_output": agent_output[:2000],
            "duration_s": round(duration, 1),
            "exit_code": proc.returncode,
        }

    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "result": {"status": "timeout", "summary": f"Agent timed out after {timeout}s"},
            "agent_output": "",
            "duration_s": timeout,
            "exit_code": -1,
        }

    except Exception as e:
        return {
            "status": "error",
            "result": {"status": "error", "summary": str(e)},
            "agent_output": "",
            "duration_s": time.time() - start,
            "exit_code": -2,
        }


def prompt_to_codex_arg(prompt: str) -> str:
    """Format prompt for codex CLI. Codex takes the prompt as a positional arg."""
    # codex --full-auto "prompt here"
    # For very long prompts, codex reads from stdin via pipe, but
    # the simple case works with a positional arg
    return prompt
