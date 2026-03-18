#!/usr/bin/env python3
"""LTT task — single-project proactive research cycle.

Thin wrapper around charter_worker.proactive. Runs ONE cycle for the
project defined in this folder's definition.yaml.

This file is generic — copy it as-is for any new LTT project.
Only the folder contents (definition.yaml, state/, context_files/) differ.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

TASK_DIR = Path(__file__).resolve().parent
REPO_ROOT = TASK_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from charter_worker.proactive.phase_context import load_context
from charter_worker.proactive.phase_research import research
from charter_worker.proactive.phase_synthesize import synthesize
from charter_worker.proactive.phase_feedback import send_ltt_email
from charter_worker.proactive.phase_speculate import speculate
from charter_worker.proactive.guardrails import GuardrailResult

STATE_DIR = TASK_DIR / "state"
DEFINITION_FILE = TASK_DIR / "definition.yaml"
PROJECT_BUDGET_SECONDS = 900


def _load_yaml(path):
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _save_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    tmp.rename(path)


def _load_state():
    path = STATE_DIR / "task_state.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"cycle": 0, "days_since_reply": 0, "status": "active", "metrics_history": []}


def _save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "task_state.json", "w") as f:
        json.dump(state, f, indent=2)


def _append_log(entries):
    log_path = STATE_DIR / "exploration_log.jsonl"
    with open(log_path, "a") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main():
    started = time.time()
    date_str = datetime.now().strftime("%Y-%m-%d")
    task_id = TASK_DIR.name
    definition = _load_yaml(DEFINITION_FILE)
    project_id = definition.get("project_id", task_id)

    print(f"[{task_id}] Starting proactive cycle — {date_str}", file=sys.stderr)

    meta = _load_state()
    all_guardrails = []
    errors = []

    # --- Phase 1: Context ---
    print(f"\n--- Phase 1: Load Context ---", file=sys.stderr)
    try:
        context = load_context(project_id, STATE_DIR, meta)
        all_guardrails.extend(context.get("guardrail_results", []))

        if context["status"].get("paused"):
            print(f"  Paused by user feedback", file=sys.stderr)
            meta["status"] = "paused"
            _save_state(meta)
            return
    except Exception as e:
        print(f"  Phase 1 FAILED: {e}", file=sys.stderr)
        errors.append({"phase": "context", "error": str(e)})
        context = {
            "definition": definition,
            "status": _load_yaml(STATE_DIR / "status.yaml"),
            "exploration_log": [],
            "feedback": None,
            "promoted_findings": [],
            "days_since_reply": meta.get("days_since_reply", 0),
            "context_files_summary": "",
        }

    context["definition"] = definition
    budget_tier = "light" if context["status"].get("needs_human_input") else "full"

    # --- Phase 2: Research ---
    print(f"\n--- Phase 2: Research & Search ---", file=sys.stderr)
    raw_findings = []
    new_log_entries = []
    try:
        raw_findings, p2_guardrails, new_log_entries = research(
            context, budget_tier=budget_tier
        )
        all_guardrails.extend(p2_guardrails)
        print(f"  Found {len(raw_findings)} raw findings", file=sys.stderr)
    except Exception as e:
        print(f"  Phase 2 FAILED: {e}", file=sys.stderr)
        errors.append({"phase": "research", "error": str(e)})

    # --- Phase 3: Synthesize ---
    print(f"\n--- Phase 3: Synthesize & Reflect ---", file=sys.stderr)
    try:
        updated_status, p3_guardrails = synthesize(
            context, raw_findings, project_dir=STATE_DIR
        )
        all_guardrails.extend(p3_guardrails)
        context["status"] = updated_status
    except Exception as e:
        print(f"  Phase 3 FAILED: {e}", file=sys.stderr)
        errors.append({"phase": "synthesize", "error": str(e)})
        updated_status = context["status"]
        updated_status["cycle_number"] = meta.get("cycle", 0) + 1

    _save_yaml(STATE_DIR / "status.yaml", updated_status)
    if new_log_entries:
        _append_log(new_log_entries)

    cycle_metrics = updated_status.pop("_cycle_metrics", {})
    meta["cycle"] = updated_status.get("cycle_number", meta.get("cycle", 0) + 1)
    if cycle_metrics:
        meta.setdefault("metrics_history", []).append(cycle_metrics)
        meta["metrics_history"] = meta["metrics_history"][-10:]

    # --- Phase 4: Email ---
    print(f"\n--- Phase 4: Email ---", file=sys.stderr)
    email_result = {"status": "skipped"}
    try:
        projects_status = [{
            "project_id": project_id,
            "status": updated_status,
            "definition": definition,
            "cycle_summary": {"tldr": updated_status.get("current_hypothesis", "")[:100]},
            "days_since_reply": meta.get("days_since_reply", 0),
        }]
        email_result, email_guardrails = send_ltt_email(
            projects_status, meta, dry_run=False
        )
        all_guardrails.extend(email_guardrails)
        print(f"  Email: {email_result.get('status')}", file=sys.stderr)

        if email_result.get("message_id"):
            (STATE_DIR / "email_thread_id.txt").write_text(email_result["message_id"])
            meta["last_email_date"] = date_str
            meta["last_message_id"] = email_result["message_id"]
    except Exception as e:
        print(f"  Phase 4 FAILED: {e}", file=sys.stderr)
        errors.append({"phase": "email", "error": str(e)})

    # --- Phase 5: Speculate ---
    elapsed = time.time() - started
    print(f"\n--- Phase 5: Speculative Pre-computation ---", file=sys.stderr)
    try:
        buffer, p5_guardrails = speculate(
            context, STATE_DIR,
            total_budget_seconds=PROJECT_BUDGET_SECONDS,
            elapsed_seconds=elapsed,
        )
        all_guardrails.extend(p5_guardrails)
    except Exception as e:
        print(f"  Phase 5 FAILED: {e}", file=sys.stderr)
        errors.append({"phase": "speculate", "error": str(e)})

    _save_state(meta)

    # --- Write summary ---
    duration = time.time() - started
    summary_dir = REPO_ROOT / "daily_summaries" / date_str / "tasks" / task_id
    summary_dir.mkdir(parents=True, exist_ok=True)

    conf = updated_status.get("confidence_score", "?")
    needs = updated_status.get("needs_human_input", False)
    hyp = updated_status.get("current_hypothesis", "")[:150]

    summary_json = {
        "task_id": task_id,
        "date": date_str,
        "status": "success" if not errors else "partial",
        "tldr": [f"Cycle {meta['cycle']}, confidence {conf}/5. {hyp}"],
        "action_items": [],
        "errors": [{"type": e["phase"], "message": e["error"]} for e in errors],
        "metadata": {"duration_s": round(duration, 1), "budget_hint": "high"},
    }
    if needs:
        summary_json["action_items"].append(f"Reply to [{task_id}] email")

    with open(summary_dir / "summary.json", "w") as f:
        json.dump(summary_json, f, indent=2, ensure_ascii=False)

    lines = [f"# {task_id} — {date_str}", ""]
    lines.append(f"## TL;DR")
    lines.append(f"- Cycle {meta['cycle']}, confidence {conf}/5{'  **NEEDS INPUT**' if needs else ''}")
    lines.append(f"- {hyp}")
    lines.append("")
    lines.append(f"## Hypothesis")
    lines.append(updated_status.get("current_hypothesis", "N/A"))
    lines.append("")
    findings = updated_status.get("key_findings", [])
    if findings:
        lines.append("## Key Findings")
        for f_ in findings[:5]:
            lines.append(f"- {f_.get('finding', '')[:120]}")
            if f_.get("provenance"):
                lines.append(f"  Source: {f_['provenance']}")
        lines.append("")
    if errors:
        lines.append("## Errors")
        for e in errors:
            lines.append(f"- {e['phase']}: {e['error']}")
        lines.append("")
    lines.append(f"*Duration: {duration:.1f}s | Email: {email_result.get('status')}*")

    with open(summary_dir / "summary.md", "w") as f:
        f.write("\n".join(lines))

    print(f"\n[{task_id}] Complete. {duration:.1f}s, cycle {meta['cycle']}", file=sys.stderr)


if __name__ == "__main__":
    main()
