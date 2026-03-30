"""Main reflection pipeline — entry point for the orchestrator.

Runs once per day at 4 AM. Coordinates: collect → analyze → act → report → persist.
"""

import sys
import time
from pathlib import Path

from .collector import collect_reflection_data
from .analyzer import analyze_failure_patterns, analyze_engagement, assess_task_value
from .actor import execute_reflection_actions
from .report import generate_health_report
from .state import load_reflection_state, save_reflection_state


_PIPELINE_TIMEOUT = 2700  # 45 minutes max for entire reflection


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

    # --- Phase 1: Collect ---
    ctx = collect_reflection_data(instance_root, date_str, lookback_days=7)

    # Quick exit: if all tasks are healthy, skip analysis
    any_persistent = any(
        h.get("days_failing", 0) >= 2
        for h in ctx["task_health"].values()
    )
    any_email_loop = any(
        e.get("has_email_loop")
        for e in ctx["engagement"].values()
    )

    if not any_persistent and not any_email_loop and not ctx["system_patterns"]:
        print("  [reflect] All tasks healthy, skipping deep analysis", file=sys.stderr)
        # Still generate a minimal report
        report_md = generate_health_report(ctx, [], {}, {}, [])
        _update_reflection_count(instance_root, date_str)
        elapsed = time.time() - started
        print(f"  [reflect] Reflection complete in {elapsed:.0f}s (quick exit)", file=sys.stderr)
        return {
            "health_report_md": report_md,
            "patterns_found": 0,
            "fixes_applied": 0,
            "duration_s": elapsed,
        }

    # --- Phase 2: Analyze failures ---
    failure_patterns = []
    if any_persistent or ctx["system_patterns"]:
        failure_patterns = analyze_failure_patterns(ctx)

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
    # Only act if we have time left
    remaining_time = _PIPELINE_TIMEOUT - (time.time() - started)
    max_fixes = 0
    if remaining_time > 900:  # need at least 15 min for fix agents
        max_fixes = min(2, int(remaining_time / 700))

    actions_taken = execute_reflection_actions(
        ctx,
        failure_patterns,
        engagement_analysis,
        value_assessments,
        instance_root,
        max_fix_agents=max_fixes,
    )

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

    return {
        "health_report_md": report_md,
        "patterns_found": len(failure_patterns),
        "fixes_applied": fixes_applied,
        "duration_s": elapsed,
    }


def _update_reflection_count(instance_root: Path, date_str: str):
    """Update the reflection count and date in state."""
    rstate = load_reflection_state(instance_root)
    rstate["last_reflection_date"] = date_str
    rstate["reflection_count"] = rstate.get("reflection_count", 0) + 1
    save_reflection_state(instance_root, rstate)
