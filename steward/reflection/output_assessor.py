"""Output-Based Quality Assessment.

Replaces the structured-signal approach (status/errors/days_failing) with a
single LLM call that reads the same task summaries the user reads and judges
whether each task produced meaningful new output today.

The ONLY question: "Did this task deliver meaningful new content?"
If not, it's a failure — no threshold, no waiting period.
"""

import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class OutputAssessment:
    task_id: str
    has_meaningful_output: bool
    confidence: str  # high / medium / low
    evidence: str
    is_stale: bool
    apparent_issue: Optional[str]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _read_text(path: Path, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text()
        return text[:max_chars] if len(text) > max_chars else text
    except OSError:
        return ""


def _date_minus(date_str: str, days: int) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=days)
    return dt.strftime("%Y-%m-%d")


def _load_task_summaries(
    instance_root: Path, date_str: str, lookback: int = 1,
) -> dict[str, dict[str, str]]:
    """Load summary.md for each task for today and the previous `lookback` days.

    Returns {task_id: {"today": text, "yesterday": text, ...}}
    """
    registry = _load_yaml(instance_root / "tasks" / "registry.yaml")
    tasks = [t for t in registry.get("tasks", []) if t.get("enabled", True)]
    summaries_root = instance_root / "daily_summaries"

    dates = [date_str] + [_date_minus(date_str, d) for d in range(1, lookback + 1)]
    labels = ["today", "yesterday", "day_before"][:len(dates)]

    result = {}
    for task in tasks:
        task_id = task["id"]
        task_summaries = {}
        for label, d in zip(labels, dates):
            path = summaries_root / d / "tasks" / task_id / "summary.md"
            task_summaries[label] = _read_text(path)
        # Check for override
        override_path = instance_root / task["path"] / "state" / "reflector_override.json"
        if override_path.exists():
            try:
                with open(override_path) as f:
                    override = json.load(f)
                if override.get("healthy_until", "") >= date_str:
                    task_summaries["_override"] = override.get("reason", "manual override")
            except (json.JSONDecodeError, OSError):
                pass
        result[task_id] = task_summaries

    return result


def _build_prompt(task_summaries: dict[str, dict[str, str]]) -> str:
    sections = []
    for task_id, summaries in task_summaries.items():
        if "_override" in summaries:
            continue  # skip overridden tasks
        section = f"=== TASK: {task_id} ===\n"
        if summaries.get("today"):
            section += f"--- TODAY ---\n{summaries['today']}\n"
        else:
            section += "--- TODAY ---\n(no summary produced)\n"
        if summaries.get("yesterday"):
            section += f"--- YESTERDAY ---\n{summaries['yesterday']}\n"
        sections.append(section)

    return f"""\
You are evaluating whether automated tasks produced meaningful NEW output today.

For each task below, you see today's summary and yesterday's summary.
Judge each task on ONE question: did it deliver meaningful new content today?

Meaningful output means: new findings, new data, new artifacts, new action items,
or substantive progress. Calendar events created, emails sent, study cards produced,
papers read — these all count as meaningful if they represent today's work.

NOT meaningful: recycled/identical content from prior days, boilerplate-only summaries,
"no updates" or "skipped" messages, summaries that describe running but show no results,
stale findings with no new entries.

STALENESS CHECK: If today's key findings, artifacts, or action items are substantially
the same TEXT as yesterday's, the task is stale — it ran but produced nothing new.
Small wording changes or reordering don't count as new.

MISSING OUTPUT: If no summary was produced at all, that is always a failure.

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
```json
{{
  "assessments": [
    {{
      "task_id": "string",
      "has_meaningful_output": true,
      "confidence": "high",
      "evidence": "Produced 3 new deep-read paper notes and updated vault",
      "is_stale": false,
      "apparent_issue": null
    }}
  ]
}}
```

For tasks WITHOUT meaningful output, set apparent_issue to a one-sentence
diagnosis of what appears wrong (e.g. "research queries timing out",
"email bootstrap loop", "no new materials to process").

TASK SUMMARIES:

{chr(10).join(sections)}"""


def assess_output_quality(
    instance_root: Path,
    date_str: Optional[str] = None,
    lookback: int = 1,
) -> tuple[list[OutputAssessment], list[dict]]:
    """Assess whether each task produced meaningful output today.

    Returns:
        assessments: list of OutputAssessment for every task
        failure_patterns: list of pattern dicts (same shape as analyzer output)
            for tasks that failed to produce meaningful output
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    task_summaries = _load_task_summaries(instance_root, date_str, lookback)

    # Tasks with overrides are automatically assessed as healthy
    overridden = {
        tid for tid, s in task_summaries.items() if "_override" in s
    }

    # Build prompt for non-overridden tasks
    assessable = {
        tid: s for tid, s in task_summaries.items() if tid not in overridden
    }

    if not assessable:
        return [], []

    prompt = _build_prompt(assessable)

    try:
        from ..llm import call_llm_json
        result = call_llm_json(prompt, timeout=180)
        raw_assessments = result.get("assessments", [])
    except Exception as e:
        print(f"  [reflect] Output assessment LLM failed: {e}", file=sys.stderr)
        raw_assessments = []

    # Parse into OutputAssessment objects
    assessments = []
    assessed_ids = set()
    for raw in raw_assessments:
        tid = raw.get("task_id", "")
        if tid not in assessable:
            continue
        assessed_ids.add(tid)
        assessments.append(OutputAssessment(
            task_id=tid,
            has_meaningful_output=raw.get("has_meaningful_output", True),
            confidence=raw.get("confidence", "low"),
            evidence=raw.get("evidence", ""),
            is_stale=raw.get("is_stale", False),
            apparent_issue=raw.get("apparent_issue"),
        ))

    # Tasks the LLM didn't mention — if no summary exists, flag them
    for tid in assessable:
        if tid in assessed_ids:
            continue
        today_text = assessable[tid].get("today", "")
        if not today_text.strip():
            assessments.append(OutputAssessment(
                task_id=tid,
                has_meaningful_output=False,
                confidence="high",
                evidence="No summary produced for today",
                is_stale=False,
                apparent_issue="Task produced no summary file",
            ))
        else:
            # LLM missed it — assume healthy (conservative)
            assessments.append(OutputAssessment(
                task_id=tid,
                has_meaningful_output=True,
                confidence="low",
                evidence="LLM did not assess this task; assumed healthy",
                is_stale=False,
                apparent_issue=None,
            ))

    # Add overridden tasks as healthy
    for tid in overridden:
        assessments.append(OutputAssessment(
            task_id=tid,
            has_meaningful_output=True,
            confidence="high",
            evidence=f"Override: {task_summaries[tid].get('_override', '')}",
            is_stale=False,
            apparent_issue=None,
        ))

    # Convert flagged tasks to failure patterns (same shape as analyzer output)
    failure_patterns = []
    for a in assessments:
        if a.has_meaningful_output:
            continue
        failure_patterns.append({
            "pattern_id": f"no_output_{a.task_id}",
            "affected_tasks": [a.task_id],
            "root_cause": a.apparent_issue or "No meaningful output produced",
            "durable_fix_suggestion": "",
            "fix_type": "code" if a.confidence == "high" else "manual",
            "confidence": a.confidence,
            "evidence": a.evidence,
            "is_stale": a.is_stale,
        })

    ok = sum(1 for a in assessments if a.has_meaningful_output)
    flagged = len(assessments) - ok
    print(f"  [reflect] Output assessment: {ok} ok, {flagged} flagged", file=sys.stderr)

    return assessments, failure_patterns
