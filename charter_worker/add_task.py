"""charter-add-task — Natural-language task creation.

Creates a new task from a plain English description, auto-selecting
the right template, schedule, and execution mode.

Usage:
    charter-add-task "Send me a daily digest of new papers on LLM agent eval"
    charter-add-task "Track my job applications" --schedule daily --mode agent
"""

import argparse
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

import yaml

from .llm import call_llm_json


# ---------------------------------------------------------------------------
# Template discovery (reuses init_cmd pattern)
# ---------------------------------------------------------------------------

def _templates_dir() -> Path:
    """Find the templates directory."""
    # Same logic as init_cmd.py
    return Path(__file__).resolve().parent.parent / "templates"


def list_templates() -> list[str]:
    """Return names of available templates."""
    td = _templates_dir()
    if not td.is_dir():
        return []
    return sorted(
        d.name for d in td.iterdir()
        if d.is_dir() and (d / "charter.yaml").exists()
    )


# ---------------------------------------------------------------------------
# Step 1: Classify task (1 LLM call)
# ---------------------------------------------------------------------------

def classify_task(description: str, templates: list[str]) -> dict:
    """Use LLM to classify a natural-language task description.

    Returns: {template, task_id, name, schedule, execution_mode, definition}
    """
    prompt = f"""You are helping create a recurring automated task.

USER'S DESCRIPTION:
{description}

AVAILABLE TEMPLATES:
{', '.join(templates)}

Template descriptions:
- hello_world: Zero-dep demo, direct mode, counts files. Use as fallback.
- ltt_thinker: Proactive research agent with web search, synthesis, email reports.
  Good for: literature tracking, topic monitoring, hypothesis-driven research.
- experiment_task: Multi-step experiment runner with code generation and execution.
  Good for: automated experiments, benchmarking, data analysis pipelines.

Based on the description, determine:
1. Which template best fits (or hello_world as fallback)
2. A short snake_case task_id (max 30 chars)
3. A human-readable name
4. Schedule: hourly, daily, or weekly
5. Execution mode: agent (LLM runs workflow) or direct (script runs workflow)
6. A structured project definition with goal and scope

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "template": "template_name",
  "task_id": "short_snake_case_id",
  "name": "Human Readable Name",
  "schedule": "daily",
  "execution_mode": "agent",
  "definition": {{
    "project_id": "same as task_id",
    "goal": "2-3 sentence description of what the task should accomplish",
    "scope_boundaries": {{
      "in_scope": ["topic 1", "topic 2"],
      "out_of_scope": ["things to exclude"]
    }},
    "success_criteria": ["concrete deliverable 1", "concrete deliverable 2"],
    "actions": {{
      "web_search": {{"enabled": true}}
    }}
  }},
  "task_md_content": "Markdown instructions for the agent (if agent mode). Include step-by-step workflow."
}}"""

    return call_llm_json(prompt, timeout=180)


# ---------------------------------------------------------------------------
# Step 2: Create task folder
# ---------------------------------------------------------------------------

def create_task(instance_root: Path, classification: dict) -> Path:
    """Create task folder from classification, copying the template.

    Returns path to the created task directory.
    """
    task_id = classification["task_id"]
    template = classification.get("template", "hello_world")
    task_path = instance_root / "tasks" / task_id

    if task_path.exists():
        raise FileExistsError(f"Task directory already exists: {task_path}")

    # Copy template
    templates_dir = _templates_dir()
    template_dir = templates_dir / template
    if not template_dir.exists():
        template_dir = templates_dir / "hello_world"

    shutil.copytree(template_dir, task_path, dirs_exist_ok=True)

    # Ensure state directory
    (task_path / "state").mkdir(exist_ok=True)

    # Write/update charter.yaml
    charter_path = task_path / "charter.yaml"
    if charter_path.exists():
        with open(charter_path) as f:
            charter = yaml.safe_load(f) or {}
    else:
        charter = {}

    charter["task_id"] = task_id
    charter["name"] = classification.get("name", task_id)
    charter["schedule"] = {
        "frequency": classification.get("schedule", "daily"),
        "max_runtime_minutes": 60,
    }
    charter["execution"] = {
        "agent": "codex" if classification.get("execution_mode") == "agent" else "direct",
    }
    if classification.get("execution_mode") != "agent":
        charter["execution"]["entrypoint"] = "python run.py"
    charter["report"] = {"digest": True}

    with open(charter_path, "w") as f:
        yaml.dump(charter, f, default_flow_style=False, allow_unicode=True)

    # Write definition.yaml
    definition = classification.get("definition", {})
    if definition:
        definition["project_id"] = task_id
        with open(task_path / "definition.yaml", "w") as f:
            yaml.dump(definition, f, default_flow_style=False, allow_unicode=True)

    # Write task.md (for agent mode)
    task_md_content = classification.get("task_md_content")
    if task_md_content:
        with open(task_path / "task.md", "w") as f:
            f.write(task_md_content)

    return task_path


# ---------------------------------------------------------------------------
# Step 3: Register in registry
# ---------------------------------------------------------------------------

def register_task(instance_root: Path, task_id: str, task_path: str):
    """Append a task to tasks/registry.yaml."""
    registry_file = instance_root / "tasks" / "registry.yaml"

    if registry_file.exists():
        with open(registry_file) as f:
            registry = yaml.safe_load(f) or {}
    else:
        registry = {"version": 2, "tasks": []}

    tasks = registry.setdefault("tasks", [])

    # Check for duplicate
    for t in tasks:
        if t.get("id") == task_id:
            return  # already registered

    tasks.append({
        "id": task_id,
        "enabled": True,
        "path": task_path,
    })

    with open(registry_file, "w") as f:
        yaml.dump(registry, f, default_flow_style=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Add a task from a natural-language description"
    )
    parser.add_argument("description", help="What the task should do")
    parser.add_argument("--schedule", default=None,
                        choices=["hourly", "daily", "weekly"],
                        help="Override schedule (default: auto-detect)")
    parser.add_argument("--mode", default=None,
                        choices=["agent", "direct"],
                        help="Override execution mode (default: auto-detect)")
    parser.add_argument("--instance-dir", default=None,
                        help="Instance root directory")
    args = parser.parse_args()

    instance_root = Path(args.instance_dir) if args.instance_dir else Path(
        os.environ.get("CHARTER_INSTANCE_ROOT", ".")
    )

    templates = list_templates()
    if not templates:
        print("Error: no templates found. Run charter-init first.", file=sys.stderr)
        sys.exit(1)

    print(f"Classifying task...", file=sys.stderr)
    classification = classify_task(args.description, templates)

    # Apply CLI overrides
    if args.schedule:
        classification["schedule"] = args.schedule
    if args.mode:
        classification["execution_mode"] = args.mode

    task_id = classification.get("task_id", "new_task")
    print(f"  Template: {classification.get('template', '?')}", file=sys.stderr)
    print(f"  Task ID: {task_id}", file=sys.stderr)
    print(f"  Schedule: {classification.get('schedule', '?')}", file=sys.stderr)
    print(f"  Mode: {classification.get('execution_mode', '?')}", file=sys.stderr)

    print(f"Creating task...", file=sys.stderr)
    task_path = create_task(instance_root, classification)

    register_task(instance_root, task_id, f"tasks/{task_id}")

    print(f"\nCreated: {task_path}/", file=sys.stderr)
    print(f"  charter.yaml    — {classification.get('schedule', 'daily')}, "
          f"{classification.get('execution_mode', 'agent')} mode", file=sys.stderr)
    if (task_path / "definition.yaml").exists():
        print(f"  definition.yaml — {classification.get('definition', {}).get('goal', '')[:60]}",
              file=sys.stderr)
    if (task_path / "task.md").exists():
        print(f"  task.md         — agent instructions", file=sys.stderr)
    print(f"\nTest it:", file=sys.stderr)
    print(f"  charter-orchestrator --force {task_id}", file=sys.stderr)
    print(f"\nWhen stable, promote to direct mode:", file=sys.stderr)
    print(f"  charter-promote {task_id}", file=sys.stderr)


if __name__ == "__main__":
    main()
