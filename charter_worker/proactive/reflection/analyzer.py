"""Phase 2-3 — Analysis.

Three LLM-backed passes: failure patterns, engagement, value assessment.
"""

import json
import sys
from difflib import SequenceMatcher
from typing import Optional

from ..llm import call_llm_json


# ---------------------------------------------------------------------------
# Pass 1 — Failure Pattern Analysis
# ---------------------------------------------------------------------------

def _group_failure_patterns(task_health: dict) -> list[dict]:
    """Programmatic grouping of failures by root cause similarity.

    Groups tasks that share similar error messages or diagnosis text.
    """
    patterns = []

    # Group by diagnosis similarity
    persistent_tasks = {
        tid: h for tid, h in task_health.items()
        if h.get("days_failing", 0) >= 3
    }

    if not persistent_tasks:
        return patterns

    # Cluster by diagnosis text similarity
    clustered = set()
    task_list = list(persistent_tasks.items())

    for i, (tid1, h1) in enumerate(task_list):
        if tid1 in clustered:
            continue
        cluster = [tid1]
        diag1 = h1.get("current_diagnosis", {}).get("result", {}).get("diagnosis", "")

        for j in range(i + 1, len(task_list)):
            tid2, h2 = task_list[j]
            if tid2 in clustered:
                continue
            diag2 = h2.get("current_diagnosis", {}).get("result", {}).get("diagnosis", "")
            if diag1 and diag2:
                sim = SequenceMatcher(None, diag1.lower(), diag2.lower()).ratio()
                if sim > 0.4:
                    cluster.append(tid2)

        if len(cluster) >= 2:
            for t in cluster:
                clustered.add(t)
            patterns.append({
                "affected_tasks": cluster,
                "type": "diagnosis_cluster",
                "sample_diagnosis": diag1[:200],
            })

    # Add unclustered persistent failures as individual patterns
    for tid, h in persistent_tasks.items():
        if tid not in clustered:
            patterns.append({
                "affected_tasks": [tid],
                "type": "persistent_single",
                "sample_diagnosis": h.get("current_diagnosis", {}).get("result", {}).get("diagnosis", "")[:200],
                "days_failing": h.get("days_failing", 0),
            })

    return patterns


def _build_failure_context(task_health: dict, pattern: dict) -> str:
    """Build context string for LLM failure analysis."""
    sections = []
    for tid in pattern["affected_tasks"]:
        h = task_health.get(tid, {})
        sections.append(f"""
TASK: {tid}
  Days failing: {h.get('days_failing', '?')}
  Success rate (7d): {h.get('success_rate_7d', '?')}
  Recent errors: {json.dumps(h.get('errors', [])[:5], indent=2)}
  Diagnosis history (last 5):
{json.dumps(h.get('diagnosis_history', [])[-5:], indent=2)}
  Fix history (last 3):
{json.dumps(h.get('fix_history', [])[-3:], indent=2)}
  Log tail (last 1000 chars):
{h.get('log_tail', '(no log)')[-1000:]}
""")
    return "\n".join(sections)


def analyze_failure_patterns(ctx: dict) -> list[dict]:
    """Analyze failure patterns across tasks. Returns list of FailurePattern dicts.

    Each pattern has:
        pattern_id, affected_tasks, root_cause, durable_fix_suggestion,
        fix_type, confidence, prior_fixes_tried
    """
    task_health = ctx["task_health"]
    system_patterns = ctx.get("system_patterns", [])

    # Programmatic grouping
    grouped = _group_failure_patterns(task_health)

    # Add system-level patterns
    for sp in system_patterns:
        if sp.get("type") == "systemic":
            grouped.append(sp)

    if not grouped:
        print("  [reflect] No persistent failure patterns detected", file=sys.stderr)
        return []

    # LLM analysis for patterns with persistent failures
    persistent_patterns = [
        p for p in grouped
        if any(
            task_health.get(t, {}).get("days_failing", 0) >= 3
            for t in p.get("affected_tasks", [])
        )
    ]

    if not persistent_patterns:
        # Return programmatic patterns without LLM enrichment
        return [{
            "pattern_id": p.get("id", f"pattern_{i}"),
            "affected_tasks": p["affected_tasks"],
            "root_cause": p.get("sample_diagnosis", p.get("description", "unknown")),
            "durable_fix_suggestion": "",
            "fix_type": "unknown",
            "confidence": "low",
            "prior_fixes_tried": [],
        } for i, p in enumerate(grouped)]

    # Build combined context for LLM
    all_context = ""
    for p in persistent_patterns[:3]:  # cap to 3 patterns for prompt length
        all_context += f"\n--- Pattern (affects {p['affected_tasks']}) ---\n"
        all_context += _build_failure_context(task_health, p)

    prompt = f"""You are analyzing recurring failures in an automated task orchestrator.
These tasks have been failing for 3+ days despite reactive diagnosis and fixes.
Your job is to identify the DEEPER root causes that the reactive system missed.

{all_context}

For each pattern, consider:
1. Are the reactive fixes addressing symptoms rather than root causes?
2. Is there a systemic issue (environment, infrastructure, configuration)?
3. What durable fix would prevent recurrence, not just patch today's failure?
4. Can the fix be applied automatically to code/config, or does it need human action?

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "patterns": [
    {{
      "affected_tasks": ["task_id1", "task_id2"],
      "root_cause": "One paragraph identifying the deeper root cause",
      "durable_fix_suggestion": "Specific fix description that addresses the root cause",
      "fix_type": "code|config|disable|manual",
      "confidence": "high|medium|low",
      "prior_fixes_ineffective_because": "Why previous fixes didn't stick"
    }}
  ]
}}"""

    try:
        result = call_llm_json(prompt, timeout=120)
        llm_patterns = result.get("patterns", [])
    except Exception as e:
        print(f"  [reflect] Failure analysis LLM failed: {e}", file=sys.stderr)
        llm_patterns = []

    # Merge LLM results with programmatic patterns
    analyzed = []
    for i, p in enumerate(persistent_patterns[:3]):
        llm_p = llm_patterns[i] if i < len(llm_patterns) else {}
        prior_fixes = []
        for tid in p["affected_tasks"]:
            for fix in task_health.get(tid, {}).get("fix_history", []):
                prior_fixes.append(fix.get("description", "")[:100])

        analyzed.append({
            "pattern_id": f"persistent_{i}",
            "affected_tasks": p["affected_tasks"],
            "root_cause": llm_p.get("root_cause", p.get("sample_diagnosis", "unknown")),
            "durable_fix_suggestion": llm_p.get("durable_fix_suggestion", ""),
            "fix_type": llm_p.get("fix_type", "unknown"),
            "confidence": llm_p.get("confidence", "low"),
            "prior_fixes_tried": prior_fixes,
            "prior_fixes_ineffective_because": llm_p.get("prior_fixes_ineffective_because", ""),
        })

    print(f"  [reflect] Failure patterns: {len(analyzed)} detected "
          f"({sum(1 for p in analyzed if p['fix_type'] in ('code', 'config'))} actionable)",
          file=sys.stderr)

    return analyzed


# ---------------------------------------------------------------------------
# Pass 2 — Engagement Analysis
# ---------------------------------------------------------------------------

def analyze_engagement(ctx: dict) -> dict:
    """Analyze user engagement per task. Returns per-task engagement analysis.

    Only analyzes tasks that have email feedback loops (days_since_reply is not None).
    """
    engagement = ctx["engagement"]
    task_health = ctx["task_health"]
    prior = ctx.get("prior_reflection", {}).get("engagement_history", {})

    # Filter to tasks with email loops
    email_tasks = {
        tid: eng for tid, eng in engagement.items()
        if eng.get("has_email_loop")
    }

    if not email_tasks:
        print("  [reflect] No tasks with email feedback loops", file=sys.stderr)
        return {}

    # Build context for LLM
    task_context = ""
    for tid, eng in email_tasks.items():
        health = task_health.get(tid, {})
        history = prior.get(tid, [])
        recent_replies = [h for h in history[-7:] if h.get("days_since_reply") == 0]

        task_context += f"""
TASK: {tid}
  Days since last reply: {eng.get('days_since_reply', '?')}
  Emails sent (7d): {eng.get('emails_sent_7d', 0)}
  Crash emails (7d): {eng.get('crash_emails_7d', 0)}
  Task success rate (7d): {health.get('success_rate_7d', '?')}
  Reply history (last 7 snapshots): {json.dumps(history[-7:], indent=2) if history else '(no history)'}
  Replies in window: {len(recent_replies)}
"""

    prompt = f"""You are analyzing user engagement with an automated personal assistant system.
Each task sends periodic email reports. The user replies to provide feedback.

{task_context}

For each task, assess:
1. Is engagement increasing, stable, or declining?
2. If declining: is it because the reports are low quality, too frequent, or the task
   keeps failing (user gave up)?
3. What specific adjustments would improve engagement?

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "tasks": [
    {{
      "task_id": "...",
      "engagement_trend": "increasing|stable|declining|unknown",
      "report_quality_score": 3,
      "report_quality_issues": ["issue 1"],
      "suggested_adjustments": ["adjustment 1"],
      "likely_cause_of_decline": "reason or null"
    }}
  ]
}}"""

    try:
        result = call_llm_json(prompt, timeout=90)
        task_results = result.get("tasks", [])
    except Exception as e:
        print(f"  [reflect] Engagement analysis LLM failed: {e}", file=sys.stderr)
        task_results = []

    # Build result dict
    analysis = {}
    result_map = {t["task_id"]: t for t in task_results if "task_id" in t}
    for tid in email_tasks:
        llm = result_map.get(tid, {})
        analysis[tid] = {
            "task_id": tid,
            "engagement_trend": llm.get("engagement_trend", "unknown"),
            "report_quality_score": llm.get("report_quality_score", 3),
            "report_quality_issues": llm.get("report_quality_issues", []),
            "suggested_adjustments": llm.get("suggested_adjustments", []),
            "likely_cause_of_decline": llm.get("likely_cause_of_decline"),
            "days_since_reply": engagement[tid].get("days_since_reply"),
        }

    declining = [tid for tid, a in analysis.items() if a["engagement_trend"] == "declining"]
    if declining:
        print(f"  [reflect] Engagement declining: {declining}", file=sys.stderr)

    return analysis


# ---------------------------------------------------------------------------
# Pass 3 — Value Assessment
# ---------------------------------------------------------------------------

def assess_task_value(ctx: dict) -> dict:
    """Assess each task's value contribution. Returns per-task value tier."""
    task_health = ctx["task_health"]
    engagement = ctx["engagement"]

    # Build context
    task_context = ""
    for tid, health in task_health.items():
        eng = engagement.get(tid, {})
        task_context += f"""
TASK: {tid}
  Success rate (7d): {health.get('success_rate_7d', '?')}
  Days failing: {health.get('days_failing', 0)}
  Avg duration: {health.get('avg_duration_s', '?')}s
  Has email loop: {eng.get('has_email_loop', False)}
  Days since reply: {eng.get('days_since_reply', 'N/A')}
  Crash emails (7d): {eng.get('crash_emails_7d', 0)}
"""

    prompt = f"""You are assessing the value of tasks in a personal productivity system.
The goal is to maximize the user's productivity: papers published, experiments completed,
life achievements, health improvements.

{task_context}

For each task, assign a value tier:
- "high": Directly drives output (papers, experiments, decisions). Worth fixing if broken.
- "medium": Supports productivity (planning, organization). Important but not output-producing.
- "low": Nice to have but not critical. Could be paused without impact.
- "negative": Consuming resources (LLM budget, user attention with crash emails) without
  producing value. Should be disabled or restructured.

Consider: A task that keeps failing and spamming crash emails is WORSE than a disabled task.

Respond with ONLY a JSON block fenced with ```json ... ``` containing:
{{
  "assessments": [
    {{
      "task_id": "...",
      "tier": "high|medium|low|negative",
      "rationale": "One sentence explanation"
    }}
  ]
}}"""

    try:
        result = call_llm_json(prompt, timeout=90)
        assessments = result.get("assessments", [])
    except Exception as e:
        print(f"  [reflect] Value assessment LLM failed: {e}", file=sys.stderr)
        assessments = []

    value_map = {}
    for a in assessments:
        tid = a.get("task_id")
        if tid:
            value_map[tid] = {
                "tier": a.get("tier", "medium"),
                "rationale": a.get("rationale", ""),
            }

    return value_map
