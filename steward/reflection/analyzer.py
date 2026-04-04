"""Analysis passes — engagement and value assessment.

Failure detection is handled by output_assessor.py (output-based, not signal-based).
This module provides supplementary engagement and value analysis.
"""

import json
import sys
from typing import Optional

from ..llm import call_llm_json


# ---------------------------------------------------------------------------
# Engagement Analysis
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
        result = call_llm_json(prompt, timeout=180)
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
        result = call_llm_json(prompt, timeout=180)
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
