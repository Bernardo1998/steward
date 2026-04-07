# Agent Setup Guide

You are an AI coding agent. Follow these steps exactly to set up a working
steward instance with a running hello_world task. Every command and
file content is given inline — execute them in order.

## Prerequisites

- Python 3.10+
- pip
- git
- The steward repo cloned locally

## Step 1: Install steward

```bash
pip install -e /path/to/steward/
```

Verify:
```bash
python -c "import steward; print('OK')"
```

## Step 2: Create an instance directory

```bash
mkdir -p ~/my-steward-instance
cd ~/my-steward-instance
git init
```

## Step 3: Create the task registry

Write this file to `tasks/registry.yaml`:

```yaml
version: 2
tasks:
  - id: "hello_world"
    enabled: true
    path: "tasks/hello_world"
```

```bash
mkdir -p tasks
```

## Step 4: Copy the hello_world task

```bash
cp -r /path/to/steward/templates/hello_world tasks/hello_world
```

If the template path is not available, create the files manually:

Write `tasks/hello_world/charter.yaml`:

```yaml
task_id: hello_world
name: "Hello World"

schedule:
  frequency: "daily"
  max_runtime_minutes: 1

execution:
  agent: "direct"
  entrypoint: "python run.py"

report:
  digest: true
```

Write `tasks/hello_world/run.py`:

```python
#!/usr/bin/env python3
"""Hello World task — counts Python files and writes a summary."""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
INSTANCE_ROOT = TASK_DIR.parent.parent

def main():
    started = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    py_files = list(INSTANCE_ROOT.rglob("*.py"))
    file_count = len(py_files)
    largest = max(py_files, key=lambda p: p.stat().st_size) if py_files else None

    out_dir = INSTANCE_ROOT / "daily_summaries" / today / "tasks" / "hello_world"
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = round(time.time() - started, 2)

    summary = {
        "task_id": "hello_world",
        "date": today,
        "status": "success",
        "tldr": [
            f"Found {file_count} Python files",
            f"Largest: {largest.name} ({largest.stat().st_size:,} bytes)" if largest else "No Python files",
        ],
        "action_items": [],
        "errors": [],
        "metadata": {"duration_s": duration, "budget_hint": "low"},
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / "summary.md").write_text(
        f"# hello_world — {today}\n\nFound **{file_count}** Python files.\n"
    )
    print(f"hello_world: {file_count} Python files, summary at {out_dir}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

Create the state directory:

```bash
mkdir -p tasks/hello_world/state
```

## Step 5: Run the task directly

```bash
cd ~/my-steward-instance/tasks/hello_world
python run.py
```

Expected output: `hello_world: N Python files, summary at ...`

## Step 6: Run via the orchestrator

```bash
cd ~/my-steward-instance
STEWARD_INSTANCE_ROOT=$(pwd) steward --force hello_world
```

This spawns the task through the orchestrator, which manages locks, timeouts,
and summary collection.

## Step 7: Verify

Run these checks — all should pass:

```bash
# 1. summary.json exists and is valid JSON
TODAY=$(date +%Y-%m-%d)
python -c "import json; json.load(open('daily_summaries/$TODAY/tasks/hello_world/summary.json')); print('summary.json: OK')"

# 2. summary.md exists
test -f "daily_summaries/$TODAY/tasks/hello_world/summary.md" && echo "summary.md: OK"

# 3. Status is success
python -c "import json; d=json.load(open('daily_summaries/$TODAY/tasks/hello_world/summary.json')); assert d['status']=='success', d['status']; print('status: success')"
```

## Done

The instance is working. To add your own tasks:

1. Create a new folder under `tasks/` with `charter.yaml` and either `run.py`
   (for `agent: direct`) or `task.md` (for `agent: codex` or `agent: claude`)
2. Register it in `tasks/registry.yaml`
3. Run: `steward --force <task_id>`

See the [README](../README.md) for charter.yaml reference, email setup,
cron automation, and the full module documentation.

See [templates/ltt_thinker/](../templates/ltt_thinker/) for an autonomous
research agent template, or [templates/experiment_task/](../templates/experiment_task/)
for an experiment runner template.
