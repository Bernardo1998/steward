"""charter-promote — Workflow crystallizer.

Analyzes an agent-mode task's execution history to identify which steps
are deterministic (scriptable) vs. need LLM judgment, then generates
a candidate direct-mode run.py for review.

Usage:
    charter-promote <task_id> --last 5
    charter-promote <task_id> --last 10 --apply
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

from .llm import call_llm_json


# ---------------------------------------------------------------------------
# Step 1: Collect cycle data
# ---------------------------------------------------------------------------

def collect_promotion_data(
    instance_root: Path,
    task_id: str,
    last_n: int = 5,
) -> dict:
    """Gather the last N cycles of data for promotion analysis.

    Reads: charter.yaml, task.md, cycle logs, prompts, summaries,
    and orchestrator retry history.
    """
    task_dir = None
    # Find task path from registry
    registry_file = instance_root / "tasks" / "registry.yaml"
    if registry_file.exists():
        with open(registry_file) as f:
            registry = yaml.safe_load(f) or {}
        for t in registry.get("tasks", []):
            if t.get("id") == task_id:
                task_dir = instance_root / t["path"]
                break

    if not task_dir:
        task_dir = instance_root / "tasks" / task_id

    if not task_dir.exists():
        raise FileNotFoundError(f"Task directory not found: {task_dir}")

    # Charter
    charter_path = task_dir / "charter.yaml"
    charter_text = charter_path.read_text() if charter_path.exists() else ""
    charter = yaml.safe_load(charter_text) if charter_text else {}

    # Task instructions
    task_md_path = task_dir / "task.md"
    task_md = task_md_path.read_text() if task_md_path.exists() else ""

    # Cycle logs (most recent N)
    logs_dir = task_dir / "logs"
    cycle_logs = []
    if logs_dir.exists():
        log_files = sorted(logs_dir.glob("cycle_*.log"), reverse=True)[:last_n]
        for lf in log_files:
            content = lf.read_text(errors="replace")
            # Cap per-log to avoid prompt overflow
            if len(content) > 8000:
                content = content[:4000] + "\n\n[...truncated...]\n\n" + content[-4000:]
            cycle_logs.append({"date": lf.stem.replace("cycle_", ""), "content": content})

    # Prompts
    prompts = []
    if logs_dir.exists():
        prompt_files = sorted(logs_dir.glob("prompt_*.txt"), reverse=True)[:last_n]
        for pf in prompt_files:
            prompts.append({"date": pf.stem.replace("prompt_", ""), "content": pf.read_text(errors="replace")[:3000]})

    # Summaries from daily_summaries
    summaries = []
    summaries_root = instance_root / "daily_summaries"
    if summaries_root.exists():
        # Scan recent dates
        today = datetime.now()
        for days_ago in range(30):
            d = today - timedelta(days=days_ago)
            ds = d.strftime("%Y-%m-%d")
            sj = summaries_root / ds / "tasks" / task_id / "summary.json"
            if sj.exists():
                try:
                    with open(sj) as f:
                        data = json.load(f)
                    summaries.append(data)
                except (json.JSONDecodeError, OSError):
                    pass
            if len(summaries) >= last_n:
                break

    # Orchestrator state (retry history)
    orch_state = {}
    orch_file = instance_root / "orchestrator_state.json"
    if orch_file.exists():
        try:
            with open(orch_file) as f:
                orch_state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    task_runs = orch_state.get("task_runs", {}).get(task_id, {})

    return {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "charter": charter,
        "charter_text": charter_text,
        "task_md": task_md[:6000],
        "cycle_logs": cycle_logs,
        "prompts": prompts,
        "summaries": summaries,
        "retry_history": {
            "last_success_date": task_runs.get("last_success_date", ""),
            "retry_count": task_runs.get("retry_count", 0),
        },
        "current_mode": charter.get("execution", {}).get("agent", "unknown"),
    }


# ---------------------------------------------------------------------------
# Step 2: Analyze stability (1 LLM call)
# ---------------------------------------------------------------------------

def analyze_promotion_readiness(data: dict) -> dict:
    """Analyze whether the task is ready for promotion from agent to direct mode.

    Returns structured readiness assessment.
    """
    # Build a condensed view of cycle behavior
    log_summaries = []
    for log in data["cycle_logs"][:5]:
        log_summaries.append(f"--- Cycle {log['date']} ---\n{log['content'][:3000]}")
    logs_text = "\n\n".join(log_summaries) if log_summaries else "(no cycle logs available)"

    summary_text = json.dumps(data["summaries"][:5], indent=2)[:4000] if data["summaries"] else "(no summaries)"

    prompt = f"""You are analyzing an agent-mode task to determine if it can be promoted
to a cheaper direct-mode (scripted) workflow.

TASK: {data['task_id']}
CURRENT MODE: {data['current_mode']}

TASK INSTRUCTIONS (task.md):
{data['task_md'][:4000]}

RECENT CYCLE LOGS (what the agent actually did):
{logs_text}

RECENT SUMMARIES:
{summary_text}

RETRY HISTORY:
  Last success: {data['retry_history']['last_success_date']}
  Recent retry count: {data['retry_history']['retry_count']}

Analyze:
1. What steps does the agent repeat identically (or near-identically) every cycle?
   These are candidates for scripting.
2. What steps vary and require LLM judgment (creative decisions, ambiguous input)?
   These should remain as call_llm_json() calls.
3. What are the inputs, outputs, and dependencies?
4. What are likely failure points?
5. Is this task a CYCLE (plan→act→reflect, fits CycleRunner) or a PIPELINE
   (fixed sequential steps, needs standalone run.py)?
6. Overall: ready for promotion, partially ready, or not ready?

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "readiness": "ready|partial|not_ready",
  "task_pattern": "cycle|pipeline",
  "deterministic_steps": [
    {{"step": "description", "can_script": true, "how": "brief implementation hint"}}
  ],
  "llm_required_steps": [
    {{"step": "description", "why": "why LLM is needed"}}
  ],
  "dependencies": ["list of external deps: APIs, files, tools"],
  "failure_risks": ["likely failure points"],
  "recommended_mode": "direct|hybrid",
  "estimated_cost_reduction": "percentage as string, e.g. '80%'",
  "estimated_attention_reduction": "description of reduced human attention needed",
  "rationale": "2-3 sentence explanation of the recommendation"
}}"""

    return call_llm_json(prompt, timeout=300)


# ---------------------------------------------------------------------------
# Step 3: Generate candidate workflow (1 LLM call)
# ---------------------------------------------------------------------------

def generate_promoted_workflow(data: dict, analysis: dict) -> dict:
    """Generate a candidate run.py and updated charter.yaml.

    Returns dict with run_py_code, charter_yaml, report_md.
    """
    readiness = analysis.get("readiness", "not_ready")
    if readiness == "not_ready":
        report = _build_not_ready_report(data, analysis)
        return {"report_md": report, "run_py_code": None, "charter_yaml": None}

    task_pattern = analysis.get("task_pattern", "pipeline")
    deterministic = json.dumps(analysis.get("deterministic_steps", []), indent=2)
    llm_steps = json.dumps(analysis.get("llm_required_steps", []), indent=2)

    if task_pattern == "cycle":
        target_desc = """Generate a CycleRunner subclass. Import from charter_worker.runner:

from charter_worker.runner import CycleRunner
from charter_worker.actions import Action, ActionResult

class MyTask(CycleRunner):
    def plan(self, context):
        # Return list of Action objects
        return [Action("lightweight_search", query="...")]

    # Override other phases only if needed

if __name__ == "__main__":
    MyTask(definition="definition.yaml", state_dir="state/").run_cycle()
"""
    else:
        target_desc = """Generate a standalone run.py script. Use call_llm_json() for
steps that need LLM judgment:

from charter_worker.llm import call_llm_json

def main():
    # Scripted steps...
    result = call_llm_json("prompt...", timeout=120)  # LLM step
    # More scripted steps...
    write_summary()
"""

    prompt = f"""Generate a Python script that replaces an agent-mode task with a
direct-mode workflow.

TASK: {data['task_id']}

ORIGINAL INSTRUCTIONS (task.md):
{data['task_md'][:3000]}

ANALYSIS:
Deterministic steps (script these):
{deterministic}

LLM-required steps (use call_llm_json for these):
{llm_steps}

Dependencies: {json.dumps(analysis.get('dependencies', []))}

TARGET FORMAT:
{target_desc}

REQUIREMENTS:
- The script must write summary.json and summary.md to the summary directory
  (read from CHARTER_SUMMARY_DIR env var or construct from CHARTER_INSTANCE_ROOT)
- summary.json must have: task_id, date, status, tldr, action_items, artifacts, errors, metadata
- Handle errors gracefully — catch exceptions per step, continue, report in summary
- Use call_llm_json() from charter_worker.llm for any step needing LLM
- Keep it concise — under 200 lines

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "run_py_code": "full Python source code as a string",
  "charter_yaml": "updated charter.yaml content as a string",
  "notes": "any important notes about the generated code"
}}"""

    try:
        result = call_llm_json(prompt, timeout=600)
    except Exception as e:
        print(f"  Workflow generation failed: {e}", file=sys.stderr)
        print(f"  Producing report without generated code.", file=sys.stderr)
        result = {"run_py_code": None, "charter_yaml": None, "notes": f"Generation failed: {e}"}

    # Build promotion report
    report = _build_promotion_report(data, analysis, result)
    result["report_md"] = report

    return result


def _build_not_ready_report(data: dict, analysis: dict) -> str:
    """Build a report explaining why the task isn't ready for promotion."""
    lines = [
        f"# Promotion Report: {data['task_id']}",
        f"\n**Date**: {datetime.now().strftime('%Y-%m-%d')}",
        f"**Current mode**: {data['current_mode']}",
        f"**Readiness**: NOT READY",
        "",
        "## Why Not Ready",
        "",
        analysis.get("rationale", "No rationale provided."),
        "",
        "## LLM-Required Steps (still too variable)",
        "",
    ]
    for step in analysis.get("llm_required_steps", []):
        lines.append(f"- **{step.get('step', '?')}**: {step.get('why', '?')}")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append("Keep running in agent mode. Re-run `charter-promote` after "
                 "more cycles to check if behavior has stabilized.")
    return "\n".join(lines)


def _build_promotion_report(data: dict, analysis: dict, workflow: dict) -> str:
    """Build the full promotion report."""
    lines = [
        f"# Promotion Report: {data['task_id']}",
        f"\n**Date**: {datetime.now().strftime('%Y-%m-%d')}",
        f"**Current mode**: {data['current_mode']}",
        f"**Readiness**: {analysis.get('readiness', '?').upper()}",
        f"**Recommended mode**: {analysis.get('recommended_mode', '?')}",
        f"**Pattern**: {analysis.get('task_pattern', '?')}",
        f"**Estimated cost reduction**: {analysis.get('estimated_cost_reduction', '?')}",
        f"**Estimated attention reduction**: {analysis.get('estimated_attention_reduction', '?')}",
        "",
        "## Deterministic Steps (scripted)",
        "",
    ]
    for step in analysis.get("deterministic_steps", []):
        lines.append(f"- {step.get('step', '?')}")
        if step.get("how"):
            lines.append(f"  *How*: {step['how']}")

    lines.extend(["", "## LLM-Required Steps (kept as call_llm_json)", ""])
    for step in analysis.get("llm_required_steps", []):
        lines.append(f"- {step.get('step', '?')}: {step.get('why', '?')}")

    lines.extend(["", "## Dependencies", ""])
    for dep in analysis.get("dependencies", []):
        lines.append(f"- {dep}")

    lines.extend(["", "## Failure Risks", ""])
    for risk in analysis.get("failure_risks", []):
        lines.append(f"- {risk}")

    lines.extend(["", "## Rationale", "", analysis.get("rationale", "")])

    if workflow.get("notes"):
        lines.extend(["", "## Notes", "", workflow["notes"]])

    lines.extend([
        "",
        "## Generated Files",
        "",
        "- `run.py.generated` — candidate workflow script",
        "- `charter.promoted.yaml` — updated charter (agent: direct)",
        "",
        "## To Apply",
        "",
        "Review the generated files, then:",
        "```bash",
        f"charter-promote {data['task_id']} --apply",
        "```",
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 4: Write output files
# ---------------------------------------------------------------------------

def write_promotion_artifacts(task_dir: Path, workflow: dict):
    """Write promotion artifacts to the task directory.

    Does NOT overwrite existing files (except promotion_report.md which
    is expected to be regenerated).
    """
    task_dir = Path(task_dir)

    # Always write/overwrite the report
    (task_dir / "promotion_report.md").write_text(workflow["report_md"])

    # Write generated run.py (don't overwrite if exists)
    if workflow.get("run_py_code"):
        gen_path = task_dir / "run.py.generated"
        gen_path.write_text(workflow["run_py_code"])

    # Write promoted charter (don't overwrite if exists)
    if workflow.get("charter_yaml"):
        charter_path = task_dir / "charter.promoted.yaml"
        charter_path.write_text(workflow["charter_yaml"])


# ---------------------------------------------------------------------------
# Step 5: Apply promotion
# ---------------------------------------------------------------------------

def apply_promotion(task_dir: Path):
    """Apply a generated promotion: backup originals and swap files.

    Requires run.py.generated and charter.promoted.yaml to exist.
    """
    task_dir = Path(task_dir)

    gen_run = task_dir / "run.py.generated"
    gen_charter = task_dir / "charter.promoted.yaml"

    if not gen_charter.exists():
        raise FileNotFoundError(f"No charter.promoted.yaml found in {task_dir}")

    # Backup originals
    charter_orig = task_dir / "charter.yaml"
    if charter_orig.exists():
        shutil.copy2(charter_orig, task_dir / "charter.yaml.pre-promote")

    run_orig = task_dir / "run.py"
    if run_orig.exists():
        shutil.copy2(run_orig, task_dir / "run.py.pre-promote")

    # Swap
    shutil.copy2(gen_charter, charter_orig)
    if gen_run.exists():
        shutil.copy2(gen_run, run_orig)

    # Clean up generated files
    gen_charter.unlink()
    if gen_run.exists():
        gen_run.unlink()

    print(f"Promotion applied to {task_dir.name}.", file=sys.stderr)
    print(f"  Backups: charter.yaml.pre-promote, run.py.pre-promote", file=sys.stderr)
    print(f"  To revert: copy .pre-promote files back.", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Promote a task from agent mode to direct mode"
    )
    parser.add_argument("task_id", help="Task to analyze for promotion")
    parser.add_argument("--last", type=int, default=5,
                        help="Number of recent cycles to analyze (default: 5)")
    parser.add_argument("--apply", action="store_true",
                        help="Apply promotion (backup + swap files)")
    parser.add_argument("--instance-dir", default=None,
                        help="Instance root directory")
    args = parser.parse_args()

    instance_root = Path(args.instance_dir) if args.instance_dir else Path(
        os.environ.get("CHARTER_INSTANCE_ROOT", ".")
    )

    if args.apply:
        # Find task dir
        task_dir = instance_root / "tasks" / args.task_id
        if not task_dir.exists():
            print(f"Error: task directory not found: {task_dir}", file=sys.stderr)
            sys.exit(1)
        apply_promotion(task_dir)
        return

    print(f"Collecting data for {args.task_id} (last {args.last} cycles)...",
          file=sys.stderr)
    data = collect_promotion_data(instance_root, args.task_id, last_n=args.last)

    print(f"Analyzing promotion readiness...", file=sys.stderr)
    analysis = analyze_promotion_readiness(data)
    readiness = analysis.get("readiness", "unknown")
    print(f"  Readiness: {readiness}", file=sys.stderr)
    print(f"  Pattern: {analysis.get('task_pattern', '?')}", file=sys.stderr)
    print(f"  Recommended: {analysis.get('recommended_mode', '?')}", file=sys.stderr)

    print(f"Generating promoted workflow...", file=sys.stderr)
    workflow = generate_promoted_workflow(data, analysis)

    task_dir = Path(data["task_dir"])
    write_promotion_artifacts(task_dir, workflow)
    print(f"\nPromotion artifacts written to {task_dir}/", file=sys.stderr)
    print(f"  promotion_report.md   — review this first", file=sys.stderr)
    if workflow.get("run_py_code"):
        print(f"  run.py.generated      — candidate workflow", file=sys.stderr)
    if workflow.get("charter_yaml"):
        print(f"  charter.promoted.yaml — updated charter", file=sys.stderr)
    print(f"\nTo apply: charter-promote {args.task_id} --apply", file=sys.stderr)


if __name__ == "__main__":
    main()
