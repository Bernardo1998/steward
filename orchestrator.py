#!/usr/bin/env python3
"""Task Orchestrator — the "for" loop.

Reads tasks/registry.yaml, checks schedules, manages locks,
spawns CLI agent sub-sessions for due tasks, collects summaries,
sends daily digest.

Does NOT import charter_worker. Does NOT read task internals.

Usage:
    charter-orchestrator                         # one cycle (for cron)
    charter-orchestrator --dry-run               # show what would run
    charter-orchestrator --instance-dir /path    # explicit instance root
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml


def _resolve_instance_root(cli_arg: str | None = None) -> Path:
    """Resolve instance root: CLI arg > env var > cwd."""
    if cli_arg:
        return Path(cli_arg).resolve()
    env = os.environ.get("CHARTER_INSTANCE_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd()


# These are set in main() after parsing args
REPO_ROOT: Path = Path.cwd()
STATE_FILE: Path = REPO_ROOT / "orchestrator_state.json"
REGISTRY_FILE: Path = REPO_ROOT / "tasks" / "registry.yaml"
SUMMARIES_ROOT: Path = REPO_ROOT / "daily_summaries"


def _init_paths(instance_root: Path):
    """Initialize module-level path variables from instance root."""
    global REPO_ROOT, STATE_FILE, REGISTRY_FILE, SUMMARIES_ROOT
    REPO_ROOT = instance_root
    STATE_FILE = REPO_ROOT / "orchestrator_state.json"
    REGISTRY_FILE = REPO_ROOT / "tasks" / "registry.yaml"
    SUMMARIES_ROOT = REPO_ROOT / "daily_summaries"
    # Also set env var so charter_worker modules can find the instance root
    os.environ["CHARTER_INSTANCE_ROOT"] = str(REPO_ROOT)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"task_runs": {}, "last_digest_date": "", "last_digest_time": ""}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def load_registry() -> list[dict]:
    if not REGISTRY_FILE.exists():
        print(f"[orch] No registry at {REGISTRY_FILE}", file=sys.stderr)
        return []
    with open(REGISTRY_FILE) as f:
        data = yaml.safe_load(f) or {}
    return [t for t in data.get("tasks", []) if t.get("enabled", True)]


def load_charter(task_path: str) -> dict:
    charter_path = REPO_ROOT / task_path / "charter.yaml"
    if not charter_path.exists():
        return {}
    with open(charter_path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def is_due(schedule: dict, now: datetime, last_run_str: str) -> bool:
    freq = schedule.get("frequency", "daily")
    last_run = None
    if last_run_str:
        try:
            last_run = datetime.fromisoformat(last_run_str)
        except ValueError:
            pass

    if freq == "hourly":
        # Due every cycle. Lock check prevents double-spawn.
        return True

    if freq == "daily":
        if last_run and last_run.date() == now.date():
            return False
        run_hour = schedule.get("run_hour")
        if run_hour is not None and now.hour < run_hour:
            return False
        return True

    if freq == "weekly":
        run_day = schedule.get("run_day")
        if run_day and now.strftime("%A") != run_day:
            return False
        if last_run and (now - last_run).days < 6:
            return False
        return True

    return False


def _needs_retry(task_id: str, task_path: str, schedule: dict, state: dict,
                 date_str: str) -> bool:
    """Check if a daily task needs retry (attempted today, no summary, retries left)."""
    if schedule.get("frequency", "daily") != "daily":
        return False

    task_state = state.get("task_runs", {}).get(task_id, {})

    # Only retry if attempted today
    if task_state.get("last_date") != date_str:
        return False

    # Already succeeded today?
    if task_state.get("last_success_date") == date_str:
        return False

    # Check if summary.json exists with non-failed status
    summary_json = SUMMARIES_ROOT / date_str / "tasks" / task_id / "summary.json"
    if summary_json.exists():
        try:
            with open(summary_json) as f:
                data = json.load(f)
            if data.get("status") != "failed":
                return False
        except (json.JSONDecodeError, OSError):
            pass

    # Check retry count (reset on day change)
    max_retries = schedule.get("max_retries", 3)
    retry_count = task_state.get("retry_count", 0)
    if task_state.get("last_retry_date") != date_str:
        retry_count = 0
    if retry_count >= max_retries:
        return False

    # Don't retry if locked (still running)
    if is_locked(task_path):
        return False

    return True


# ---------------------------------------------------------------------------
# Lock files
# ---------------------------------------------------------------------------

def lock_path(task_path: str) -> Path:
    return REPO_ROOT / task_path / ".lock"


def is_locked(task_path: str) -> bool:
    lp = lock_path(task_path)
    if not lp.exists():
        return False
    try:
        lock = json.loads(lp.read_text())
        pid = lock.get("pid")
        if pid:
            os.kill(pid, 0)  # check if alive
            return True       # still running
    except (ProcessLookupError, json.JSONDecodeError, OSError):
        pass

    # Stale lock — check timeout
    try:
        lock = json.loads(lp.read_text())
        started = datetime.fromisoformat(lock.get("started_at", ""))
        max_minutes = lock.get("max_runtime_minutes", 120)
        if datetime.now() - started > timedelta(minutes=max_minutes):
            print(f"  [orch] Stale lock (timeout): {task_path}", file=sys.stderr)
    except (ValueError, json.JSONDecodeError):
        pass

    print(f"  [orch] Removing stale lock: {task_path}", file=sys.stderr)
    lp.unlink(missing_ok=True)
    return False


def create_lock(task_path: str, pid: int, max_runtime: int):
    lp = lock_path(task_path)
    lp.write_text(json.dumps({
        "pid": pid,
        "started_at": datetime.now().isoformat(),
        "max_runtime_minutes": max_runtime,
    }))


def remove_lock(task_path: str):
    lock_path(task_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Preflight constraints
# ---------------------------------------------------------------------------

def check_preflight(task_id: str, charter: dict) -> list[str]:
    """Check preflight constraints from charter.yaml. Returns list of failures."""
    try:
        from preflight import check_constraints
        return check_constraints(charter, REPO_ROOT)
    except ImportError:
        return []


# ---------------------------------------------------------------------------
# Self-healing: diagnose & fix crashed tasks
# ---------------------------------------------------------------------------

_DIAGNOSE_TIMEOUT = 600  # 10 minutes max for diagnosis agent

def _diagnose_and_fix(task_id: str, task_path: str, reason: str,
                      date_str: str, state: dict) -> dict:
    """Spawn a Claude Code agent to diagnose and fix a crashed task.

    Returns dict with:
        diagnosed: bool — whether diagnosis ran
        diagnosis: str — root cause explanation
        fix_applied: bool — whether a code/config fix was made
        fix_description: str — what was changed
        should_retry: bool — whether retry is likely to succeed
    """
    result = {
        "diagnosed": False, "diagnosis": "", "fix_applied": False,
        "fix_description": "", "should_retry": False,
    }

    # Don't diagnose the same task twice in one day
    diag_state = state.setdefault("diagnoses", {})
    if diag_state.get(task_id, {}).get("date") == date_str:
        prev = diag_state[task_id]
        print(f"  [orch] {task_id}: already diagnosed today, reusing result",
              file=sys.stderr)
        return prev.get("result", result)

    # Gather crash context
    log_file = REPO_ROOT / task_path / "logs" / f"cycle_{date_str}.log"
    log_text = ""
    if log_file.exists():
        raw = log_file.read_text()
        log_text = raw[-4000:] if len(raw) > 4000 else raw

    task_dir = REPO_ROOT / task_path
    charter_file = task_dir / "charter.yaml"
    charter_text = charter_file.read_text() if charter_file.exists() else "(no charter.yaml)"

    # Check for run.py or task.md as the entry point
    run_py = task_dir / "run.py"
    entry_text = ""
    if run_py.exists():
        raw = run_py.read_text()
        entry_text = raw[:6000] if len(raw) > 6000 else raw

    # Build the diagnostic prompt
    prompt = f"""\
You are a self-healing orchestrator agent. A task has crashed and you must diagnose
the root cause and fix it so the next retry succeeds.

TASK: {task_id}
PATH: {task_path}
DATE: {date_str}
FAILURE REASON: {reason}

CHARTER CONFIG:
```yaml
{charter_text[:2000]}
```

CRASH LOG (last 4000 chars):
```
{log_text if log_text else "(empty — process produced no output before crash)"}
```

ENTRY POINT (run.py, first 6000 chars):
```python
{entry_text if entry_text else "(no run.py found)"}
```

YOUR JOB:
1. Read the crash log and task code to understand WHY the task failed.
2. If the fix requires a code or config change, MAKE THE CHANGE directly.
   - You have write access to files under {task_dir}
   - You can also read/edit files in the charter-worker library at /mnt/c/charter-worker/
3. If the failure is environmental (network, disk, etc.), note it but don't retry blindly.
4. If the log is empty, check for common issues: import errors, missing dependencies,
   stale state files, wrong file paths.

After investigating, output EXACTLY one JSON block (fenced with ```json ... ```) with:
```json
{{
  "diagnosis": "One-paragraph root cause explanation",
  "fix_applied": true/false,
  "fix_description": "What you changed (or 'no fix needed' / 'manual intervention required')",
  "should_retry": true/false,
  "confidence": "high/medium/low"
}}
```

Be concise. Focus on fixing, not explaining."""

    print(f"  [orch] {task_id}: spawning diagnostic agent...", file=sys.stderr)
    try:
        proc = subprocess.run(
            ["codex", "exec",
             "--dangerously-bypass-approvals-and-sandbox",
             "-C", str(task_dir),
             "--add-dir", str(REPO_ROOT),
             "-"],
            input=prompt,
            capture_output=True, text=True,
            timeout=_DIAGNOSE_TIMEOUT,
            cwd=str(task_dir),
        )
        output = proc.stdout or ""

        # Extract JSON result from output
        import re
        json_match = re.search(r"```json\s*\n(.*?)\n\s*```", output, re.DOTALL)
        if not json_match:
            # Try without fences
            json_match = re.search(r'\{[^{}]*"diagnosis"[^{}]*\}', output)
        if json_match:
            raw = json_match.group(1) if "```" in json_match.group(0) else json_match.group(0)
            parsed = json.loads(raw)
            result = {
                "diagnosed": True,
                "diagnosis": parsed.get("diagnosis", ""),
                "fix_applied": parsed.get("fix_applied", False),
                "fix_description": parsed.get("fix_description", ""),
                "should_retry": parsed.get("should_retry", True),
            }
        else:
            # Agent didn't produce structured output — use raw text as diagnosis
            result["diagnosed"] = True
            result["diagnosis"] = output[-1500:] if output else "Agent produced no output"
            result["should_retry"] = True  # default to retry

        print(f"  [orch] {task_id}: diagnosis complete — "
              f"fix={'YES' if result['fix_applied'] else 'no'}, "
              f"retry={'YES' if result['should_retry'] else 'no'}",
              file=sys.stderr)
        print(f"  [orch] {task_id}: {result['diagnosis'][:200]}",
              file=sys.stderr)

    except subprocess.TimeoutExpired:
        result["diagnosed"] = True
        result["diagnosis"] = f"Diagnostic agent timed out after {_DIAGNOSE_TIMEOUT}s"
        result["should_retry"] = True  # try anyway
        print(f"  [orch] {task_id}: diagnostic agent timed out", file=sys.stderr)
    except Exception as e:
        result["diagnosis"] = f"Diagnostic agent failed to start: {e}"
        print(f"  [orch] {task_id}: diagnostic agent error: {e}", file=sys.stderr)

    # Cache diagnosis for today (don't diagnose same task twice)
    diag_state[task_id] = {"date": date_str, "result": result}
    return result


# ---------------------------------------------------------------------------
# Failure reporting
# ---------------------------------------------------------------------------

def _send_failure_report(task_id: str, task_path: str, charter: dict,
                         date_str: str, reason: str,
                         diagnosis: dict | None = None):
    """Send an email + write fallback summary when a task fails without self-reporting."""
    report_cfg = charter.get("report", {})
    email_cfg = report_cfg.get("own_email", {})
    prefix = email_cfg.get("prefix", f"[{task_id.upper()}]")

    # Gather available context
    log_file = REPO_ROOT / task_path / "logs" / f"cycle_{date_str}.log"
    summary_file = SUMMARIES_ROOT / date_str / "tasks" / task_id / "summary.md"

    parts = [
        f"# {task_id} — {reason}\n",
        f"**Date:** {date_str}",
        f"**Reason:** {reason}\n",
    ]

    # Include diagnosis if available
    if diagnosis and diagnosis.get("diagnosed"):
        parts.append("## Diagnosis (auto-investigated)\n")
        parts.append(f"**Root cause:** {diagnosis.get('diagnosis', 'unknown')}\n")
        if diagnosis.get("fix_applied"):
            parts.append(f"**Fix applied:** {diagnosis.get('fix_description', '')}\n")
            parts.append("*The orchestrator has automatically applied a fix and will retry.*\n")
        elif diagnosis.get("should_retry"):
            parts.append("*No code fix needed — retrying.*\n")
        else:
            parts.append("*Manual intervention may be required.*\n")

    if summary_file.exists():
        content = summary_file.read_text()
        if content.strip():
            parts.append("## Partial Summary\n")
            parts.append(content[:3000])

    if log_file.exists():
        log_text = log_file.read_text()
        tail = log_text[-2000:] if len(log_text) > 2000 else log_text
        parts.append("\n## Log Tail (last 2000 chars)\n")
        parts.append(f"```\n{tail}\n```")

    body = "\n".join(parts)

    try:
        from charter_worker.comm.email import send_email
        result = send_email(
            subject=f"{prefix} {reason} — {date_str}",
            body_markdown=body,
        )
        print(f"  [orch] Failure report for {task_id}: {result.get('status')}", file=sys.stderr)
    except Exception as e:
        print(f"  [orch] Could not send failure report for {task_id}: {e}", file=sys.stderr)

    # Write fallback summary if none exists
    summary_dir = SUMMARIES_ROOT / date_str / "tasks" / task_id
    summary_md = summary_dir / "summary.md"
    summary_json_file = summary_dir / "summary.json"
    if not summary_md.exists():
        summary_dir.mkdir(parents=True, exist_ok=True)
        summary_md.write_text(
            f"# {task_id} — {date_str}\n\n"
            f"**{reason}**\n\n"
            f"Fallback summary generated by orchestrator.\n"
            f"The task did not complete its reporting phase.\n"
        )
    if not summary_json_file.exists():
        fallback_json = {
            "task_id": task_id,
            "date": date_str,
            "status": "failed",
            "tldr": [reason],
            "action_items": [f"Investigate {task_id} failure"],
            "errors": [{"type": "orchestrator", "message": reason}],
            "metadata": {"budget_hint": "unknown"},
        }
        with open(summary_json_file, "w") as fj:
            json.dump(fallback_json, fj, indent=2)


def _check_unreported_tasks(tasks: list[dict], date_str: str, state: dict):
    """Pre-pass: detect timed-out/crashed tasks and send failure emails.

    Runs at the start of each orchestrator cycle. For each locked task:
    - If process is alive and within timeout: leave it
    - If process is alive but over timeout: SIGTERM, send failure report
    - If process is dead: send failure report if email was expected
    Then clean up the lock.
    """
    for task in tasks:
        task_id = task["id"]
        task_path = task["path"]
        lp = lock_path(task_path)
        if not lp.exists():
            continue

        try:
            lock = json.loads(lp.read_text())
        except (json.JSONDecodeError, OSError):
            lp.unlink(missing_ok=True)
            continue

        pid = lock.get("pid")
        started_at = lock.get("started_at", "")
        max_minutes = lock.get("max_runtime_minutes", 120)

        # Is process still alive?
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except (ProcessLookupError, OSError):
                pass

        if alive:
            try:
                started = datetime.fromisoformat(started_at)
                elapsed_min = (datetime.now() - started).total_seconds() / 60
                if elapsed_min <= max_minutes:
                    continue  # Running within timeout, leave it
                # Over timeout — terminate
                reason = f"TIMEOUT after {int(elapsed_min)}min (limit: {max_minutes}min)"
                print(f"  [orch] {task_id}: {reason}, terminating PID {pid}",
                      file=sys.stderr)
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(5)
                    try:
                        os.kill(pid, 0)
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        pass
                except OSError:
                    pass
            except ValueError:
                reason = "STALE (unparseable lock timestamp)"
        else:
            # Process dead — determine reason
            try:
                started = datetime.fromisoformat(started_at)
                elapsed_min = (datetime.now() - started).total_seconds() / 60
                if elapsed_min > max_minutes:
                    reason = f"TIMEOUT (exited after {max_minutes}min limit)"
                else:
                    reason = f"CRASHED after {int(elapsed_min)}min"
            except ValueError:
                reason = "CRASHED (unknown duration)"

        # Check if task actually completed normally (summary written after lock)
        lock_date = started_at[:10] if len(started_at) >= 10 else date_str
        summary_file = SUMMARIES_ROOT / lock_date / "tasks" / task_id / "summary.md"
        completed_normally = False
        if summary_file.exists():
            try:
                lock_time = datetime.fromisoformat(started_at)
                summary_mtime = datetime.fromtimestamp(summary_file.stat().st_mtime)
                if summary_mtime >= lock_time:
                    completed_normally = True
            except (ValueError, OSError):
                pass

        if completed_normally:
            print(f"  [orch] {task_id}: completed normally (summary exists), cleaning lock",
                  file=sys.stderr)
            # Record success date
            state.setdefault("task_runs", {}).setdefault(task_id, {})["last_success_date"] = lock_date
        else:
            # --- Self-healing: diagnose before reporting ---
            diagnosis = _diagnose_and_fix(task_id, task_path, reason,
                                          lock_date, state)

            # If diagnosis applied a fix, remove stale fallback summary so
            # retry generates a fresh one
            if diagnosis.get("fix_applied"):
                stale_summary = SUMMARIES_ROOT / lock_date / "tasks" / task_id / "summary.json"
                if stale_summary.exists():
                    try:
                        with open(stale_summary) as f:
                            sdata = json.load(f)
                        if sdata.get("status") == "failed":
                            stale_summary.unlink()
                            stale_md = stale_summary.with_suffix(".md")
                            stale_md.unlink(missing_ok=True)
                            print(f"  [orch] {task_id}: cleared stale fallback summary for retry",
                                  file=sys.stderr)
                    except (json.JSONDecodeError, OSError):
                        pass

            # Send failure report (with diagnosis) if this task expects email
            charter = load_charter(task_path)
            email_enabled = (charter.get("report", {})
                                  .get("own_email", {})
                                  .get("enabled", False))
            if email_enabled:
                failure_emails = state.setdefault("failure_emails_sent", {})
                if failure_emails.get(task_id) == lock_date:
                    print(f"  [orch] {task_id}: failure email already sent today, skipping",
                          file=sys.stderr)
                else:
                    _send_failure_report(task_id, task_path, charter, lock_date,
                                         reason, diagnosis=diagnosis)
                    failure_emails[task_id] = lock_date

        # Clean up lock
        print(f"  [orch] Cleaning up lock: {task_path}", file=sys.stderr)
        lp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Sub-agent dispatch
# ---------------------------------------------------------------------------

def spawn_agent(task: dict, charter: dict, date_str: str) -> subprocess.Popen:
    """Spawn a CLI agent session for a task.

    Uses stdin piping for prompts (robust for any length).
    Codex: `codex exec` with appropriate sandbox mode.
    Claude: `claude -p` with tool allowlist.
    """
    task_id = task["id"]
    task_path = task["path"]
    working_dir = REPO_ROOT / task_path
    execution = charter.get("execution", {})
    agent = execution.get("agent", "codex")

    # Ensure logs directory
    log_dir = working_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / f"cycle_{date_str}.log"

    # Build prompt for the sub-agent
    summary_dir = f"{SUMMARIES_ROOT}/{date_str}/tasks/{task_id}"
    prompt = (
        f'You are a charter worker executing task "{task_id}". '
        f'Your working directory is the current directory. '
        f'Read charter.yaml for your role and configuration. '
        f'Read task.md for detailed step-by-step instructions. '
        f'Execute ONE cycle of the workflow, then exit. '
        f'Write your summary to: {summary_dir}/summary.md '
        f'and summary.json in the same directory. '
        f'Today is {date_str}.'
    )

    # Write prompt to file so it can be piped via stdin (avoids shell escaping)
    prompt_file = log_dir / f"prompt_{date_str}.txt"
    prompt_file.write_text(prompt)

    if agent == "codex":
        sandbox_mode = execution.get("sandbox", "full-auto")
        auto_resume = execution.get("auto_resume", False)
        max_resume_iterations = execution.get("max_resume_iterations", 5)

        if auto_resume:
            # Use agent_loop.sh for auto-resume on context exhaustion
            agent_loop = Path(__file__).resolve().parent / "agent_loop.sh"
            state_file = working_dir / "state" / "experiment_state.json"
            cmd = [
                str(agent_loop),
                str(working_dir),
                str(REPO_ROOT),
                str(prompt_file),
                str(log_file),
                str(state_file),
                str(max_resume_iterations),
                sandbox_mode,
            ]
        elif sandbox_mode == "none":
            cmd = [
                "codex", "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C", str(working_dir),
                "--add-dir", str(REPO_ROOT),
                "-",  # read prompt from stdin
            ]
        else:
            cmd = [
                "codex", "exec", "--full-auto",
                "-C", str(working_dir),
                "--add-dir", str(REPO_ROOT),
                "-",  # read prompt from stdin
            ]
    elif agent == "direct":
        # Run entrypoint script directly (no LLM agent wrapper).
        # Use for tasks with self-contained run.py that call codex internally.
        entrypoint = execution.get("entrypoint", "python run.py")
        env = os.environ.copy()
        env["CHARTER_INSTANCE_ROOT"] = str(REPO_ROOT)
        cmd = ["bash", "-lc", entrypoint]
    elif agent == "claude":
        cmd = [
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
        ]
    else:
        print(f"  [orch] Unknown agent '{agent}' for {task_id}, skipping", file=sys.stderr)
        return None

    auto_resume = agent == "codex" and execution.get("auto_resume", False)
    print(f"  [orch] Spawning {agent} for {task_id} in {working_dir}"
          f"{' (auto-resume)' if auto_resume else ''}", file=sys.stderr)

    # Open log file for output
    log_fh = open(log_file, "a")

    if agent == "codex" and not auto_resume:
        # Pipe prompt via stdin from file
        prompt_fh = open(prompt_file, "r")
        proc = subprocess.Popen(
            cmd,
            stdin=prompt_fh,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    elif agent == "direct":
        proc = subprocess.Popen(
            cmd,
            cwd=str(working_dir),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
        )
    else:
        proc = subprocess.Popen(
            cmd,
            cwd=str(working_dir),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )

    max_runtime = charter.get("schedule", {}).get("max_runtime_minutes", 60)
    create_lock(task_path, proc.pid, max_runtime)

    return proc


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------

def _collect_summaries(date_str: str, since_time: str | None = None) -> list[dict]:
    """Collect task summaries from a date's summary directory.

    If since_time is given (ISO timestamp), only include summaries whose
    file mtime is after that time (for catching late arrivals).
    """
    summary_root = SUMMARIES_ROOT / date_str / "tasks"
    if not summary_root.exists():
        return []

    since_ts = None
    if since_time:
        try:
            since_ts = datetime.fromisoformat(since_time).timestamp()
        except ValueError:
            pass

    summaries = []
    for task_dir in sorted(summary_root.iterdir()):
        if not task_dir.is_dir():
            continue
        summary_md = task_dir / "summary.md"
        if summary_md.exists():
            if since_ts and summary_md.stat().st_mtime <= since_ts:
                continue
            content = summary_md.read_text()
            summaries.append({"task": task_dir.name, "content": content})

    return summaries


def _completeness_check(tasks: list[dict], state: dict, date_str: str,
                        check_hour: int = 5) -> list[dict]:
    """At check_hour, find daily tasks missing summary.json that are still retry-eligible."""
    now = datetime.now()
    if now.hour != check_hour:
        return []

    missing = []
    for task in tasks:
        task_id = task["id"]
        task_path = task["path"]
        charter = load_charter(task_path)
        schedule = charter.get("schedule", {})

        if schedule.get("frequency", "daily") != "daily":
            continue

        # Already succeeded today?
        task_state = state.get("task_runs", {}).get(task_id, {})
        if task_state.get("last_success_date") == date_str:
            continue

        # Check if summary.json exists with non-failed status
        summary_json = SUMMARIES_ROOT / date_str / "tasks" / task_id / "summary.json"
        if summary_json.exists():
            try:
                with open(summary_json) as f:
                    data = json.load(f)
                if data.get("status") != "failed":
                    continue
            except (json.JSONDecodeError, OSError):
                pass

        # Check retry eligibility
        max_retries = schedule.get("max_retries", 2)
        retry_count = task_state.get("retry_count", 0)
        if task_state.get("last_retry_date") != date_str:
            retry_count = 0
        if retry_count >= max_retries:
            continue

        if is_locked(task_path):
            continue

        missing.append(task)

    return missing


def collect_and_send_digest(date_str: str, state: dict):
    """Collect task summaries and send daily digest email.

    Scans today's summaries. Also includes yesterday's summaries that
    arrived after the last digest was sent (late experiment results).
    """
    # Today's summaries
    summaries = _collect_summaries(date_str)

    # Yesterday's late arrivals (summaries written after last digest)
    last_digest_time = state.get("last_digest_time", "")
    yesterday = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    late_summaries = _collect_summaries(yesterday, since_time=last_digest_time)
    if late_summaries:
        print(f"  [orch] Including {len(late_summaries)} late summary(ies) from {yesterday}", file=sys.stderr)

    all_summaries = summaries + [
        {"task": f"{s['task']} (late, {yesterday})", "content": s["content"]}
        for s in late_summaries
    ]

    if not all_summaries:
        print(f"  [orch] No summaries to digest for {date_str}", file=sys.stderr)
        return

    # Build digest
    lines = [f"# Daily Digest — {date_str}", ""]
    for s in all_summaries:
        lines.append(f"---\n## {s['task']}\n")
        lines.append(s["content"])
        lines.append("")

    digest_md = "\n".join(lines)

    # Write digest file
    digest_dir = SUMMARIES_ROOT / date_str
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / "daily_digest.md").write_text(digest_md)
    print(f"  [orch] Digest written: {digest_dir / 'daily_digest.md'}", file=sys.stderr)

    # Send email via charter_worker.comm
    try:
        from charter_worker.comm.email import send_email
        result = send_email(
            subject=f"Daily Digest — {date_str}",
            body_markdown=digest_md,
        )
        print(f"  [orch] Digest email: {result.get('status', 'unknown')}", file=sys.stderr)
        return result
    except Exception as e:
        print(f"  [orch] Digest email failed: {e}", file=sys.stderr)
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Task Orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="Show what would run")
    parser.add_argument("--force", nargs="*", metavar="TASK_ID", help="Force-run these tasks regardless of schedule (e.g., --force weekly_planner)")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--instance-dir", default=None, help="Instance root directory (where tasks/ lives)")
    args = parser.parse_args()

    # Resolve and init paths
    instance_root = _resolve_instance_root(args.instance_dir)
    _init_paths(instance_root)

    now = datetime.now()
    date_str = args.date
    print(f"[orch] Orchestrator starting — {now.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
    print(f"[orch] Instance root: {REPO_ROOT}", file=sys.stderr)

    state = load_state()
    tasks = load_registry()

    if not tasks:
        print("[orch] No enabled tasks in registry", file=sys.stderr)
        return

    # Pre-pass: check for unreported failures from previous cycles
    _check_unreported_tasks(tasks, date_str, state)

    # Determine what's due
    spawned = []
    skipped = []
    for task in tasks:
        task_id = task["id"]
        task_path = task["path"]
        charter = load_charter(task_path)

        if not charter:
            print(f"  [orch] {task_id}: no charter.yaml, skipping", file=sys.stderr)
            skipped.append(task_id)
            continue

        schedule = charter.get("schedule", {})
        last_run = state.get("task_runs", {}).get(task_id, {}).get("last_run", "")

        # Check schedule (--force overrides)
        forced = args.force is not None and (len(args.force) == 0 or task_id in args.force)
        if not forced and not is_due(schedule, now, last_run):
            skipped.append(task_id)
            continue
        if forced:
            print(f"  [orch] {task_id}: FORCED (schedule override)", file=sys.stderr)

        # Preflight constraints
        failures = check_preflight(task_id, charter)
        if failures:
            print(f"  [orch] {task_id}: preflight FAILED:", file=sys.stderr)
            for f in failures:
                print(f"    - {f}", file=sys.stderr)
            skipped.append(task_id)
            continue

        # Check lock
        if is_locked(task_path):
            print(f"  [orch] {task_id}: locked (still running), skip", file=sys.stderr)
            skipped.append(task_id)
            continue

        # Due and unlocked — spawn
        if args.dry_run:
            agent = charter.get("execution", {}).get("agent", "codex")
            print(f"  [DRY RUN] Would spawn {agent} for {task_id}", file=sys.stderr)
            continue

        proc = spawn_agent(task, charter, date_str)
        if proc:
            spawned.append({"id": task_id, "path": task_path, "proc": proc, "charter": charter})

    if args.dry_run:
        print(f"\n[orch] Dry run complete. Would spawn {len(tasks) - len(skipped)} tasks.", file=sys.stderr)
        return

    # Wait for short tasks (max_runtime <= 15 min), let long ones run
    for t in spawned:
        max_rt = t["charter"].get("schedule", {}).get("max_runtime_minutes", 60)
        task_id = t["id"]

        if max_rt <= 15:
            print(f"  [orch] Waiting for {task_id} (max {max_rt} min)...", file=sys.stderr)
            try:
                t["proc"].wait(timeout=max_rt * 60)
                exit_code = t["proc"].returncode
                print(f"  [orch] {task_id} finished (exit {exit_code})", file=sys.stderr)
                if exit_code != 0:
                    email_cfg = t["charter"].get("report", {}).get("own_email", {})
                    if email_cfg.get("enabled"):
                        failure_emails = state.setdefault("failure_emails_sent", {})
                        if failure_emails.get(task_id) == date_str:
                            print(f"  [orch] {task_id}: failure email already sent today, skipping",
                                  file=sys.stderr)
                        else:
                            _send_failure_report(task_id, t["path"], t["charter"],
                                                 date_str, f"FAILED (exit code {exit_code})")
                            failure_emails[task_id] = date_str
            except subprocess.TimeoutExpired:
                print(f"  [orch] {task_id} still running after {max_rt} min, leaving async", file=sys.stderr)
        else:
            print(f"  [orch] {task_id} is long-running ({max_rt} min), leaving async", file=sys.stderr)

        # Record run time
        if task_id not in state.get("task_runs", {}):
            state.setdefault("task_runs", {})[task_id] = {}
        state["task_runs"][task_id]["last_run"] = now.isoformat()
        state["task_runs"][task_id]["last_date"] = date_str

        # Remove lock if process finished
        if t["proc"].poll() is not None:
            remove_lock(t["path"])
            # Check for successful completion (summary.json with non-failed status)
            summary_json = SUMMARIES_ROOT / date_str / "tasks" / task_id / "summary.json"
            if summary_json.exists():
                try:
                    with open(summary_json) as f:
                        data = json.load(f)
                    if data.get("status") != "failed":
                        state["task_runs"][task_id]["last_success_date"] = date_str
                except (json.JSONDecodeError, OSError):
                    pass

    # -----------------------------------------------------------------------
    # Retry pass: re-spawn crashed daily tasks (max 3 retries per day)
    # After self-healing diagnosis, only retry if diagnosis says should_retry.
    # -----------------------------------------------------------------------
    retry_spawned = []
    spawned_ids = {t["id"] for t in spawned}
    for task in tasks:
        task_id = task["id"]
        task_path = task["path"]
        if task_id in spawned_ids:
            continue  # Already spawned this cycle

        charter = load_charter(task_path)
        schedule = charter.get("schedule", {})

        if _needs_retry(task_id, task_path, schedule, state, date_str):
            # Check diagnosis: if we diagnosed and it says don't retry, skip
            diag = state.get("diagnoses", {}).get(task_id, {}).get("result", {})
            if diag.get("diagnosed") and not diag.get("should_retry"):
                print(f"  [orch] {task_id}: diagnosis says no retry — manual fix needed",
                      file=sys.stderr)
                continue

            # Preflight check before retry
            failures = check_preflight(task_id, charter)
            if failures:
                print(f"  [orch] {task_id}: retry preflight FAILED, skipping", file=sys.stderr)
                continue

            task_state = state.setdefault("task_runs", {}).setdefault(task_id, {})
            retry_count = task_state.get("retry_count", 0)
            if task_state.get("last_retry_date") != date_str:
                retry_count = 0
            retry_count += 1
            task_state["retry_count"] = retry_count
            task_state["last_retry_date"] = date_str

            diag_note = ""
            if diag.get("fix_applied"):
                diag_note = f" (fix applied: {diag.get('fix_description', '')[:60]})"
            print(f"  [orch] {task_id}: RETRY #{retry_count}{diag_note} (no summary for {date_str})",
                  file=sys.stderr)
            proc = spawn_agent(task, charter, date_str)
            if proc:
                retry_spawned.append({"id": task_id, "path": task_path,
                                      "proc": proc, "charter": charter})

    if retry_spawned:
        print(f"  [orch] Retrying {len(retry_spawned)} task(s): "
              f"{[t['id'] for t in retry_spawned]}", file=sys.stderr)

    # -----------------------------------------------------------------------
    # Completeness check (at configured hour): spawn any missing daily tasks
    # -----------------------------------------------------------------------
    completeness_tasks = _completeness_check(tasks, state, date_str)
    completeness_spawned = []
    already_handled = spawned_ids | {t["id"] for t in retry_spawned}
    for task in completeness_tasks:
        task_id = task["id"]
        if task_id in already_handled:
            continue

        task_path = task["path"]
        charter = load_charter(task_path)

        failures = check_preflight(task_id, charter)
        if failures:
            print(f"  [orch] {task_id}: completeness-check preflight FAILED", file=sys.stderr)
            continue

        task_state = state.setdefault("task_runs", {}).setdefault(task_id, {})
        retry_count = task_state.get("retry_count", 0)
        if task_state.get("last_retry_date") != date_str:
            retry_count = 0
        retry_count += 1
        task_state["retry_count"] = retry_count
        task_state["last_retry_date"] = date_str

        print(f"  [orch] {task_id}: COMPLETENESS spawn (missing summary for {date_str})",
              file=sys.stderr)
        proc = spawn_agent(task, charter, date_str)
        if proc:
            completeness_spawned.append({"id": task_id, "path": task_path,
                                         "proc": proc, "charter": charter})

    if completeness_spawned:
        print(f"  [orch] Completeness check spawned {len(completeness_spawned)} task(s): "
              f"{[t['id'] for t in completeness_spawned]}", file=sys.stderr)

    # -----------------------------------------------------------------------
    # Daily digest with guaranteed delivery
    # -----------------------------------------------------------------------
    # Send digest at DIGEST_HOUR (default 5 AM) so short tasks have time to
    # complete. Include whatever summaries exist — don't wait for long tasks.
    DIGEST_HOUR = 5
    digest_pending = state.get("digest_pending", False)
    digest_file_written = state.get("last_digest_date") == date_str

    if not digest_file_written and now.hour >= DIGEST_HOUR:
        # Digest hour reached: collect completed task summaries and send
        print(f"\n[orch] Sending daily digest for {date_str}...", file=sys.stderr)
        result = collect_and_send_digest(date_str, state)
        state["last_digest_date"] = date_str
        state["last_digest_time"] = now.isoformat()

        # Retry once after cooldown if rate-limited
        email_failed = isinstance(result, dict) and result.get("status") in ("rate_limited", "error")
        if isinstance(result, dict) and result.get("status") == "rate_limited":
            print(f"  [orch] Digest rate-limited, retrying after 35s...", file=sys.stderr)
            time.sleep(35)
            result = collect_and_send_digest(date_str, state)
            email_failed = isinstance(result, dict) and result.get("status") in ("rate_limited", "error")

        if email_failed:
            state["digest_pending"] = True
            state["digest_retry_count"] = 1
            print(f"  [orch] Digest email failed, will retry next cycle", file=sys.stderr)
        else:
            state["digest_pending"] = False
            state["digest_retry_count"] = 0
    elif not digest_file_written:
        print(f"  [orch] Digest deferred until {DIGEST_HOUR}:00 (now {now.hour}:00)",
              file=sys.stderr)
    elif digest_pending:
        # Retry pending digest email on later cycles
        digest_retry_count = state.get("digest_retry_count", 0)
        if digest_retry_count < 10:
            print(f"\n[orch] Retrying digest email (attempt {digest_retry_count + 1}/10)...",
                  file=sys.stderr)
            result = collect_and_send_digest(date_str, state)
            if isinstance(result, dict) and result.get("status") in ("rate_limited", "error"):
                state["digest_retry_count"] = digest_retry_count + 1
                print(f"  [orch] Digest email still failing, will retry next cycle",
                      file=sys.stderr)
            else:
                state["digest_pending"] = False
                state["digest_retry_count"] = 0
                print(f"  [orch] Digest email delivered on retry", file=sys.stderr)
        else:
            print(f"  [orch] Digest email: max retries (10) reached, giving up", file=sys.stderr)
            state["digest_pending"] = False

    save_state(state)

    # Status report
    all_spawned = ([t["id"] for t in spawned]
                   + [f"{t['id']}(retry)" for t in retry_spawned]
                   + [f"{t['id']}(completeness)" for t in completeness_spawned])
    print(f"\n[orch] Cycle complete.", file=sys.stderr)
    print(f"  Spawned: {all_spawned}", file=sys.stderr)
    print(f"  Skipped: {skipped}", file=sys.stderr)


if __name__ == "__main__":
    main()
