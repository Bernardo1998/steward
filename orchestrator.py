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
        if sandbox_mode == "none":
            # Full access — for tasks needing IMAP/SMTP/network
            cmd = [
                "codex", "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C", str(working_dir),
                "--add-dir", str(REPO_ROOT),
                "-",  # read prompt from stdin
            ]
        else:
            # Sandboxed — workspace-write via --full-auto
            cmd = [
                "codex", "exec", "--full-auto",
                "-C", str(working_dir),
                "--add-dir", str(REPO_ROOT),
                "-",  # read prompt from stdin
            ]
    elif agent == "claude":
        cmd = [
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
        ]
    else:
        print(f"  [orch] Unknown agent '{agent}' for {task_id}, skipping", file=sys.stderr)
        return None

    print(f"  [orch] Spawning {agent} for {task_id} in {working_dir}", file=sys.stderr)

    # Open log file for output
    log_fh = open(log_file, "a")

    if agent == "codex":
        # Pipe prompt via stdin from file
        prompt_fh = open(prompt_file, "r")
        proc = subprocess.Popen(
            cmd,
            stdin=prompt_fh,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
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
    except Exception as e:
        print(f"  [orch] Digest email failed: {e}", file=sys.stderr)


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

    # Daily digest (first cycle of the day)
    if state.get("last_digest_date") != date_str:
        print(f"\n[orch] Sending daily digest for {date_str}...", file=sys.stderr)
        collect_and_send_digest(date_str, state)
        state["last_digest_date"] = date_str
        state["last_digest_time"] = now.isoformat()

    save_state(state)

    # Status report
    print(f"\n[orch] Cycle complete.", file=sys.stderr)
    print(f"  Spawned: {[t['id'] for t in spawned]}", file=sys.stderr)
    print(f"  Skipped: {skipped}", file=sys.stderr)


if __name__ == "__main__":
    main()
