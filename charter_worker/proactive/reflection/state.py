"""Reflection state persistence.

Manages reflection_state.json at the instance root.
Tracks multi-day failure patterns, fix outcomes, engagement history.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional


def _state_path(instance_root: Path) -> Path:
    return instance_root / "reflection_state.json"


def load_reflection_state(instance_root: Path) -> dict:
    """Load reflection state, returning empty structure if missing."""
    p = _state_path(instance_root)
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {
        "last_reflection_date": "",
        "reflection_count": 0,
        "diagnosis_history": {},
        "failure_streaks": {},
        "applied_fixes": [],
        "engagement_history": {},
        "task_value_tiers": {},
        "patterns": [],
    }


def save_reflection_state(instance_root: Path, state: dict):
    """Write reflection state atomically."""
    p = _state_path(instance_root)
    tmp = p.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.replace(p)


def append_diagnosis_history(
    instance_root: Path,
    task_id: str,
    date_str: str,
    diagnosis: dict,
):
    """Append a diagnosis record to the persistent history.

    Called by orchestrator._diagnose_and_fix to build the multi-day
    trail that reflection uses for pattern analysis.
    """
    state = load_reflection_state(instance_root)
    history = state.setdefault("diagnosis_history", {})
    task_history = history.setdefault(task_id, [])

    # Don't duplicate same date + same diagnosis text
    for entry in task_history:
        if entry.get("date") == date_str and entry.get("diagnosis") == diagnosis.get("diagnosis", ""):
            return

    task_history.append({
        "date": date_str,
        "diagnosis": diagnosis.get("diagnosis", ""),
        "fix_applied": diagnosis.get("fix_applied", False),
        "fix_desc": diagnosis.get("fix_description", ""),
        "outcome": "pending",
    })

    # Keep only last 30 entries per task
    if len(task_history) > 30:
        history[task_id] = task_history[-30:]

    save_reflection_state(instance_root, state)


def record_fix_outcome(
    instance_root: Path,
    fix_id: str,
    outcome: str,
    days_until_recurrence: Optional[int] = None,
):
    """Update the outcome of a previously applied fix."""
    state = load_reflection_state(instance_root)
    for fix in state.get("applied_fixes", []):
        if fix.get("id") == fix_id:
            fix["outcome"] = outcome
            fix["days_until_recurrence"] = days_until_recurrence
            break
    save_reflection_state(instance_root, state)


def update_failure_streaks(
    instance_root: Path,
    task_health: dict,
):
    """Update failure streak records from current task health data.

    task_health: {task_id: {"days_failing": int, "last_success_date": str, ...}}
    """
    state = load_reflection_state(instance_root)
    streaks = state.setdefault("failure_streaks", {})
    today = datetime.now().strftime("%Y-%m-%d")

    for task_id, health in task_health.items():
        days = health.get("days_failing", 0)
        if days > 0:
            existing = streaks.get(task_id, {})
            if not existing or existing.get("days", 0) == 0:
                # New streak
                streaks[task_id] = {
                    "start": health.get("first_failure_date", today),
                    "days": days,
                    "fixes_tried": 0,
                }
            else:
                # Update existing streak
                existing["days"] = days
        else:
            # Task is healthy — clear streak
            if task_id in streaks:
                del streaks[task_id]

    save_reflection_state(instance_root, state)


def update_engagement_history(
    instance_root: Path,
    engagement: dict,
    date_str: str,
):
    """Append today's engagement snapshot for each task.

    engagement: {task_id: {"days_since_reply": int, "report_quality": int, ...}}
    """
    state = load_reflection_state(instance_root)
    history = state.setdefault("engagement_history", {})

    for task_id, eng in engagement.items():
        task_history = history.setdefault(task_id, [])
        task_history.append({
            "date": date_str,
            "days_since_reply": eng.get("days_since_reply"),
            "report_quality": eng.get("report_quality"),
        })
        # Keep last 30 entries
        if len(task_history) > 30:
            history[task_id] = task_history[-30:]

    save_reflection_state(instance_root, state)
