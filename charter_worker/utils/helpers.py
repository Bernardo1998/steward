"""Shared utilities for tasks. NO shared state allowed."""

import json
import os
from datetime import datetime
from pathlib import Path


def get_today_str() -> str:
    """Return today's date as YYYY-MM-DD."""
    return datetime.now().strftime("%Y-%m-%d")


def get_instance_root() -> Path:
    """Return the instance root (where tasks/ and daily_summaries/ live).

    Reads CHARTER_INSTANCE_ROOT env var, falls back to cwd().
    """
    env = os.environ.get("CHARTER_INSTANCE_ROOT")
    if env:
        return Path(env).resolve()
    return Path.cwd()


# Backward compat alias
get_repo_root = get_instance_root


def get_daily_summary_dir(task_id: str) -> Path:
    """Return today's summary directory for a task."""
    root = get_instance_root()
    today = get_today_str()
    return root / "daily_summaries" / today / "tasks" / task_id


def ensure_summary_dir(task_id: str) -> Path:
    """Create and return the summary directory for a task."""
    summary_dir = get_daily_summary_dir(task_id)
    summary_dir.mkdir(parents=True, exist_ok=True)
    return summary_dir


def load_json(path: Path) -> dict:
    """Load JSON from a file, return empty dict if not exists."""
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict) -> None:
    """Save data as JSON to a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_task_state(task_id: str) -> dict:
    """Load state for a task from its state directory."""
    root = get_instance_root()
    state_path = root / "tasks" / task_id / "state" / "state.json"
    return load_json(state_path)


def save_task_state(task_id: str, state: dict) -> None:
    """Save state for a task to its state directory."""
    root = get_instance_root()
    state_path = root / "tasks" / task_id / "state" / "state.json"
    save_json(state_path, state)
