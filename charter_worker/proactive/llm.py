"""Thin LLM abstraction — all calls go through here so provider is swappable."""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional


def call_llm(
    prompt: str,
    *,
    search: bool = False,
    timeout: int = 900,
    model: Optional[str] = None,
) -> str:
    """Call codex exec and return raw output.

    Uses stdin piping for the prompt (robust for any length/encoding).
    """
    if not shutil.which("codex"):
        raise RuntimeError("codex CLI not found on PATH. Install: npm i -g @openai/codex")

    cmd = ["codex"]
    if search:
        cmd.append("--search")
    cmd.extend(["exec", "--ephemeral", "-s", "read-only"])
    if model:
        cmd.extend(["-m", model])
    cmd.append("-")  # read prompt from stdin

    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout.strip()
    if not output:
        stderr_hint = result.stderr.strip()[:300] if result.stderr else ""
        raise RuntimeError(
            f"LLM returned empty output (exit {result.returncode}). "
            f"stderr: {stderr_hint}"
        )
    return output


def call_llm_json(prompt: str, **kwargs) -> dict:
    """Call LLM and extract JSON from ```json fenced block."""
    output = call_llm(prompt, **kwargs)
    return extract_json(output)


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
