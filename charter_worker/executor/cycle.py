"""Executor cycle — the 5-phase loop for experiment execution.

Mirrors charter_worker.proactive's 5-phase cycle but with experiment-specific
actions. The key difference: phases 2 and 3 delegate to CLI agent sessions
rather than making single LLM calls.

Phase 1 — Context:   Load experiment state, check email replies, apply feedback
Phase 2 — Execute:   Delegate to CLI agent (run step, fix failure, continue plan)
Phase 3 — Analyze:   Delegate to CLI agent (validate outputs, write analysis)
Phase 4 — Report:    Consolidated daily email (once per day, not per failure)
Phase 5 — Plan Next: Delegate to CLI agent (propose ablations, next experiments)

Phases 2, 3, 5 each launch a SEPARATE CLI agent session. This gives each
session a clean context window and full workspace access.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from .agent import run_agent_session


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path: Path, data: dict):
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    tmp.rename(path)


def _get_step(status: dict, n: int) -> dict:
    steps = status.get("steps", {})
    return steps.get(n) or steps.get(str(n)) or {}


def _append_journal(exp_dir: Path, entry: dict):
    entry["timestamp"] = datetime.now().isoformat()
    with open(exp_dir / "journal.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Phase 1 — Context
# ---------------------------------------------------------------------------

def phase_context(exp_dir: Path, status: dict, config: dict) -> dict:
    """Load experiment state and determine what to do this cycle.

    Returns context dict with: status, next_action, experiment_dir, etc.
    """
    state = status.get("state", "planned")
    total_steps = status.get("total_steps", 0)

    # Find where we are
    current_step = 0
    next_pending = None
    has_failed = False
    needs_human = False

    for i in range(1, total_steps + 1):
        s = _get_step(status, i)
        if s.get("state") == "completed":
            current_step = i
        elif s.get("state") == "failed":
            has_failed = True
            if s.get("auto_retries", 0) >= config.get("max_auto_retries", 3):
                needs_human = True
            elif next_pending is None:
                next_pending = i
        elif s.get("state") in ("pending", None) and next_pending is None:
            next_pending = i

    # Determine action for this cycle
    if state in ("archived", "validated"):
        next_action = "done"
    elif state == "needs_human":
        next_action = "wait"
    elif needs_human:
        next_action = "wait"
        status["state"] = "needs_human"
    elif has_failed and next_pending:
        next_action = "fix_and_run"
    elif next_pending:
        next_action = "run_step"
    elif current_step == total_steps:
        next_action = "validate"
    else:
        next_action = "wait"

    return {
        "status": status,
        "state": state,
        "next_action": next_action,
        "current_step": current_step,
        "next_step": next_pending,
        "total_steps": total_steps,
        "has_failed": has_failed,
        "needs_human": needs_human,
        "experiment_dir": exp_dir,
    }


# ---------------------------------------------------------------------------
# Phase 2 — Execute (delegates to CLI agent)
# ---------------------------------------------------------------------------

def phase_execute(ctx: dict, config: dict) -> dict:
    """Run the next step or fix a failed step.

    Launches a CLI agent session that:
    - Reads the experiment plan
    - Checks current status and logs
    - Executes the next step OR diagnoses and fixes a failure
    - Updates the plan checklist
    """
    exp_dir = ctx["experiment_dir"]
    status = ctx["status"]
    next_step = ctx["next_step"]
    action = ctx["next_action"]

    if action == "run_step":
        step = _get_step(status, next_step)
        task_prompt = f"""You are a research experiment executor.

WORKSPACE: {exp_dir.parent.parent}
EXPERIMENT: {exp_dir.name}

Read the experiment plan at: experiments/{exp_dir.name}/plan.md
Current step to execute: Step {next_step} — "{step.get('name', '')}"
Script to run: experiments/{exp_dir.name}/{step.get('script', '')}

Instructions:
1. Read the plan to understand context.
2. Run the bash script: bash experiments/{exp_dir.name}/{step.get('script', '')}
3. Check that it completes without errors.
4. If it succeeds, mark step {next_step} as done in your result.
5. If it fails, read the error log and try to fix the issue (edit the script
   or code), then re-run. You have up to 3 attempts.
6. Report what happened."""

    elif action == "fix_and_run":
        step = _get_step(status, next_step)
        last_error = step.get("last_error", "unknown error")
        retries = step.get("auto_retries", 0)
        task_prompt = f"""You are a research experiment debugger.

WORKSPACE: {exp_dir.parent.parent}
EXPERIMENT: {exp_dir.name}

Step {next_step} ("{step.get('name', '')}") FAILED on the previous attempt.
This is auto-fix attempt {retries + 1}/3.

Previous error:
{last_error}

Instructions:
1. Read the experiment plan: experiments/{exp_dir.name}/plan.md
2. Read the failed script: experiments/{exp_dir.name}/{step.get('script', '')}
3. Read the error log in experiments/{exp_dir.name}/logs/
4. Diagnose the root cause.
5. Fix the issue — edit the script or code as needed.
6. Re-run the fixed script.
7. Report what you diagnosed and fixed."""
    else:
        return {"status": "skipped", "reason": f"action={action}"}

    # Launch agent session
    result = run_agent_session(
        workspace=exp_dir.parent.parent,  # workspace root
        task_prompt=task_prompt,
        agent=config.get("agent", "auto"),
        timeout=config.get("max_wall_clock_per_step", 1800),
    )

    # Update status based on agent result
    agent_status = result.get("result", {}).get("status", "unknown")
    step = _get_step(status, next_step)

    if agent_status == "success":
        step["state"] = "completed"
        step["completed_at"] = datetime.now().isoformat()
        step["auto_retries"] = 0
        _append_journal(exp_dir, {
            "event": "step_completed", "step": next_step,
            "agent": config.get("agent", "auto"),
            "summary": result.get("result", {}).get("summary", ""),
        })
    elif agent_status == "failed":
        step["state"] = "failed"
        step["auto_retries"] = step.get("auto_retries", 0) + 1
        step["last_error"] = result.get("result", {}).get("summary", "Agent failed")
        _append_journal(exp_dir, {
            "event": "step_failed", "step": next_step,
            "attempt": step["auto_retries"],
            "summary": result.get("result", {}).get("summary", ""),
            "errors": result.get("result", {}).get("errors", []),
        })
    else:
        step["state"] = "failed"
        step["last_error"] = f"Agent returned: {agent_status}"

    _save_yaml(exp_dir / "status.yaml", status)
    return result


# ---------------------------------------------------------------------------
# Phase 3 — Analyze (delegates to CLI agent)
# ---------------------------------------------------------------------------

def phase_analyze(ctx: dict, config: dict) -> dict:
    """Validate experiment outputs and write analysis.

    Launches a CLI agent session that:
    - Reads the plan's success criteria
    - Checks all output files
    - Validates metrics against thresholds
    - Writes analysis.md
    """
    exp_dir = ctx["experiment_dir"]
    status = ctx["status"]

    task_prompt = f"""You are a research experiment validator.

WORKSPACE: {exp_dir.parent.parent}
EXPERIMENT: {exp_dir.name}

All steps have completed. Now validate the results.

Instructions:
1. Read the experiment plan: experiments/{exp_dir.name}/plan.md
2. Check the success criteria listed in the plan.
3. Read the output files in experiments/{exp_dir.name}/outputs/
4. For each criterion, check if it passes or fails. Show the actual values.
5. Write a brief analysis to experiments/{exp_dir.name}/analysis.md
6. In your result JSON:
   - Set status to "success" if ALL criteria pass
   - Set status to "failed" if any criterion fails
   - Include the metrics you checked in the "metrics" field
   - If failed, suggest what to change in "next_suggestion"
"""

    result = run_agent_session(
        workspace=exp_dir.parent.parent,
        task_prompt=task_prompt,
        agent=config.get("agent", "auto"),
        timeout=600,
    )

    agent_status = result.get("result", {}).get("status", "unknown")

    if agent_status == "success":
        status["state"] = "validated"
        status["completed_at"] = datetime.now().isoformat()
        _append_journal(exp_dir, {
            "event": "validated",
            "metrics": result.get("result", {}).get("metrics"),
        })
    elif agent_status == "failed":
        # Validation failed — check if we can auto-retry
        val_retries = status.get("validation_retries", 0)
        if val_retries < config.get("max_auto_retries", 3):
            status["validation_retries"] = val_retries + 1
            status["state"] = "executing"
            # Reset the step most likely responsible
            suggestion = result.get("result", {}).get("next_suggestion", "")
            _append_journal(exp_dir, {
                "event": "validation_failed",
                "attempt": val_retries + 1,
                "suggestion": suggestion,
                "metrics": result.get("result", {}).get("metrics"),
            })
            # The next monitor cycle will invoke phase_fix
        else:
            status["state"] = "needs_human"
            _append_journal(exp_dir, {
                "event": "validation_exhausted",
                "metrics": result.get("result", {}).get("metrics"),
            })

    _save_yaml(exp_dir / "status.yaml", status)
    return result


# ---------------------------------------------------------------------------
# Phase 4 — Report (daily email, mirrors proactive/phase_feedback.py)
# ---------------------------------------------------------------------------

def phase_report(experiments: list[dict], runner_state: dict, config: dict):
    """Send one consolidated daily email covering all experiments.

    Reuses charter_worker email infrastructure.
    """
    # Build report
    lines = ["# Experiment Runner — Daily Report", ""]
    lines.append("| Experiment | Status | Progress | Notes |")
    lines.append("|-----------|--------|----------|-------|")

    needs_input = []
    for exp in experiments:
        eid = exp["experiment_id"]
        st = exp.get("status", "?")
        steps = f"{exp.get('steps_completed', 0)}/{exp.get('total_steps', 0)}"
        notes = exp.get("notes", "")
        lines.append(f"| {eid} | {st} | {steps} | {notes} |")
        if st == "needs_human":
            needs_input.append(exp)

    lines.append("")

    if needs_input:
        lines.append("## Needs Your Input")
        for exp in needs_input:
            eid = exp["experiment_id"]
            lines.append(f"\n### {eid}")
            lines.append(f"What I tried: {exp.get('auto_fix_summary', 'see journal')}")
            lines.append(f"Why I'm stuck: {exp.get('stuck_reason', 'exhausted auto-retries')}")
            lines.append(f"Suggestion: {exp.get('suggestion', 'check the logs')}")
        lines.append("")
        lines.append("Reply with instructions (e.g., 'for 002: adjust lr to 0.0005 and retry from step 2')")

    completed = [e for e in experiments if e["status"] == "validated"]
    if completed:
        lines.append("\n## Completed")
        for e in completed:
            lines.append(f"- **{e['experiment_id']}**: {e.get('notes', 'validated')}")

    running = [e for e in experiments if e["status"] == "executing"]
    if running:
        lines.append("\n## Running Autonomously")
        for e in running:
            lines.append(f"- **{e['experiment_id']}**: step {e.get('steps_completed', 0)}/{e.get('total_steps', 0)}")

    lines.append("\n---")
    lines.append("I'll keep working autonomously. Reply only if you want to redirect something.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 5 — Plan Next (delegates to CLI agent)
# ---------------------------------------------------------------------------

def phase_plan_next(exp_dir: Path, status: dict, config: dict) -> dict:
    """After validation, propose next experiments or ablations.

    Launches a CLI agent that:
    - Reviews the completed experiment's results
    - Reads the broader project context (paper, other experiments)
    - Proposes 1-2 follow-up experiments or ablations
    """
    task_prompt = f"""You are a research experiment planner.

WORKSPACE: {exp_dir.parent.parent}
COMPLETED EXPERIMENT: {exp_dir.name}

The experiment completed and was validated. Now plan what to do next.

Instructions:
1. Read the completed experiment's plan and analysis:
   experiments/{exp_dir.name}/plan.md
   experiments/{exp_dir.name}/analysis.md
2. Read other experiments in experiments/ to understand the broader context.
3. If there's a paper/ directory, read it to understand the research goals.
4. Propose 1-2 follow-up experiments:
   - Ablation studies (change one variable to understand its effect)
   - Extensions (next logical step in the research plan)
   - Improvements (address weaknesses found in the analysis)
5. For each proposal, create a new experiment directory with plan.md.
   Use the next sequential number (e.g., if 003 exists, create 004).
6. Include bash scripts for each step in the plan.
7. Add the new experiments to experiments/experiment_registry.yaml
   with approved: false (user must approve before running).
"""

    result = run_agent_session(
        workspace=exp_dir.parent.parent,
        task_prompt=task_prompt,
        agent=config.get("agent", "auto"),
        timeout=900,
    )

    _append_journal(exp_dir, {
        "event": "plan_next_proposed",
        "summary": result.get("result", {}).get("summary", ""),
        "new_experiments": result.get("result", {}).get("files_modified", []),
    })

    return result


# ---------------------------------------------------------------------------
# Full cycle — called by the task's run.py
# ---------------------------------------------------------------------------

def run_monitor_cycle(workspace: Path, experiment_id: str, config: dict) -> dict:
    """One autonomous monitoring cycle for an experiment.

    Called hourly by cron. Advances the experiment through phases 1-3, 5.
    Phase 4 (email) is NOT called here — that's report mode only.
    """
    exp_dir = workspace / "experiments" / experiment_id
    status_path = exp_dir / "status.yaml"
    status = _load_yaml(status_path)

    if not status:
        return {"experiment_id": experiment_id, "status": "error", "notes": "no status.yaml"}

    # Phase 1: Context
    ctx = phase_context(exp_dir, status, config)
    action = ctx["next_action"]

    print(f"  [{experiment_id}] state={ctx['state']}, action={action}, "
          f"step={ctx['current_step']}/{ctx['total_steps']}", file=sys.stderr)

    if action == "done":
        return {"experiment_id": experiment_id, "status": ctx["state"]}

    if action == "wait":
        return {
            "experiment_id": experiment_id, "status": "needs_human",
            "steps_completed": ctx["current_step"],
            "total_steps": ctx["total_steps"],
            "stuck_reason": "exhausted auto-retries",
        }

    # Phase 2: Execute (run step or fix failure)
    if action in ("run_step", "fix_and_run"):
        exec_result = phase_execute(ctx, config)
        # Reload status after agent modified it
        status = _load_yaml(status_path)
        ctx = phase_context(exp_dir, status, config)

    # Phase 3: Analyze (if all steps completed)
    if ctx["next_action"] == "validate":
        analyze_result = phase_analyze(ctx, config)
        status = _load_yaml(status_path)
        ctx = phase_context(exp_dir, status, config)

    # Phase 5: Plan next (if validated)
    if status.get("state") == "validated":
        plan_result = phase_plan_next(exp_dir, status, config)

    # Build result summary
    result = {
        "experiment_id": experiment_id,
        "status": status.get("state", "unknown"),
        "steps_completed": sum(
            1 for i in range(1, ctx["total_steps"] + 1)
            if _get_step(status, i).get("state") == "completed"
        ),
        "total_steps": ctx["total_steps"],
    }

    return result
