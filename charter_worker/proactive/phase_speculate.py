"""Phase 5 — Speculative Pre-computation.

Lightweight exploration of top action directions.
Results stored in speculative buffer (never in main status until promoted).
"""

import sys
import time
from pathlib import Path
from typing import Optional

import yaml

from .llm import call_llm_json
from .guardrails import g9_validate_isolation, GuardrailResult


def speculate(
    context: dict,
    project_dir: Path,
    max_directions: int = 2,
    timeout_per_direction: int = 300,
    total_budget_seconds: Optional[float] = None,
    elapsed_seconds: float = 0,
) -> tuple[dict, list[GuardrailResult]]:
    """Run speculative pre-computation on top action directions.

    Args:
        context: Updated context (with synthesized status)
        project_dir: Path to project directory
        max_directions: Max directions to explore
        timeout_per_direction: Seconds per speculation query
        total_budget_seconds: Total budget for this project (skip if exceeded)
        elapsed_seconds: Time already spent on this project

    Returns:
        (buffer_dict, guardrail_results)
    """
    guardrail_results = []
    status = context["status"]

    # Budget check
    if total_budget_seconds and elapsed_seconds > total_budget_seconds * 0.8:
        print("  [phase5] Budget exceeded, skipping speculation", file=sys.stderr)
        return {"speculative_threads": []}, []

    suggestions = status.get("action_suggestions", [])
    if not suggestions:
        return {"speculative_threads": []}, []

    # Pick top directions
    directions = suggestions[:max_directions]
    threads = []

    for direction in directions:
        action = direction.get("action", "")
        print(f"  [phase5] Speculating on: {action[:60]}...", file=sys.stderr)

        prompt = f"""Do a quick preliminary investigation of this research direction:
"{action}"

Context: {context['definition'].get('goal', '')}

Do a brief search and provide preliminary findings. This is exploratory — no need for thoroughness.

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "preliminary_findings": "2-3 sentences of what you found",
  "promising": true,
  "key_leads": ["lead 1", "lead 2"],
  "sources": ["url1"]
}}"""

        try:
            result = call_llm_json(prompt, search=True, timeout=timeout_per_direction)
            threads.append({
                "direction": action,
                "queries_run": [action],
                "preliminary_findings": result.get("preliminary_findings", ""),
                "promising": result.get("promising", False),
                "key_leads": result.get("key_leads", []),
                "sources": result.get("sources", []),
                "promoted": False,
            })
        except Exception as e:
            print(f"  [phase5] Speculation failed for '{action[:40]}': {e}", file=sys.stderr)
            threads.append({
                "direction": action,
                "queries_run": [action],
                "preliminary_findings": f"Failed: {e}",
                "promising": False,
                "promoted": False,
            })

    buffer = {"speculative_threads": threads}

    # G9: Validate isolation
    g9_result = g9_validate_isolation(threads, status.get("key_findings", []))
    guardrail_results.append(g9_result)

    # Write buffer
    buffer_path = project_dir / "speculative_buffer.yaml"
    with open(buffer_path, "w") as f:
        yaml.dump(buffer, f, default_flow_style=False)

    return buffer, guardrail_results
