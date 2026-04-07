"""Programmatic Task Diagnoser.

Bridges Detect (output_assessor) and Fix (actor). When a task is flagged
as having no meaningful output, this module gathers task-specific symptoms
WITHOUT calling the LLM — just file reads and pattern matching.

The resulting diagnosis is passed to the fix agent so it doesn't have to
re-explore the codebase from scratch. This cuts fix agent token usage
from ~1.6M per attempt down to ~400K, and avoids the cold-start problem
where each fix agent tries the same exploration as the previous one.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


def _read_text(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(errors="replace")
        return text[-max_chars:] if len(text) > max_chars else text
    except OSError:
        return ""


def _read_jsonl_tail(path: Path, n: int = 10) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(errors="replace").strip().split("\n")
        entries = []
        for line in lines[-n:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return entries
    except OSError:
        return []


def _detect_timeout_pattern(log_text: str) -> Optional[str]:
    """Look for repeated timeout signatures in log text."""
    patterns = [
        (r"timed out after (\d+) seconds", "subprocess timeout"),
        (r"Phase 2 hit wall-clock timeout", "Phase 2 SIGALRM timeout"),
        (r"TimeoutError", "Python TimeoutError"),
        (r"max retries exceeded", "retry exhaustion"),
        (r"rate limit", "API rate limit"),
    ]
    found = []
    for pattern, label in patterns:
        matches = re.findall(pattern, log_text, re.IGNORECASE)
        if matches:
            found.append(f"{label} ({len(matches)}x)")
    return ", ".join(found) if found else None


def _detect_swallowed_exception(log_text: str) -> Optional[str]:
    """Look for exceptions caught and printed but not surfaced."""
    patterns = [
        r"\[\w+\] (?:Email |Phase \d+ )?(?:FAILED|failed): (.+?)$",
        r"Warning: (.+?)$",
        r"Skipped \((.+?)\)",
    ]
    findings = []
    for p in patterns:
        for m in re.finditer(p, log_text, re.MULTILINE):
            findings.append(m.group(1).strip()[:120])
    if findings:
        return "; ".join(findings[-3:])  # most recent 3
    return None


def diagnose_task(
    instance_root: Path,
    task_id: str,
    task_path: str,
    apparent_issue: str,
    is_stale: bool,
    date_str: str,
) -> dict:
    """Build a structured diagnosis for a broken task.

    Returns dict with:
        symptoms: list[str] — concrete observations
        recent_errors: list[str] — last few error messages from summary.json
        log_signals: list[str] — patterns detected in run.log
        state_anomalies: list[str] — issues found in state files
        relevant_files: list[str] — files the fix agent should focus on
        prior_diagnoses: list[str] — what previous fix attempts found (if any)
    """
    task_dir = instance_root / task_path
    summary_dir = instance_root / "daily_summaries" / date_str / "tasks" / task_id

    diagnosis = {
        "task_id": task_id,
        "apparent_issue": apparent_issue,
        "is_stale": is_stale,
        "symptoms": [],
        "recent_errors": [],
        "log_signals": [],
        "state_anomalies": [],
        "relevant_files": [],
        "prior_diagnoses": [],
    }

    # 1. Read today's summary.json for errors and action_results
    summary_json_path = summary_dir / "summary.json"
    if summary_json_path.exists():
        try:
            with open(summary_json_path) as f:
                summary = json.load(f)
            # Errors
            for err in summary.get("errors", [])[:5]:
                if isinstance(err, dict):
                    msg = err.get("message", err.get("error", str(err)))
                else:
                    msg = str(err)
                diagnosis["recent_errors"].append(str(msg)[:200])
            # Action results — surface skipped/failed actions
            for action in summary.get("action_results", []):
                if action.get("status") in ("skipped", "failed"):
                    blocking = action.get("blocking", True)
                    tag = "blocking" if blocking else "non-blocking"
                    diagnosis["symptoms"].append(
                        f"action {action['action_type']}={action['status']} ({tag})"
                    )
            # Status itself
            status = summary.get("status", "unknown")
            if status != "success":
                diagnosis["symptoms"].append(f"summary status={status}")
        except (json.JSONDecodeError, OSError) as e:
            diagnosis["symptoms"].append(f"summary.json unreadable: {e}")
    else:
        diagnosis["symptoms"].append("No summary.json produced today")

    # 2. Recent run log (cycle log)
    log_path = task_dir / "logs" / f"cycle_{date_str}.log"
    log_text = _read_text(log_path, max_chars=8000)
    if log_text:
        timeout_sig = _detect_timeout_pattern(log_text)
        if timeout_sig:
            diagnosis["log_signals"].append(f"Timeouts detected: {timeout_sig}")
        swallowed = _detect_swallowed_exception(log_text)
        if swallowed:
            diagnosis["log_signals"].append(f"Swallowed errors: {swallowed}")
        # Last 3 lines for context
        last_lines = [l for l in log_text.strip().split("\n")[-5:] if l.strip()]
        if last_lines:
            diagnosis["log_signals"].append(f"Last log lines: {' | '.join(last_lines)[:300]}")

    # 3. Exploration log (LTT-style tasks)
    exp_log = task_dir / "state" / "exploration_log.jsonl"
    recent_exp = _read_jsonl_tail(exp_log, n=8)
    if recent_exp:
        timeout_count = sum(
            1 for e in recent_exp
            if "timeout" in str(e.get("error", "") + e.get("conclusion", "")).lower()
        )
        if timeout_count >= 3:
            diagnosis["log_signals"].append(
                f"exploration_log: {timeout_count}/{len(recent_exp)} recent entries are timeouts"
            )
        # Show last entry
        if recent_exp:
            last = recent_exp[-1]
            diagnosis["log_signals"].append(
                f"Last exploration: {(last.get('conclusion') or last.get('error') or '')[:200]}"
            )

    # 4. State file inspection
    state_dir = task_dir / "state"
    if state_dir.exists():
        for state_file in state_dir.glob("*.json"):
            try:
                with open(state_file) as f:
                    state = json.load(f)
                # Look for stale or missing fields
                if isinstance(state, dict):
                    last_cycle = state.get("last_cycle_time", "")
                    if last_cycle:
                        try:
                            dt = datetime.fromisoformat(last_cycle.replace("Z", "+00:00"))
                            now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
                            age = (now - dt).total_seconds() / 3600
                            if age > 24:
                                diagnosis["state_anomalies"].append(
                                    f"{state_file.name}: last_cycle_time is {age:.0f}h old"
                                )
                        except (ValueError, TypeError):
                            pass
                    # Check for failed_steps lists
                    if "failed_steps" in state and state["failed_steps"]:
                        diagnosis["state_anomalies"].append(
                            f"{state_file.name}: {len(state['failed_steps'])} failed steps recorded"
                        )
                    # Check for retry counts at max
                    if "step_attempts" in state:
                        maxed = [k for k, v in state["step_attempts"].items() if v >= 2]
                        if maxed:
                            diagnosis["state_anomalies"].append(
                                f"{state_file.name}: {len(maxed)} steps at max retries"
                            )
            except (json.JSONDecodeError, OSError):
                pass

    # 5. Identify relevant files (where the fix should focus)
    # Always include these if they exist
    candidate_files = [
        "run.py",
        "task.md",
        "definition.yaml",
        "charter.yaml",
        "config.yaml",
    ]
    for f in candidate_files:
        if (task_dir / f).exists():
            diagnosis["relevant_files"].append(f)

    # 6. Prior diagnoses from reflector state
    reflection_state_path = instance_root / "_shared" / "state" / "reflection_state.json"
    if not reflection_state_path.exists():
        reflection_state_path = instance_root / "tasks" / "_shared" / "state" / "reflection_state.json"
    if reflection_state_path.exists():
        try:
            with open(reflection_state_path) as f:
                rstate = json.load(f)
            for fix in rstate.get("applied_fixes", [])[-5:]:
                if fix.get("task") == task_id:
                    diagnosis["prior_diagnoses"].append({
                        "date": fix.get("date", ""),
                        "diagnosis": fix.get("diagnosis", "")[:200],
                        "fix": fix.get("description", "")[:200],
                        "verified": fix.get("verified", None),
                    })
        except (json.JSONDecodeError, OSError):
            pass

    return diagnosis


def format_diagnosis_for_prompt(diagnosis: dict) -> str:
    """Render the diagnosis as a focused prompt section for the fix agent."""
    lines = []
    lines.append(f"DIAGNOSIS (programmatic, no LLM exploration needed):")
    lines.append(f"  Apparent issue: {diagnosis['apparent_issue']}")
    lines.append(f"  Stale output: {diagnosis['is_stale']}")
    lines.append("")

    if diagnosis["symptoms"]:
        lines.append("SYMPTOMS (from today's summary):")
        for s in diagnosis["symptoms"]:
            lines.append(f"  - {s}")
        lines.append("")

    if diagnosis["recent_errors"]:
        lines.append("RECENT ERRORS:")
        for e in diagnosis["recent_errors"]:
            lines.append(f"  - {e}")
        lines.append("")

    if diagnosis["log_signals"]:
        lines.append("LOG SIGNALS (patterns detected in run.log):")
        for s in diagnosis["log_signals"]:
            lines.append(f"  - {s}")
        lines.append("")

    if diagnosis["state_anomalies"]:
        lines.append("STATE ANOMALIES (unusual values in state files):")
        for a in diagnosis["state_anomalies"]:
            lines.append(f"  - {a}")
        lines.append("")

    if diagnosis["prior_diagnoses"]:
        lines.append("PRIOR FIX ATTEMPTS (do NOT repeat these):")
        for p in diagnosis["prior_diagnoses"]:
            verified = p.get("verified")
            tag = "verified ✓" if verified else "unverified" if verified is False else "?"
            lines.append(f"  - [{p['date']}] [{tag}] {p['diagnosis'][:100]}")
            lines.append(f"      Fix tried: {p['fix'][:100]}")
        lines.append("")

    if diagnosis["relevant_files"]:
        lines.append(f"START WITH THESE FILES (don't explore the whole repo):")
        for f in diagnosis["relevant_files"]:
            lines.append(f"  - {f}")
        lines.append("")

    return "\n".join(lines)
