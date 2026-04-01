#!/usr/bin/env python3
"""charter-init — Bootstrap a new steward instance directory.

Creates the instance structure, copies a template task, and sets up
.gitignore and registry so the user can immediately run:

    charter-orchestrator --force hello_world

Usage:
    charter-init <directory>
    charter-init <directory> --template ltt_thinker
    charter-init <directory> --list-templates
"""

import argparse
import shutil
import sys
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Templates that require external agents (codex/claude) — not suitable as
# zero-dependency first-run demos.
_AGENT_TEMPLATES = {"ltt_thinker", "experiment_task"}


def list_templates() -> list[str]:
    """Return names of available templates."""
    if not TEMPLATES_DIR.is_dir():
        return []
    return sorted(
        d.name
        for d in TEMPLATES_DIR.iterdir()
        if d.is_dir() and (d / "charter.yaml").exists()
    )


def init_instance(target: Path, template: str = "hello_world") -> dict:
    """Create a new steward instance directory.

    Returns a dict with keys: created_dirs, copied_files, warnings.
    """
    result = {"created_dirs": [], "copied_files": [], "warnings": []}

    # Validate template
    template_dir = TEMPLATES_DIR / template
    if not template_dir.is_dir():
        available = list_templates()
        raise FileNotFoundError(
            f"Template '{template}' not found. Available: {available}"
        )

    # Create directory structure
    target.mkdir(parents=True, exist_ok=True)
    for subdir in ["tasks", "daily_summaries", "logs"]:
        d = target / subdir
        d.mkdir(parents=True, exist_ok=True)
        result["created_dirs"].append(str(d))

    # Copy template into tasks/<template>/
    task_dest = target / "tasks" / template
    if task_dest.exists():
        result["warnings"].append(f"Task directory already exists: {task_dest}")
    else:
        shutil.copytree(str(template_dir), str(task_dest))
        result["copied_files"].append(str(task_dest))

    # Ensure state/ exists
    state_dir = task_dest / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    # Write registry
    registry_path = target / "tasks" / "registry.yaml"
    if registry_path.exists():
        result["warnings"].append(
            f"Registry already exists: {registry_path} — not overwriting"
        )
    else:
        registry_path.write_text(
            f'version: 2\ntasks:\n  - id: "{template}"\n'
            f'    enabled: true\n    path: "tasks/{template}"\n'
        )
        result["copied_files"].append(str(registry_path))

    # Write .gitignore
    gitignore_path = target / ".gitignore"
    if gitignore_path.exists():
        result["warnings"].append(
            f".gitignore already exists: {gitignore_path} — not overwriting"
        )
    else:
        gitignore_path.write_text(
            "email_config.yaml\n"
            "**/openai_api_key.json\n"
            "orchestrator_state.json\n"
            "daily_summaries/\n"
            "logs/\n"
            "*.lock\n"
        )
        result["copied_files"].append(str(gitignore_path))

    # Write run.sh
    run_sh = target / "run.sh"
    if not run_sh.exists():
        run_sh.write_text(
            '#!/bin/bash\nset -e\ncd "$(dirname "$0")"\n'
            'STEWARD_INSTANCE_ROOT="$(pwd)" charter-orchestrator\n'
        )
        run_sh.chmod(0o755)
        result["copied_files"].append(str(run_sh))

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Bootstrap a new steward instance directory"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        help="Path to create the instance directory",
    )
    parser.add_argument(
        "--template",
        default="hello_world",
        help="Template to copy (default: hello_world)",
    )
    parser.add_argument(
        "--list-templates",
        action="store_true",
        help="List available templates and exit",
    )
    args = parser.parse_args()

    if args.list_templates:
        templates = list_templates()
        if not templates:
            print("No templates found.", file=sys.stderr)
            return 1
        print("Available templates:")
        for t in templates:
            marker = " (requires agent)" if t in _AGENT_TEMPLATES else ""
            print(f"  {t}{marker}")
        return 0

    if not args.directory:
        parser.error("directory is required (unless using --list-templates)")

    target = Path(args.directory).resolve()
    try:
        result = init_instance(target, args.template)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Initialized steward instance at {target}")
    if result["warnings"]:
        for w in result["warnings"]:
            print(f"  Warning: {w}")
    print(f"\nNext steps:")
    print(f"  cd {target}")
    print(f"  charter-orchestrator --force {args.template}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
