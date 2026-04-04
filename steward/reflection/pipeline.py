"""Main reflection pipeline — entry point for the orchestrator.

Runs every hour until all tasks have meaningful output for today.
Coordinates: detect → fix → re-run → re-assess → report → persist.

The orchestrator calls:
  - run_detect() every hour (cheap — one LLM call, returns unresolved tasks)
  - run_reflection() when unresolved tasks exist (expensive — spawns fix agents)
"""

import subprocess
import sys
import time
from pathlib import Path

from .collector import collect_reflection_data
from .output_assessor import assess_output_quality
from .analyzer import analyze_engagement, assess_task_value
from .actor import execute_reflection_actions
from .report import generate_health_report
from .state import load_reflection_state, save_reflection_state


_PIPELINE_TIMEOUT = 9000  # 2.5 hours — enough for N+3 fix agents at 700s each


def run_detect(instance_root: Path, date_str: str) -> dict:
    """Cheap detection pass — assess output quality for all tasks.

    Called hourly by the orchestrator to check if reflection is needed.
    Returns:
        all_resolved: bool — True if every task has meaningful output
        unresolved: list[str] — task IDs with no meaningful output
        health_report_md: str — markdown for digest injection
    """
    started = time.time()
    assessments, failure_patterns = assess_output_quality(instance_root, date_str)

    unresolved = [a.task_id for a in assessments if not a.has_meaningful_output]
    all_resolved = len(unresolved) == 0

    # Build a minimal report for the digest
    ctx = {"output_assessments": assessments, "task_health": {}, "date": date_str}
    report_md = ""
    if not all_resolved:
        from .report import generate_health_report
        report_md = generate_health_report(ctx, failure_patterns, {}, {}, [])

    elapsed = time.time() - started
    status = "all resolved" if all_resolved else f"{len(unresolved)} unresolved"
    print(f"  [reflect] Detect pass: {status} ({elapsed:.0f}s)", file=sys.stderr)

    return {
        "all_resolved": all_resolved,
        "unresolved": unresolved,
        "health_report_md": report_md,
        "duration_s": elapsed,
    }


def _rerun_task(instance_root: Path, task_id: str, date_str: str,
                timeout: int = 600) -> bool:
    """Re-run a task after a fix to verify it produces output.

    Returns True if the task ran successfully (exit 0).
    """
    import os
    import yaml

    registry_file = instance_root / "tasks" / "registry.yaml"
    if not registry_file.exists():
        return False
    with open(registry_file) as f:
        registry = yaml.safe_load(f) or {}

    task_entry = None
    for t in registry.get("tasks", []):
        if t["id"] == task_id:
            task_entry = t
            break
    if not task_entry:
        return False

    task_path = task_entry["path"]
    task_dir = instance_root / task_path

    # Load charter for execution config
    charter_path = task_dir / "charter.yaml"
    charter = {}
    if charter_path.exists():
        with open(charter_path) as f:
            charter = yaml.safe_load(f) or {}

    execution = charter.get("execution", {})
    entrypoint = execution.get("entrypoint", "python run.py")
    summary_dir = instance_root / "daily_summaries" / date_str / "tasks" / task_id

    env = os.environ.copy()
    env["STEWARD_INSTANCE_ROOT"] = str(instance_root)
    env["STEWARD_RUN_DATE"] = date_str
    env["STEWARD_SUMMARY_DIR"] = str(summary_dir)
    env["STEWARD_TASK_ID"] = task_id
    env["STEWARD_TASK_PATH"] = task_path

    print(f"  [reflect] Re-running {task_id} to verify fix...", file=sys.stderr)
    try:
        proc = subprocess.run(
            ["bash", "-lc", entrypoint],
            cwd=task_dir,
            env=env,
            capture_output=True,
            timeout=timeout,
        )
        success = proc.returncode == 0
        status = "exit 0" if success else f"exit {proc.returncode}"
        print(f"  [reflect] Re-run {task_id}: {status}", file=sys.stderr)
        return success
    except subprocess.TimeoutExpired:
        print(f"  [reflect] Re-run {task_id}: timed out after {timeout}s", file=sys.stderr)
        return False
    except Exception as e:
        print(f"  [reflect] Re-run {task_id}: error {e}", file=sys.stderr)
        return False


def run_reflection(
    instance_root: Path,
    date_str: str,
    orchestrator_state: dict,
) -> dict:
    """Run the daily reflection pipeline.

    Called by orchestrator.main() at REFLECTION_HOUR.

    Returns dict with:
        health_report_md: str — markdown for digest injection
        patterns_found: int
        fixes_applied: int
        duration_s: float
    """
    started = time.time()

    # --- Phase 1: Output-based assessment ---
    # Read the same summaries the user reads and judge: meaningful output?
    assessments, failure_patterns = assess_output_quality(instance_root, date_str)

    # --- Phase 2: Collect supporting data (engagement, email, health) ---
    ctx = collect_reflection_data(instance_root, date_str, lookback_days=7)
    ctx["output_assessments"] = assessments

    any_email_loop = any(
        e.get("has_email_loop")
        for e in ctx["engagement"].values()
    )

    # Check time budget
    if time.time() - started > _PIPELINE_TIMEOUT * 0.6:
        print("  [reflect] Time budget 60% consumed, skipping engagement/value analysis",
              file=sys.stderr)
        engagement_analysis = {}
        value_assessments = {}
    else:
        # --- Phase 3: Analyze engagement ---
        engagement_analysis = {}
        if any_email_loop:
            engagement_analysis = analyze_engagement(ctx)

        # --- Phase 4: Assess value ---
        value_assessments = assess_task_value(ctx)

    # --- Phase 5: Act ---
    # Budget: N+3 fix agents where N = number of registered tasks.
    # Every flagged task deserves a fix attempt — capacity should not be
    # the reason a broken task goes unfixed.
    n_tasks = len(ctx.get("task_health", {}))
    remaining_time = _PIPELINE_TIMEOUT - (time.time() - started)
    max_fixes = 0
    if remaining_time > 900:  # need at least 15 min for fix agents
        max_fixes = min(n_tasks + 3, int(remaining_time / 700))

    actions_taken = execute_reflection_actions(
        ctx,
        failure_patterns,
        engagement_analysis,
        value_assessments,
        instance_root,
        max_fix_agents=max_fixes,
    )

    # --- Phase 5b: Re-run fixed tasks and re-assess ---
    fixed_tasks = [
        a["task_id"] for a in actions_taken
        if a.get("type") == "durable_fix" and a.get("fix_applied")
    ]
    rerun_results = {}
    for task_id in fixed_tasks:
        remaining = _PIPELINE_TIMEOUT - (time.time() - started)
        if remaining < 120:
            print(f"  [reflect] Time budget exhausted, skipping re-run for {task_id}",
                  file=sys.stderr)
            break
        rerun_ok = _rerun_task(instance_root, task_id, date_str,
                               timeout=min(600, int(remaining - 60)))
        rerun_results[task_id] = rerun_ok

    # Re-assess after re-runs
    if fixed_tasks:
        post_assessments, post_failures = assess_output_quality(instance_root, date_str)
        still_broken = [a.task_id for a in post_assessments if not a.has_meaningful_output]
        newly_resolved = [t for t in fixed_tasks if t not in still_broken]
        for a in actions_taken:
            if a.get("task_id") in newly_resolved:
                a["verified"] = True
            elif a.get("task_id") in still_broken and a.get("task_id") in fixed_tasks:
                a["verified"] = False
        if still_broken:
            print(f"  [reflect] Post-fix: {len(newly_resolved)} resolved, "
                  f"{len(still_broken)} still broken: {still_broken}", file=sys.stderr)
        else:
            print(f"  [reflect] Post-fix: all {len(newly_resolved)} fixes verified",
                  file=sys.stderr)
        # Update assessments for the report
        ctx["output_assessments"] = post_assessments
        failure_patterns = post_failures

    # --- Phase 6: Report ---
    report_md = generate_health_report(
        ctx, failure_patterns, engagement_analysis, value_assessments, actions_taken,
    )

    # --- Phase 7: Persist ---
    _update_reflection_count(instance_root, date_str)

    # Store value assessments
    rstate = load_reflection_state(instance_root)
    if value_assessments:
        tiers = rstate.setdefault("task_value_tiers", {})
        for tid, v in value_assessments.items():
            tiers[tid] = {
                "tier": v.get("tier", "medium"),
                "rationale": v.get("rationale", ""),
                "assessed": date_str,
            }

    # Store system patterns
    rstate["patterns"] = [
        {
            "id": p.get("id", f"pattern_{i}"),
            "first_seen": p.get("first_seen", date_str),
            "last_seen": date_str,
            "occurrences": p.get("occurrences", 1),
            "affected_tasks": p.get("affected_tasks", []),
            "status": "open",
            "description": p.get("description", ""),
        }
        for i, p in enumerate(ctx.get("system_patterns", []))
    ]

    save_reflection_state(instance_root, rstate)

    elapsed = time.time() - started
    fixes_applied = sum(1 for a in actions_taken if a.get("type") == "durable_fix" and a.get("fix_applied"))
    print(f"  [reflect] Reflection complete in {elapsed:.0f}s "
          f"({len(failure_patterns)} patterns, {fixes_applied} fixes applied)",
          file=sys.stderr)

    # Determine if all tasks are resolved after this cycle
    final_unresolved = [
        a.task_id for a in ctx.get("output_assessments", [])
        if not a.has_meaningful_output
    ]

    return {
        "health_report_md": report_md,
        "patterns_found": len(failure_patterns),
        "fixes_applied": fixes_applied,
        "all_resolved": len(final_unresolved) == 0,
        "unresolved": final_unresolved,
        "duration_s": elapsed,
    }


def _update_reflection_count(instance_root: Path, date_str: str):
    """Update the reflection count and date in state."""
    rstate = load_reflection_state(instance_root)
    rstate["last_reflection_date"] = date_str
    rstate["reflection_count"] = rstate.get("reflection_count", 0) + 1
    save_reflection_state(instance_root, rstate)
