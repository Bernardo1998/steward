"""steward.plan_runner.scaffolder — bootstrap a new plan_runner task.

The scaffolder needs no external template directory. All per-task files
(goal.md, plan.yaml, README.md, run.py, state/state.json) are generated
from inline string templates plus the user's CLI inputs.

CLI invocation:

    steward-plan-runner init <task_id> \\
        --experiment-repo <abs_path> \\
        --email-prefix "[TAG]" \\
        [--instance-dir <dir>]      # default: cwd
        [--goal-file F] [--plan-file F]
        [--schedule hourly|daily]   # default: hourly
        [--dry-run] [--force]

The scaffolder bootstraps the instance directory if needed: creates
`<instance_dir>/tasks/<task_id>/...` and appends to (or creates)
`<instance_dir>/tasks.yaml`.

There is no dependency on any other repo. Steward must be importable
(typically: ``pip install -e <path-to-steward-repo>``).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Inline templates (no external files needed)
# ---------------------------------------------------------------------------

_GOAL_MD_TEMPLATE = """\
# {task_id} — locked goal

This is the constitution. The runner reads this every cycle. Edit only
when YOU decide the goal has shifted. The agent will NEVER edit this
file.

## Goal

<One paragraph stating the project-level goal. The runner reads this
for context but never edits it. Be specific enough that a stranger
can check whether a result meets it.>

## Things to show

- <Locked deliverable 1>
- <Locked deliverable 2>

## Scope

### In scope
- <thing you'll actually do>

### Out of scope
- <thing you won't do>
- Modifying any external library's internal behavior

## Success criteria

- <Locked success criterion 1>
- <Locked success criterion 2>

## Methodology

- **P0** <title>: <one-paragraph description>
- **P1** <title>: <one-paragraph description>

## Orchestrator

- **frequency:** {schedule}
- **max_runtime_minutes:** 25
- **email_prefix:** `{email_prefix}`
- **sandbox:** none
- **agent:** direct

## Experiment config

- **model:** gpt-5.4-mini
- **step_timeout_seconds:** 1800
- **soft_criteria_timeout_seconds:** 120
- **deepening_timeout_seconds:** 180
- **reviewer_scrutiny_timeout_seconds:** 180
- **budget_hard_limit_usd:** 50
- **max_step_cost_usd:** 30
- **experiment_repo:** {experiment_repo}
- **llm_dispatch_when_no_run_command:** true
"""


_PLAN_YAML_TEMPLATE = """\
# plan.yaml — your authored plan. Source of truth for what to do next.
#
# Schema (validated by steward.phases.plan_phases.load_plan):
#   version, title, approved_at, approved_by, parent_state_version: required.
#   stages: optional but recommended. Groups step ids into approval-gated
#           chapters. Every plan.steps[].id must appear in exactly one stage.
#           Omit `stages:` to treat the whole plan as a single implicit
#           stage (fully autonomous mode, no approval gates).
#   steps: non-empty list. Each step needs id, type, description, success_criteria.
#   step.type ∈ {{experiment, research, replicate, ablate, robust,
#                 citation_harden, failure_probe, gap_check}}.
#   step.success_criteria.hard: list of strings using ONE of these grammars:
#     - "file_exists: <path-relative-to-experiment_repo>"
#     - "metric: <path>::<dotted.key> <op> <value>"
#         op ∈ {{==, !=, >, <, >=, <=, is number, is string}}
#   step.success_criteria.soft.rubric: free-text LLM rubric (optional but at
#     least one of hard/soft must be present).
#   step.depends_on: list of earlier step ids (DAG).
#   step.run_command (optional): shell command executed in experiment_repo
#     during dispatch. Omit for autonomous Codex dispatch.
#   step.max_attempts (default 3), step.max_deepening_cycles (default 4).

version: 1
title: "<short headline for this plan version>"
approved_at: "{approved_at}"
approved_by: "user"
parent_state_version: 1

stages:
  - id: "warmup"
    description: "Smoke test confirming dispatch end-to-end."
    steps: ["s0_warmup_smoke"]
  - id: "main"
    description: "Main body of the plan."
    steps: ["s1_<short_handle>"]

steps:
  - id: "s0_warmup_smoke"
    type: "experiment"
    description: >
      Smoke test: write a minimal results JSON to prove the dispatcher
      reaches the experiment_repo. Pick any tiny computation; the goal
      is that results/s0_warmup_smoke_results.json exists.
    depends_on: []
    success_criteria:
      hard:
        - "file_exists: results/s0_warmup_smoke_results.json"
      soft:
        rubric: |
          Results JSON exists, is valid JSON, and contains at least one
          numeric field. No real experiment required.
    max_attempts: 2
    max_deepening_cycles: 1
    notes: ""

  - id: "s1_<short_handle>"
    type: "experiment"
    description: >
      <1-2 sentences describing what this step does and what it produces.
      Concrete enough for an LLM to act on it without further design
      input — if the description leaves real design choices unresolved,
      the sub-agent will emit `INTENT_NEEDED: <question>` and pause.>
    depends_on: ["s0_warmup_smoke"]
    success_criteria:
      hard:
        - "file_exists: results/s1_results.json"
        # - "metric: results/s1_results.json::pass_rate >= 0.5"
      soft:
        rubric: |
          <Plain-English description of the qualitative outcome that
          would satisfy this step.>
    max_attempts: 3
    max_deepening_cycles: 4
    notes: ""

# Runner-appended deepening actions. Do NOT hand-edit unless you intend to.
deepening_added_by_runner: []
"""


_README_MD_TEMPLATE = """\
# {task_id}

Plan-runner task. The orchestrator engine is `steward.plan_runner.PlanRunner`;
this folder holds **only** the per-task data the engine needs.

## Files

| File | Role |
|---|---|
| `goal.md` | **You author.** Locked constitution. Edit only when the goal shifts. |
| `plan.yaml` | **You author.** Steps + stages. Bump `version` on every edit. |
| `README.md` | This file. |
| `run.py` | 10-line caller of `PlanRunner.run()`. Do not edit. |
| `state/state.json` | Machine truth: per-step + cooldown + intent + approval log + email thread. |
| `state/draft_findings.md` | Latest cycle's findings (overwritten each cycle). |
| `state/exploration_log.jsonl` | Append-only dispatch log; rotates at 50 entries. |
| `state/current_step.md` | Machine-written each cycle. The cycle's step. |
| `state/inbox.md` | Machine-written each cycle. Latest user reply. |
| `state/recap.md` | Machine-written each cycle. Step checklist + findings + on-demand index. |

## Day-to-day workflow

| Situation | What to do |
|---|---|
| Approve advancing to the next stage | Reply to the cycle email with `/approve <stage_id>` or `/approve next`. |
| Redirect / replan | Edit `plan.yaml`, bump `version`, update `approved_at`. |
| Answer an `INTENT_NEEDED: ...` question | Reply with `/answer <text>`, OR add `/answer <text>` to `plan.yaml::steps[<id>].notes`. |
| Pause | `echo 'paused: true' > state/status.yaml`. |
| Inspect cycle state | `cat state/recap.md`. |

## Force a cycle now

```bash
python3 run.py
```

The runner self-throttles via `cooldown_hours` (default 3). If you need
to override, edit the `PlanRunner(...)` call in `run.py`.

## Canonical docs

See `<your steward repo>/steward/plan_runner/SETUP_GUIDE.md` for the
end-to-end setup runbook.
"""


_RUN_PY_TEMPLATE = '''\
#!/usr/bin/env python3
"""{task_id} — cycle entrypoint."""
import os
from pathlib import Path

from steward.plan_runner import PlanRunner

TASK_DIR = Path(__file__).resolve().parent
# `STEWARD_INSTANCE_ROOT` is where steward looks for `email_config.yaml`
# and writes `daily_summaries/`. For a one-task-per-machine setup this
# is the parent of `tasks/`. For multi-task instances it stays the same.
os.environ.setdefault("STEWARD_INSTANCE_ROOT", str(TASK_DIR.parent.parent))


if __name__ == "__main__":
    PlanRunner(
        task_dir=TASK_DIR,
        experiment_repo={experiment_repo!r},
        email_prefix={email_prefix!r},
    ).run()
'''


_STATE_JSON_TEMPLATE = {
    "schema_version": 1,
    "plan_version": 1,
    "plan_started_at": None,
    "cooldown": {
        "last_cycle_time": None,
        "cycle": 0,
        "days_since_reply": 0,
    },
    "step_progress": {},
    "stage_progress": {},
    "awaiting_stage_approval": None,
    "pending_intent_question": None,
    "weaknesses_log": [],
    "approval_log": [],
    "approved_through_stage": None,
    "email_thread_id": "",
    "last_message_id": "",
    "stuck_counter": 0,
    "max_stuck_cycles": 5,
}


_TASKS_YAML_HEADER = """\
# tasks.yaml — registry of enabled tasks for this steward instance.
# Each entry is a per-task block consumed by steward.orchestrator.
#
# Edit `enabled: false` to turn a task off; the runner skips disabled
# tasks at the cron tick.

version: 1
defaults:
  timezone: "America/Los_Angeles"
  run_all_enabled: true

tasks:
"""


_TASKS_YAML_BLOCK = """\

  - id: "{task_id}"
    enabled: true
    schedule: "{schedule}"
    entrypoint: "tasks/{task_id}/run.py"
    side_effects:
      - "write_repo_files"
      - "network_access"
      - "write_external_repo:{experiment_repo}"
      - "external_git_push:{experiment_repo}"
    budget_hint: "high"
    notes: "Plan-runner v3.1 task. Scaffolded by `steward-plan-runner init`."
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_task(
    task_id: str,
    instance_dir: Path,
    experiment_repo: str,
    email_prefix: str,
    schedule: str = "hourly",
    goal_file: Optional[Path] = None,
    plan_file: Optional[Path] = None,
    dry_run: bool = False,
    force: bool = False,
) -> Path:
    """Bootstrap a new plan_runner task in `instance_dir/tasks/<task_id>/`.

    Side effects (idempotent w.r.t. an already-bootstrapped instance):
      - Create `<instance_dir>/tasks/` if missing.
      - Create `<instance_dir>/tasks.yaml` if missing (with the standard
        header). Otherwise append a registry block.
      - Create `<instance_dir>/tasks/<task_id>/` with goal.md, plan.yaml,
        README.md, run.py, state/state.json (and an empty
        exploration_log.jsonl). Raises FileExistsError if the folder
        exists and `force=False`.
      - If `goal_file` is provided, its contents replace the inline goal
        template. Likewise for `plan_file`.

    Returns the new task dir.
    """
    instance_dir = Path(instance_dir).resolve()
    tasks_root = instance_dir / "tasks"
    new_path = tasks_root / task_id

    if new_path.exists() and not force:
        raise FileExistsError(
            f"{new_path} already exists. Pass force=True to overwrite."
        )

    actions: list[str] = []
    if not instance_dir.exists():
        actions.append(f"mkdir -p {instance_dir}")
    if not tasks_root.exists():
        actions.append(f"mkdir -p {tasks_root}")
    if force and new_path.exists():
        actions.append(f"rmtree {new_path}")
    actions.append(f"create {new_path}/{{goal.md, plan.yaml, README.md, run.py}}")
    actions.append(f"create {new_path}/state/{{state.json, exploration_log.jsonl}}")
    if goal_file:
        actions.append(f"copy contents of {goal_file} into {new_path}/goal.md")
    if plan_file:
        actions.append(f"copy contents of {plan_file} into {new_path}/plan.yaml")
    registry_path = instance_dir / "tasks.yaml"
    if registry_path.exists():
        actions.append(f"append {task_id!r} block to {registry_path}")
    else:
        actions.append(f"create {registry_path} with header + {task_id!r} block")

    if dry_run:
        print("Dry run; would do:")
        for a in actions:
            print(f"  - {a}")
        return new_path

    # --- Bootstrap the instance + task ---
    instance_dir.mkdir(parents=True, exist_ok=True)
    tasks_root.mkdir(parents=True, exist_ok=True)

    if force and new_path.exists():
        shutil.rmtree(new_path)
    new_path.mkdir(parents=True)
    (new_path / "state").mkdir()

    # goal.md
    if goal_file:
        goal_body = Path(goal_file).read_text(encoding="utf-8")
    else:
        goal_body = _GOAL_MD_TEMPLATE.format(
            task_id=task_id,
            schedule=schedule,
            email_prefix=email_prefix,
            experiment_repo=experiment_repo,
        )
    (new_path / "goal.md").write_text(goal_body, encoding="utf-8")

    # plan.yaml
    if plan_file:
        plan_body = Path(plan_file).read_text(encoding="utf-8")
    else:
        plan_body = _PLAN_YAML_TEMPLATE.format(
            approved_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        )
    (new_path / "plan.yaml").write_text(plan_body, encoding="utf-8")

    # README.md
    (new_path / "README.md").write_text(
        _README_MD_TEMPLATE.format(task_id=task_id), encoding="utf-8"
    )

    # run.py (10-line caller)
    (new_path / "run.py").write_text(
        _RUN_PY_TEMPLATE.format(
            task_id=task_id,
            experiment_repo=experiment_repo,
            email_prefix=email_prefix,
        ),
        encoding="utf-8",
    )
    (new_path / "run.py").chmod(0o755)

    # state/state.json + empty exploration log
    (new_path / "state" / "state.json").write_text(
        json.dumps(_STATE_JSON_TEMPLATE, indent=2) + "\n",
        encoding="utf-8",
    )
    (new_path / "state" / "exploration_log.jsonl").write_text("", encoding="utf-8")

    # tasks.yaml: bootstrap or append
    block = _TASKS_YAML_BLOCK.format(
        task_id=task_id,
        schedule=schedule,
        experiment_repo=experiment_repo,
    )
    if registry_path.exists():
        existing = registry_path.read_text(encoding="utf-8")
        if not existing.endswith("\n"):
            existing += "\n"
        registry_path.write_text(existing + block, encoding="utf-8")
    else:
        registry_path.write_text(_TASKS_YAML_HEADER + block, encoding="utf-8")

    print(f"Scaffolded: {new_path}")
    print()
    print("NEXT STEPS:")
    print(f"  1. Edit {new_path / 'goal.md'}     (fill in Goal / Things to show / Scope / etc.)")
    print(f"  2. Edit {new_path / 'plan.yaml'}   (define your stages + steps)")
    print(f"  3. Verify:  python3 -m steward.plan_runner.scaffolder verify {new_path}")
    print(f"  4. First cycle:  python3 {new_path / 'run.py'}")
    print()
    return new_path


def verify_task(task_dir: Path) -> None:
    """Static verification of a scaffolded task. Raises on any failure.

    Checks:
      - goal.md parses and contains email_prefix + experiment_repo.
      - plan.yaml parses and `stages` (if present) cover every step exactly once.
      - state.json loads.
      - select_next_action returns a sensible kind.
      - The Codex sub-agent prompt references all 4 default-read files.
    """
    from steward.phases.plan_phases import (
        APPROVAL_MARKER_RE,
        _build_codex_prompt,
        compute_stage_progress_banner,
        load_goal_md,
        load_plan,
        load_state_json,
        select_next_action,
    )
    from steward.plan_runner import PlanRunner

    td = Path(task_dir).resolve()
    g = load_goal_md(td)
    p = load_plan(td)
    s = load_state_json(td / "state", p)
    assert g["orchestrator"].get("email_prefix"), "goal.md missing email_prefix"
    assert g["experiment_config"].get("experiment_repo"), \
        "goal.md missing experiment_repo"

    if p.get("stages"):
        stage_sids = sorted(sid for st in p["stages"] for sid in st["steps"])
        plan_sids = sorted(stp["id"] for stp in p["steps"])
        assert stage_sids == plan_sids, \
            f"stages do not cover steps exactly once: stages={stage_sids} plan={plan_sids}"

    action = select_next_action(p, s, deepening_queue=p.get("deepening_added_by_runner"))
    assert action["kind"] in (
        "plan_step", "awaiting_stage_approval", "all_done",
        "stuck", "intent_pending", "deepening",
    ), action

    banner = compute_stage_progress_banner(p, s, action)
    assert banner.startswith("**Stage progress."), banner

    prompt = _build_codex_prompt(td, Path(g["experiment_config"]["experiment_repo"]))
    for f in ("goal.md", "current_step.md", "inbox.md", "recap.md"):
        assert f in prompt, f"prompt missing reference to {f}"

    # Approval markers parse
    for m in ("/approve setup", "/approve next", "/approve v3"):
        assert APPROVAL_MARKER_RE.search(m) is not None, m

    # PlanRunner instantiates cleanly
    PlanRunner(
        task_dir=td,
        experiment_repo=g["experiment_config"]["experiment_repo"],
        email_prefix=g["orchestrator"]["email_prefix"],
    )
    print(f"OK: {td.name} is ready for first cycle")
    print(f"    next action: {action.get('kind')} "
          f"{action.get('step', {}).get('id') or action.get('stage_id') or ''}".rstrip())
    print(f"    banner: {banner}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="steward-plan-runner",
        description=textwrap.dedent(__doc__ or "").strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Scaffold a new plan_runner task.")
    p_init.add_argument("task_id")
    p_init.add_argument(
        "--instance-dir", default=".",
        help="Instance root. Default: current working directory.",
    )
    p_init.add_argument(
        "--experiment-repo", required=True,
        help="Absolute path where step dispatch runs.",
    )
    p_init.add_argument(
        "--email-prefix", required=True,
        help='Subject prefix, e.g. "[MY-RESEARCH]"',
    )
    p_init.add_argument(
        "--schedule", default="hourly", choices=("hourly", "daily"),
        help="Cron cadence. Default: hourly.",
    )
    p_init.add_argument("--goal-file", type=Path, default=None)
    p_init.add_argument("--plan-file", type=Path, default=None)
    p_init.add_argument("--dry-run", action="store_true")
    p_init.add_argument("--force", action="store_true")

    p_verify = sub.add_parser(
        "verify", help="Run static verification against an existing task dir.",
    )
    p_verify.add_argument("task_dir", type=Path)

    args = parser.parse_args(argv)

    if args.cmd == "init":
        try:
            init_task(
                task_id=args.task_id,
                instance_dir=Path(args.instance_dir),
                experiment_repo=args.experiment_repo,
                email_prefix=args.email_prefix,
                schedule=args.schedule,
                goal_file=args.goal_file,
                plan_file=args.plan_file,
                dry_run=args.dry_run,
                force=args.force,
            )
        except (FileNotFoundError, FileExistsError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    elif args.cmd == "verify":
        try:
            verify_task(args.task_dir)
        except AssertionError as e:
            print(f"verify failed: {e}", file=sys.stderr)
            return 1
    return 0


# Enable `python -m steward.plan_runner.scaffolder ...`
if __name__ == "__main__":
    sys.exit(_cli_main())
