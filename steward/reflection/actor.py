"""Phase 4 — Actions.

Executes remediation actions: durable fixes, smoke tests, config adjustments.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def g11_fix_regression_check(
    proposed_fix_desc: str,
    fix_history: list[dict],
    similarity_threshold: float = 0.5,
) -> tuple[bool, str]:
    """G11 — Check if a similar fix was already tried and failed.

    Returns (should_proceed, reason).
    """
    from difflib import SequenceMatcher

    for prior_fix in fix_history:
        if prior_fix.get("outcome") == "failed":
            prior_desc = prior_fix.get("description", "")
            sim = SequenceMatcher(None, proposed_fix_desc.lower(), prior_desc.lower()).ratio()
            if sim > similarity_threshold:
                return False, (
                    f"G11: Similar fix was tried on {prior_fix.get('date')} "
                    f"and failed (similarity={sim:.2f}): {prior_desc[:80]}"
                )
    return True, "G11: No similar failed fixes found"


def g12_disable_safety_check(
    task_id: str,
    charter: dict,
    days_failing: int,
    fixes_tried: int,
) -> tuple[bool, str]:
    """G12 — Gate auto-disable behind multiple conditions.

    Returns (can_disable, reason).
    """
    if charter.get("reflection", {}).get("critical", False):
        return False, f"G12: {task_id} is marked critical, cannot auto-disable"

    if not charter.get("reflection", {}).get("auto_disable", False):
        return False, f"G12: {task_id} does not have reflection.auto_disable: true"

    if days_failing < 7:
        return False, f"G12: {task_id} only failing {days_failing} days (need 7+)"

    if fixes_tried < 5:
        return False, f"G12: Only {fixes_tried} fixes tried (need 5+)"

    return True, f"G12: {task_id} eligible for auto-disable ({days_failing} days, {fixes_tried} fixes)"


# ---------------------------------------------------------------------------
# Fix Actions
# ---------------------------------------------------------------------------

_FIX_AGENT_TIMEOUT = 600  # 10 minutes per fix agent

def _spawn_durable_fix_agent(
    task_id: str,
    task_path: str,
    instance_root: Path,
    pattern: dict,
    task_health: dict,
) -> dict:
    """Spawn a Claude Code agent to apply a targeted fix.

    Uses programmatic diagnosis (diagnoser.py) to give the agent a focused
    starting point instead of making it explore the codebase from scratch.
    """
    from datetime import datetime
    from .diagnoser import diagnose_task, format_diagnosis_for_prompt

    task_dir = instance_root / task_path
    health = task_health.get(task_id, {})

    # Programmatic diagnosis — gather symptoms WITHOUT LLM exploration
    date_str = datetime.now().strftime("%Y-%m-%d")
    diagnosis = diagnose_task(
        instance_root=instance_root,
        task_id=task_id,
        task_path=task_path,
        apparent_issue=pattern.get("root_cause", "No root cause identified"),
        is_stale=pattern.get("is_stale", False),
        date_str=date_str,
    )
    diagnosis_block = format_diagnosis_for_prompt(diagnosis)

    prompt = f"""\
You are the orchestrator's TARGETED FIX agent. A task failed to produce
meaningful output today. The diagnosis below tells you exactly what to look at —
do NOT re-explore the entire codebase.

TASK: {task_id}
PATH: {task_path}

OUTPUT ASSESSOR EVIDENCE:
  {pattern.get("evidence", "")}

{diagnosis_block}

YOUR JOB:
1. Read ONLY the files listed in "START WITH THESE FILES" above
2. Use the symptoms, log signals, and state anomalies as your starting point
3. If prior fix attempts are listed, do NOT repeat them — try a different angle
4. Apply a targeted, minimal fix
5. Verify with a quick sanity check (e.g., python -c "import run")
6. You have write access to files under {task_dir}

DO NOT:
- grep the whole codebase
- read every file in the task directory
- ignore the prior diagnoses (the symptoms repeat across attempts)

Output EXACTLY one JSON block (fenced with ```json ... ```) with:
```json
{{
  "diagnosis": "What you found as the root cause (be specific)",
  "fix_applied": true,
  "fix_description": "What you changed and why",
  "files_modified": ["list of files changed"],
  "confidence": "high|medium|low"
}}
```"""

    print(f"  [reflect] Spawning durable fix agent for {task_id}...", file=sys.stderr)

    try:
        from ..llm import call_agent_write
        proc = call_agent_write(
            prompt,
            working_dir=task_dir,
            add_dir=instance_root,
            timeout=_FIX_AGENT_TIMEOUT,
        )
        output = proc.stdout or ""

        # Extract JSON from output
        import re
        json_match = re.search(r"```json\s*\n(.*?)\n\s*```", output, re.DOTALL)
        if not json_match:
            json_match = re.search(r'\{[^{}]*"diagnosis"[^{}]*\}', output)

        if json_match:
            raw = json_match.group(1) if "```" in json_match.group(0) else json_match.group(0)
            parsed = json.loads(raw)
            result = {
                "success": True,
                "fix_applied": parsed.get("fix_applied", False),
                "fix_description": parsed.get("fix_description", ""),
                "files_modified": parsed.get("files_modified", []),
                "confidence": parsed.get("confidence", "low"),
            }
        else:
            result = {
                "success": False,
                "error": "Agent produced no structured output",
                "fix_applied": False,
                "raw_output": output[-500:],
            }

        status = "fix applied" if result.get("fix_applied") else "no fix"
        print(f"  [reflect] Durable fix for {task_id}: {status}", file=sys.stderr)
        return result

    except subprocess.TimeoutExpired:
        print(f"  [reflect] Durable fix agent timed out for {task_id}", file=sys.stderr)
        return {"success": False, "error": "timed out", "fix_applied": False}
    except Exception as e:
        print(f"  [reflect] Durable fix agent error for {task_id}: {e}", file=sys.stderr)
        return {"success": False, "error": str(e), "fix_applied": False}


def _run_smoke_test(
    task_id: str,
    task_path: str,
    instance_root: Path,
    charter: dict,
) -> dict:
    """Run a smoke test command for a task. Returns {passed, output}."""
    smoke_cmd = charter.get("reflection", {}).get("smoke_test")

    if not smoke_cmd:
        # Default: try to import run.py
        run_py = instance_root / task_path / "run.py"
        if run_py.exists():
            smoke_cmd = "python -c 'import run'"
        else:
            return {"passed": True, "output": "no smoke test configured", "skipped": True}

    task_dir = instance_root / task_path
    env = os.environ.copy()
    env["STEWARD_INSTANCE_ROOT"] = str(instance_root)

    try:
        proc = subprocess.run(
            ["bash", "-lc", smoke_cmd],
            cwd=str(task_dir),
            capture_output=True, text=True,
            timeout=30,
            env=env,
        )
        passed = proc.returncode == 0
        output = proc.stdout[-500:] if proc.stdout else ""
        if not passed:
            output += "\n" + (proc.stderr[-500:] if proc.stderr else "")

        status = "PASS" if passed else "FAIL"
        print(f"  [reflect] Smoke test {task_id}: {status}", file=sys.stderr)
        return {"passed": passed, "output": output.strip(), "skipped": False}

    except subprocess.TimeoutExpired:
        print(f"  [reflect] Smoke test {task_id}: TIMEOUT", file=sys.stderr)
        return {"passed": False, "output": "timed out after 30s", "skipped": False}
    except Exception as e:
        return {"passed": False, "output": str(e), "skipped": False}


# ---------------------------------------------------------------------------
# Main Actor
# ---------------------------------------------------------------------------

def execute_reflection_actions(
    ctx: dict,
    failure_patterns: list[dict],
    engagement_analysis: dict,
    value_assessments: dict,
    instance_root: Path,
    max_fix_agents: int = 2,
) -> list[dict]:
    """Execute remediation actions based on analysis results.

    Returns list of ActionResult dicts.
    """
    actions_taken = []
    task_health = ctx["task_health"]
    fix_agents_spawned = 0

    # --- Action 1: Durable fixes for persistent failures ---
    for pattern in failure_patterns:
        if pattern.get("fix_type") not in ("code", "config"):
            continue
        if fix_agents_spawned >= max_fix_agents:
            break

        for task_id in pattern["affected_tasks"]:
            if fix_agents_spawned >= max_fix_agents:
                break

            health = task_health.get(task_id, {})
            # No days_failing threshold — the output assessor already decided
            # this task needs fixing. Only G11 (regression check) can block.

            # G11: Check for fix regression
            fix_history = health.get("fix_history", [])
            proposed = pattern.get("durable_fix_suggestion", "")
            should_proceed, g11_reason = g11_fix_regression_check(proposed, fix_history)
            if not should_proceed:
                actions_taken.append({
                    "type": "fix_skipped",
                    "task_id": task_id,
                    "reason": g11_reason,
                })
                continue

            # Spawn fix agent
            task_entry = None
            registry_file = instance_root / "tasks" / "registry.yaml"
            if registry_file.exists():
                with open(registry_file) as f:
                    registry = yaml.safe_load(f) or {}
                for t in registry.get("tasks", []):
                    if t.get("id") == task_id:
                        task_entry = t
                        break

            if not task_entry:
                continue

            fix_result = _spawn_durable_fix_agent(
                task_id, task_entry["path"], instance_root, pattern, task_health,
            )

            fix_id = f"fix_{ctx['date']}_{task_id}_{fix_agents_spawned}"
            action = {
                "type": "durable_fix",
                "task_id": task_id,
                "fix_id": fix_id,
                "pattern_id": pattern.get("pattern_id"),
                "fix_applied": fix_result.get("fix_applied", False),
                "fix_description": fix_result.get("fix_description", ""),
                "confidence": fix_result.get("confidence", "low"),
            }

            # Run smoke test if fix was applied
            if fix_result.get("fix_applied"):
                charter_path = instance_root / task_entry["path"] / "charter.yaml"
                charter = {}
                if charter_path.exists():
                    with open(charter_path) as f:
                        charter = yaml.safe_load(f) or {}

                smoke = _run_smoke_test(task_id, task_entry["path"], instance_root, charter)
                action["smoke_test"] = smoke.get("passed", False)
                action["smoke_test_output"] = smoke.get("output", "")

                # Record in reflection state
                from .state import load_reflection_state, save_reflection_state
                rstate = load_reflection_state(instance_root)
                rstate.setdefault("applied_fixes", []).append({
                    "id": fix_id,
                    "date": ctx["date"],
                    "task": task_id,
                    "description": fix_result.get("fix_description", ""),
                    "smoke_test": "pass" if smoke.get("passed") else "fail",
                    "outcome": "pending",
                    "days_until_recurrence": None,
                })
                save_reflection_state(instance_root, rstate)

            actions_taken.append(action)
            fix_agents_spawned += 1

    # --- Action 2: Update fix outcomes from prior reflections ---
    from .state import load_reflection_state, save_reflection_state
    rstate = load_reflection_state(instance_root)
    for fix in rstate.get("applied_fixes", []):
        if fix.get("outcome") != "pending":
            continue
        # Check if the task succeeded since the fix
        tid = fix.get("task")
        health = task_health.get(tid, {})
        fix_date = fix.get("date", "")

        if health.get("last_success_date", "") > fix_date:
            fix["outcome"] = "success"
            actions_taken.append({
                "type": "fix_outcome_updated",
                "task_id": tid,
                "fix_id": fix.get("id"),
                "outcome": "success",
            })
        elif health.get("days_failing", 0) > 0 and fix_date < ctx["date"]:
            # Task still failing after fix was applied on a prior day
            days_since_fix = 0
            try:
                from datetime import datetime
                d1 = datetime.strptime(fix_date, "%Y-%m-%d")
                d2 = datetime.strptime(ctx["date"], "%Y-%m-%d")
                days_since_fix = (d2 - d1).days
            except ValueError:
                pass

            if days_since_fix >= 2:
                fix["outcome"] = "failed"
                fix["days_until_recurrence"] = days_since_fix
                actions_taken.append({
                    "type": "fix_outcome_updated",
                    "task_id": tid,
                    "fix_id": fix.get("id"),
                    "outcome": "failed",
                })

    save_reflection_state(instance_root, rstate)

    # --- Action 3: Update failure streaks ---
    from .state import update_failure_streaks
    update_failure_streaks(instance_root, task_health)

    # --- Action 4: Update engagement history ---
    engagement_snapshot = {}
    for tid, eng in engagement_analysis.items():
        engagement_snapshot[tid] = {
            "days_since_reply": eng.get("days_since_reply"),
            "report_quality": eng.get("report_quality_score"),
        }
    if engagement_snapshot:
        from .state import update_engagement_history
        update_engagement_history(instance_root, engagement_snapshot, ctx["date"])

    # --- Action 5: Update value tiers ---
    if value_assessments:
        rstate = load_reflection_state(instance_root)
        tiers = rstate.setdefault("task_value_tiers", {})
        for tid, assessment in value_assessments.items():
            tiers[tid] = {
                "tier": assessment.get("tier", "medium"),
                "rationale": assessment.get("rationale", ""),
                "assessed": ctx["date"],
            }
        save_reflection_state(instance_root, rstate)

    return actions_taken
