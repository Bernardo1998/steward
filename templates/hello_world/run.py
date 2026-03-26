#!/usr/bin/env python3
"""Hello World — minimal charter-worker task.

Proves the task contract works with zero external dependencies:
no email, no API keys, no codex.  Just Python.

What it does:
  1. Scans the repo for Python files
  2. Writes summary.json + summary.md to daily_summaries/
  3. Prints a one-line result

Run directly:
  python run.py
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
# Walk up to find the instance root (contains daily_summaries/)
INSTANCE_ROOT = TASK_DIR.parent.parent


def main():
    started = time.time()
    today = datetime.now().strftime("%Y-%m-%d")

    # --- Do the work: count Python files ---
    py_files = list(INSTANCE_ROOT.rglob("*.py"))
    file_count = len(py_files)
    largest = max(py_files, key=lambda p: p.stat().st_size) if py_files else None

    # --- Write summary (the task contract) ---
    out_dir = INSTANCE_ROOT / "daily_summaries" / today / "tasks" / "hello_world"
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = round(time.time() - started, 2)

    summary_json = {
        "task_id": "hello_world",
        "date": today,
        "status": "success",
        "tldr": [
            f"Found {file_count} Python files in the instance",
            f"Largest: {largest.name} ({largest.stat().st_size:,} bytes)" if largest else "No Python files found",
        ],
        "action_items": [],
        "artifacts": [
            {"path": str(out_dir / "summary.md"), "description": "Human-readable summary"},
        ],
        "errors": [],
        "metadata": {
            "started_at": datetime.now().isoformat(),
            "ended_at": datetime.now().isoformat(),
            "duration_s": duration,
            "budget_hint": "low",
        },
    }

    summary_md = f"""# hello_world — {today}

## TL;DR
- Found **{file_count}** Python files in the instance
- Largest: `{largest.name}` ({largest.stat().st_size:,} bytes)

## What I did
Scanned `{INSTANCE_ROOT}` recursively for `*.py` files.

## Run metadata
- Duration: {duration}s
- Budget hint: low
"""

    (out_dir / "summary.json").write_text(json.dumps(summary_json, indent=2))
    (out_dir / "summary.md").write_text(summary_md)

    print(f"hello_world: {file_count} Python files, summary at {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
