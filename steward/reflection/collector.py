"""Phase 1 — Data Collection.

Gathers multi-day data from orchestrator state, task summaries,
email logs, and task state files into a unified ReflectionContext.
No LLM calls — pure data loading.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _load_jsonl(path: Path, max_lines: int = 500) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return entries[-max_lines:] if len(entries) > max_lines else entries


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return {}


def _date_range(end_date: str, days: int) -> list[str]:
    """Generate list of date strings from end_date going back N days."""
    end = datetime.strptime(end_date, "%Y-%m-%d")
    return [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def _days_between(date1: str, date2: str) -> int:
    """Days between two YYYY-MM-DD strings. Returns 0 if unparseable."""
    try:
        d1 = datetime.strptime(date1, "%Y-%m-%d")
        d2 = datetime.strptime(date2, "%Y-%m-%d")
        return abs((d2 - d1).days)
    except (ValueError, TypeError):
        return 0


def _collect_task_health(
    task_id: str,
    task_path: str,
    orch_state: dict,
    summaries_root: Path,
    instance_root: Path,
    dates: list[str],
    reflection_state: dict,
    charter: dict = None,
) -> dict:
    """Build health record for one task over the lookback window."""
    task_runs = orch_state.get("task_runs", {}).get(task_id, {})
    last_success = task_runs.get("last_success_date", "")
    today = dates[0]

    # For weekly tasks, days_failing is only meaningful relative to their schedule
    schedule = (charter or {}).get("schedule", {})
    is_weekly = schedule.get("frequency") == "weekly"

    # Count successes/failures over the window
    statuses = []
    errors_collected = []
    durations = []
    for date in dates:
        summary_file = summaries_root / date / "tasks" / task_id / "summary.json"
        summary = _load_json(summary_file)
        if summary:
            status = summary.get("status", "unknown")
            statuses.append({"date": date, "status": status})
            if status in ("failed", "partial"):
                for err in summary.get("errors", []):
                    errors_collected.append({
                        "date": date,
                        "message": err.get("message", str(err))[:200],
                    })
                # Flag "partial with no errors" as silent degradation
                if status == "partial" and not summary.get("errors"):
                    skipped_actions = [
                        a["action_type"] for a in summary.get("action_results", [])
                        if a.get("status") == "skipped"
                    ]
                    detail = f"skipped: {', '.join(skipped_actions)}" if skipped_actions else "no errors reported"
                    errors_collected.append({
                        "date": date,
                        "message": f"Silent degradation: status=partial but {detail}",
                    })
            dur = summary.get("metadata", {}).get("duration_s")
            if dur is not None:
                durations.append(dur)
        else:
            statuses.append({"date": date, "status": "missing"})

    successes = sum(1 for s in statuses if s["status"] == "success")
    attempts = sum(1 for s in statuses if s["status"] != "missing")
    success_rate = successes / attempts if attempts > 0 else 0.0

    # Days failing: consecutive days from today with no success
    days_failing = 0
    if is_weekly:
        # Weekly tasks: only count as "failing" if they failed on their scheduled day
        # Don't count days between scheduled runs
        last_date = task_runs.get("last_date", "")
        if last_success and last_success == last_date:
            days_failing = 0  # Last run was successful
        elif last_date and last_success:
            # Count failed run days, not calendar days
            failed_runs = sum(
                1 for s in statuses
                if s["status"] == "failed"
            )
            days_failing = failed_runs
        elif last_date:
            days_failing = 1  # Ran but never succeeded
    elif last_success:
        days_failing = _days_between(last_success, today)
    elif task_runs.get("last_date"):
        # Never succeeded — count from first run
        days_failing = _days_between(task_runs["last_date"], today) + 1

    # Find first failure date in current streak
    first_failure_date = today
    for s in statuses:
        if s["status"] in ("success",):
            break
        if s["status"] in ("failed", "missing"):
            first_failure_date = s["date"]

    # Diagnosis history from reflection state (persistent multi-day)
    diag_history = reflection_state.get("diagnosis_history", {}).get(task_id, [])

    # Current diagnosis from orchestrator state (today's reactive diagnosis)
    current_diag = orch_state.get("diagnoses", {}).get(task_id, {})

    # If reflection state has no history, seed from orchestrator's current diagnosis
    if not diag_history and current_diag.get("result"):
        r = current_diag["result"]
        diag_history = [{
            "date": current_diag.get("date", today),
            "diagnosis": r.get("diagnosis", ""),
            "fix_applied": r.get("fix_applied", False),
            "fix_desc": r.get("fix_description", ""),
            "outcome": "pending",
        }]

    # Fix history from reflection state
    fix_history = [
        f for f in reflection_state.get("applied_fixes", [])
        if f.get("task") == task_id
    ]

    # Retry exhaustion: days where max retries were hit
    retry_count = task_runs.get("retry_count", 0)

    # Read most recent log tail for persistent failures
    log_tail = ""
    if days_failing >= 3:
        log_file = instance_root / task_path / "logs" / f"cycle_{today}.log"
        if log_file.exists():
            try:
                raw = log_file.read_text()
                log_tail = raw[-3000:] if len(raw) > 3000 else raw
            except OSError:
                pass

    # Detect tasks that have never succeeded (0% success with 2+ attempts)
    never_succeeded = successes == 0 and attempts >= 2

    return {
        "task_id": task_id,
        "last_success_date": last_success,
        "days_failing": days_failing,
        "first_failure_date": first_failure_date,
        "success_rate_7d": round(success_rate, 2),
        "successes_7d": successes,
        "attempts_7d": attempts,
        "never_succeeded": never_succeeded,
        "statuses": statuses,
        "errors": errors_collected,
        "diagnosis_history": diag_history,
        "current_diagnosis": current_diag,
        "fix_history": fix_history,
        "retry_count_today": retry_count,
        "avg_duration_s": round(sum(durations) / len(durations), 1) if durations else None,
        "log_tail": log_tail,
    }


def _collect_engagement(
    task_id: str,
    task_path: str,
    instance_root: Path,
    email_log: list[dict],
    dates: list[str],
) -> dict:
    """Build engagement record for one task."""
    # Check for proactive task state (days_since_reply)
    task_state_file = instance_root / task_path / "state" / "task_state.json"
    task_state = _load_json(task_state_file)
    days_since_reply = task_state.get("days_since_reply")

    # Count emails in lookback window
    date_set = set(dates)
    task_prefix = f"[{task_id.upper().replace('_', '-')}]"
    # Also try common prefixes
    alt_prefixes = [
        f"[{task_id.upper()}]",
        task_prefix,
    ]

    task_emails = [
        e for e in email_log
        if e.get("date") in date_set
        and any(e.get("subject", "").startswith(p) for p in alt_prefixes)
    ]

    emails_sent = sum(1 for e in task_emails if e.get("status") == "sent")
    crash_emails = sum(
        1 for e in task_emails
        if "CRASH" in e.get("subject", "") or "TIMEOUT" in e.get("subject", "")
    )

    return {
        "task_id": task_id,
        "days_since_reply": days_since_reply,
        "has_email_loop": days_since_reply is not None,
        "emails_sent_7d": emails_sent,
        "crash_emails_7d": crash_emails,
    }


def _detect_system_patterns(task_health: dict) -> list[dict]:
    """Detect cross-task failure patterns."""
    patterns = []

    # Pattern: diagnostic agent timeout affecting multiple tasks
    diag_timeout_tasks = []
    for task_id, health in task_health.items():
        current_diag = health.get("current_diagnosis", {}).get("result", {})
        if "timed out" in current_diag.get("diagnosis", "").lower():
            diag_timeout_tasks.append(task_id)

    if len(diag_timeout_tasks) >= 2:
        patterns.append({
            "id": "diagnostic_timeout",
            "description": "Diagnostic agent itself times out, preventing self-healing",
            "affected_tasks": diag_timeout_tasks,
            "occurrences": len(diag_timeout_tasks),
            "type": "systemic",
        })

    # Pattern: multiple tasks failing with similar error messages
    error_clusters = {}
    for task_id, health in task_health.items():
        for err in health.get("errors", []):
            msg = err.get("message", "")
            # Extract key error phrases
            for phrase in ["timeout", "ssl", "oauth", "rate limit", "429",
                           "import error", "module not found", "permission"]:
                if phrase.lower() in msg.lower():
                    cluster = error_clusters.setdefault(phrase, [])
                    if task_id not in cluster:
                        cluster.append(task_id)

    for phrase, tasks in error_clusters.items():
        if len(tasks) >= 2:
            patterns.append({
                "id": f"error_cluster_{phrase.replace(' ', '_')}",
                "description": f"'{phrase}' errors across multiple tasks",
                "affected_tasks": tasks,
                "occurrences": len(tasks),
                "type": "error_cluster",
            })

    return patterns


def _collect_email_health(email_log: list[dict], dates: list[str]) -> dict:
    """Assess email infrastructure health over the lookback window."""
    date_set = set(dates)
    recent = [e for e in email_log if e.get("date") in date_set]

    sent = sum(1 for e in recent if e.get("status") == "sent")
    errors = sum(1 for e in recent if e.get("status") == "error")
    rate_limited = sum(1 for e in recent if e.get("status") == "rate_limited")

    return {
        "total_7d": len(recent),
        "sent_7d": sent,
        "errors_7d": errors,
        "rate_limited_7d": rate_limited,
        "error_rate": round(errors / len(recent), 2) if recent else 0.0,
    }


def collect_reflection_data(
    instance_root: Path,
    date_str: Optional[str] = None,
    lookback_days: int = 7,
) -> dict:
    """Gather all data sources into a unified ReflectionContext.

    Returns dict with:
        task_health: {task_id: {...}}
        engagement: {task_id: {...}}
        system_patterns: [{...}]
        email_health: {...}
        prior_reflection: {...}
        date: str
        lookback_days: int
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    dates = _date_range(date_str, lookback_days)
    summaries_root = instance_root / "daily_summaries"

    # Load orchestrator state
    orch_state = _load_json(instance_root / "orchestrator_state.json")

    # Load reflection state
    from .state import load_reflection_state
    reflection_state = load_reflection_state(instance_root)

    # Load task registry
    registry_file = instance_root / "tasks" / "registry.yaml"
    registry = _load_yaml(registry_file)
    tasks = [t for t in registry.get("tasks", []) if t.get("enabled", True)]

    # Load email log
    email_log = _load_jsonl(
        instance_root / "tasks" / "_shared" / "state" / "email_send_log.jsonl"
    )

    # Collect per-task data
    task_health = {}
    engagement = {}
    for task in tasks:
        task_id = task["id"]
        task_path = task["path"]

        # Load charter for schedule info
        charter_path = instance_root / task_path / "charter.yaml"
        charter = _load_yaml(charter_path)

        task_health[task_id] = _collect_task_health(
            task_id, task_path, orch_state, summaries_root,
            instance_root, dates, reflection_state, charter=charter,
        )
        engagement[task_id] = _collect_engagement(
            task_id, task_path, instance_root, email_log, dates,
        )

    # Detect cross-task patterns
    system_patterns = _detect_system_patterns(task_health)

    # Email infrastructure health
    email_health = _collect_email_health(email_log, dates)

    print(f"  [reflect] Loaded {lookback_days} days of data for {len(tasks)} tasks",
          file=sys.stderr)

    return {
        "date": date_str,
        "lookback_days": lookback_days,
        "task_health": task_health,
        "engagement": engagement,
        "system_patterns": system_patterns,
        "email_health": email_health,
        "prior_reflection": reflection_state,
    }
