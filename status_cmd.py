#!/usr/bin/env python3
"""charter-status — Show the current state of all tasks in an instance.

Reads registry, orchestrator state, lock files, and latest summaries
to produce a human-readable status table.

Usage:
    charter-status
    charter-status --instance-dir /path/to/my-tasks
    charter-status --output status.md
    charter-status --json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml


def _resolve_instance_root(cli_arg: str | None = None) -> Path:
    if cli_arg:
        return Path(cli_arg).resolve()
    env = os.environ.get("CHARTER_INSTANCE_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd()


def _load_yaml(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f)


def _find_latest_summary(summaries_root: Path, task_id: str) -> dict | None:
    """Find the most recent summary.json for a task."""
    if not summaries_root.exists():
        return None
    # Scan date dirs in reverse order
    date_dirs = sorted(summaries_root.iterdir(), reverse=True)
    for date_dir in date_dirs[:7]:  # check last 7 days
        summary_path = date_dir / "tasks" / task_id / "summary.json"
        if summary_path.exists():
            try:
                with open(summary_path) as f:
                    data = json.load(f)
                data["_summary_date"] = date_dir.name
                return data
            except (json.JSONDecodeError, OSError):
                continue
    return None


def _check_lock(task_path: Path) -> dict | None:
    """Check if a task is currently locked (running)."""
    lock_file = task_path / ".lock"
    if not lock_file.exists():
        return None
    try:
        with open(lock_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"pid": "?", "started_at": "?"}


def collect_status(instance_root: Path) -> dict:
    """Collect status information for all registered tasks.

    Returns a dict with keys: instance_root, tasks, orchestrator_state, timestamp.
    Each task entry has: id, path, enabled, schedule, last_run, last_status,
    locked, latest_summary.
    """
    registry_path = instance_root / "tasks" / "registry.yaml"
    state_path = instance_root / "orchestrator_state.json"
    summaries_root = instance_root / "daily_summaries"

    registry = _load_yaml(registry_path)
    if registry is None:
        return {
            "instance_root": str(instance_root),
            "tasks": [],
            "error": f"No registry at {registry_path}",
            "timestamp": datetime.now().isoformat(),
        }

    orch_state = {}
    if state_path.exists():
        try:
            with open(state_path) as f:
                orch_state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    task_runs = orch_state.get("task_runs", {})
    tasks_info = []

    raw_tasks = registry.get("tasks", []) if isinstance(registry, dict) else []
    for task_entry in raw_tasks:
        task_id = task_entry.get("id", "?")
        task_path = instance_root / task_entry.get("path", f"tasks/{task_id}")
        enabled = task_entry.get("enabled", True)

        # Load charter for schedule info
        charter = _load_yaml(task_path / "charter.yaml") or {}
        schedule = charter.get("schedule", {})
        execution = charter.get("execution", {})

        # Run info from orchestrator state
        run_info = task_runs.get(task_id, {})
        last_run = run_info.get("last_run", "")
        last_success = run_info.get("last_success_date", "")

        # Lock status
        lock = _check_lock(task_path)

        # Latest summary
        summary = _find_latest_summary(summaries_root, task_id)

        status_str = "unknown"
        if lock:
            status_str = "running"
        elif summary:
            status_str = summary.get("status", "unknown")
        elif not enabled:
            status_str = "disabled"

        tasks_info.append({
            "id": task_id,
            "enabled": enabled,
            "mode": execution.get("agent", "codex"),
            "frequency": schedule.get("frequency", "?"),
            "status": status_str,
            "last_run": last_run[:16] if last_run else "-",
            "last_success": last_success or "-",
            "summary_date": summary.get("_summary_date", "-") if summary else "-",
            "tldr": summary.get("tldr", [])[:1] if summary else [],
            "errors": summary.get("errors", []) if summary else [],
            "locked": lock is not None,
        })

    return {
        "instance_root": str(instance_root),
        "tasks": tasks_info,
        "orchestrator_state": {
            "last_digest_date": orch_state.get("last_digest_date", "-"),
        },
        "timestamp": datetime.now().isoformat(),
    }


def format_table(status: dict) -> str:
    """Format status as a human-readable text table."""
    lines = []
    lines.append(f"Charter-worker status — {status['instance_root']}")
    lines.append(f"Timestamp: {status['timestamp'][:19]}")
    lines.append("")

    if "error" in status:
        lines.append(f"Error: {status['error']}")
        return "\n".join(lines)

    tasks = status["tasks"]
    if not tasks:
        lines.append("No tasks registered.")
        return "\n".join(lines)

    # Table header
    header = f"{'Task':<25} {'Mode':<10} {'Freq':<8} {'Status':<10} {'Last Run':<18} {'Summary':<12}"
    lines.append(header)
    lines.append("-" * len(header))

    for t in tasks:
        lock_marker = " *" if t["locked"] else ""
        lines.append(
            f"{t['id']:<25} {t['mode']:<10} {t['frequency']:<8} "
            f"{t['status'] + lock_marker:<10} {t['last_run']:<18} {t['summary_date']:<12}"
        )

    # Errors
    error_tasks = [t for t in tasks if t["errors"]]
    if error_tasks:
        lines.append("")
        lines.append("Errors:")
        for t in error_tasks:
            for e in t["errors"][:2]:
                msg = e.get("message", str(e))[:80] if isinstance(e, dict) else str(e)[:80]
                lines.append(f"  {t['id']}: {msg}")

    # TL;DR
    tldr_tasks = [t for t in tasks if t["tldr"]]
    if tldr_tasks:
        lines.append("")
        lines.append("Latest:")
        for t in tldr_tasks:
            for item in t["tldr"]:
                lines.append(f"  {t['id']}: {item[:100]}")

    orch = status.get("orchestrator_state", {})
    lines.append("")
    lines.append(f"Last digest: {orch.get('last_digest_date', '-')}")
    lines.append(f"* = currently running (locked)")

    return "\n".join(lines)


def format_markdown(status: dict) -> str:
    """Format status as markdown."""
    lines = []
    lines.append(f"# Charter-worker Status")
    lines.append(f"")
    lines.append(f"**Instance:** `{status['instance_root']}`")
    lines.append(f"**Updated:** {status['timestamp'][:19]}")
    lines.append("")

    if "error" in status:
        lines.append(f"> Error: {status['error']}")
        return "\n".join(lines)

    tasks = status["tasks"]
    if not tasks:
        lines.append("No tasks registered.")
        return "\n".join(lines)

    lines.append("| Task | Mode | Freq | Status | Last Run | Summary |")
    lines.append("|------|------|------|--------|----------|---------|")
    for t in tasks:
        lock = " (running)" if t["locked"] else ""
        lines.append(
            f"| {t['id']} | {t['mode']} | {t['frequency']} | "
            f"{t['status']}{lock} | {t['last_run']} | {t['summary_date']} |"
        )

    error_tasks = [t for t in tasks if t["errors"]]
    if error_tasks:
        lines.append("")
        lines.append("## Errors")
        for t in error_tasks:
            for e in t["errors"][:2]:
                msg = e.get("message", str(e))[:120] if isinstance(e, dict) else str(e)[:120]
                lines.append(f"- **{t['id']}**: {msg}")

    lines.append("")
    orch = status.get("orchestrator_state", {})
    lines.append(f"*Last digest: {orch.get('last_digest_date', '-')}*")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Show charter-worker task status"
    )
    parser.add_argument(
        "--instance-dir",
        default=None,
        help="Instance root directory",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Write status to file (supports .md for markdown)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON",
    )
    args = parser.parse_args()

    instance_root = _resolve_instance_root(args.instance_dir)
    status = collect_status(instance_root)

    if args.json_output:
        output = json.dumps(status, indent=2)
    elif args.output and args.output.endswith(".md"):
        output = format_markdown(status)
    else:
        output = format_table(status)

    if args.output:
        Path(args.output).write_text(output)
        print(f"Status written to {args.output}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
