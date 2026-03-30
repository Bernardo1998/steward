"""Phase 5 — Health Report Generation.

Generates markdown for injection into the daily digest.
Template-based, no LLM calls.
"""


def _status_emoji(days_failing: int) -> str:
    if days_failing == 0:
        return "healthy"
    elif days_failing <= 2:
        return "degraded"
    else:
        return "failing"


def generate_health_report(
    ctx: dict,
    failure_patterns: list[dict],
    engagement_analysis: dict,
    value_assessments: dict,
    actions_taken: list[dict],
) -> str:
    """Generate markdown health report for the daily digest."""
    task_health = ctx["task_health"]
    date_str = ctx["date"]
    sections = []

    # --- Task Status Table ---
    rows = []
    for tid in sorted(task_health.keys()):
        h = task_health[tid]
        sr = h.get("success_rate_7d", 0)
        successes = h.get("successes_7d", 0)
        attempts = h.get("attempts_7d", 0)
        days_f = h.get("days_failing", 0)
        status = _status_emoji(days_f)

        # Find actions taken for this task
        task_actions = [a for a in actions_taken if a.get("task_id") == tid]
        action_text = "--"
        for a in task_actions:
            if a["type"] == "durable_fix" and a.get("fix_applied"):
                smoke = "passed" if a.get("smoke_test") else "failed"
                action_text = f"Fix applied (smoke: {smoke})"
            elif a["type"] == "fix_skipped":
                action_text = "Fix skipped (G11)"

        rows.append(
            f"| {tid} | {sr*100:.0f}% ({successes}/{attempts}) "
            f"| {status} | {days_f} | {action_text} |"
        )

    if rows:
        sections.append("### Task Status (7-day window)\n")
        sections.append("| Task | Success Rate | Status | Days Failing | Action |")
        sections.append("|------|-------------|--------|-------------|--------|")
        sections.extend(rows)
        sections.append("")

    # --- Failure Patterns ---
    actionable_patterns = [p for p in failure_patterns if p.get("root_cause")]
    if actionable_patterns:
        sections.append("### Patterns Detected\n")
        for p in actionable_patterns:
            tasks_str = ", ".join(p["affected_tasks"])
            sections.append(f"- **{p.get('pattern_id', 'unknown')}** ({tasks_str}): "
                          f"{p.get('root_cause', 'unknown')[:200]}")
            if p.get("durable_fix_suggestion"):
                sections.append(f"  - Suggested fix: {p['durable_fix_suggestion'][:150]}")
            if p.get("fix_type") == "manual":
                sections.append(f"  - *Requires human action*")
        sections.append("")

    # --- System Patterns ---
    sys_patterns = ctx.get("system_patterns", [])
    if sys_patterns:
        for sp in sys_patterns:
            tasks_str = ", ".join(sp.get("affected_tasks", []))
            sections.append(f"- **{sp.get('id', 'system')}** ({tasks_str}): "
                          f"{sp.get('description', '')}")
        sections.append("")

    # --- Fixes Applied ---
    fixes = [a for a in actions_taken if a["type"] == "durable_fix" and a.get("fix_applied")]
    if fixes:
        sections.append("### Fixes Applied This Morning\n")
        for i, f in enumerate(fixes, 1):
            smoke_status = "PASS" if f.get("smoke_test") else "FAIL"
            sections.append(
                f"{i}. **{f['task_id']}**: {f.get('fix_description', '?')[:200]}. "
                f"Smoke test: {smoke_status}."
            )
        sections.append("")

    # --- Fix Outcomes (from prior days) ---
    outcomes = [a for a in actions_taken if a["type"] == "fix_outcome_updated"]
    if outcomes:
        sections.append("### Prior Fix Outcomes\n")
        for o in outcomes:
            sections.append(
                f"- {o['task_id']}: fix {o.get('fix_id', '?')} → **{o['outcome']}**"
            )
        sections.append("")

    # --- Engagement Trends ---
    declining = {
        tid: eng for tid, eng in engagement_analysis.items()
        if eng.get("engagement_trend") == "declining"
    }
    if declining:
        sections.append("### Engagement Trends\n")
        for tid, eng in declining.items():
            days = eng.get("days_since_reply", "?")
            issues = "; ".join(eng.get("report_quality_issues", [])[:2])
            adjustments = "; ".join(eng.get("suggested_adjustments", [])[:2])
            sections.append(f"- **{tid}**: No reply in {days} days. "
                          f"{f'Issues: {issues}. ' if issues else ''}"
                          f"{f'Suggest: {adjustments}' if adjustments else ''}")
        sections.append("")

    # --- Value Assessment ---
    negative = {
        tid: v for tid, v in value_assessments.items()
        if v.get("tier") == "negative"
    }
    if negative:
        sections.append("### Low-Value Tasks\n")
        for tid, v in negative.items():
            sections.append(f"- **{tid}**: {v.get('rationale', '?')}. "
                          f"Consider disabling or restructuring.")
        sections.append("")

    # --- Recommendations ---
    recommendations = []
    for p in failure_patterns:
        if p.get("fix_type") == "manual":
            tasks_str = ", ".join(p["affected_tasks"])
            recommendations.append(
                f"Investigate {p.get('pattern_id', 'pattern')} ({tasks_str})"
            )
    for f in fixes:
        if not f.get("smoke_test"):
            recommendations.append(
                f"Review failed smoke test for {f['task_id']}"
            )
    for tid, eng in engagement_analysis.items():
        if eng.get("days_since_reply", 0) and eng["days_since_reply"] >= 7:
            recommendations.append(
                f"Check if {tid} reports are still useful (no reply in {eng['days_since_reply']} days)"
            )

    # Email health
    email_health = ctx.get("email_health", {})
    if email_health.get("error_rate", 0) > 0.2:
        recommendations.append(
            f"Email delivery issues: {email_health.get('errors_7d', 0)} errors, "
            f"{email_health.get('rate_limited_7d', 0)} rate-limited in 7 days"
        )

    if recommendations:
        sections.append("### Recommendations (requires human action)\n")
        for r in recommendations:
            sections.append(f"- [ ] {r}")
        sections.append("")

    if not sections:
        return "*All tasks healthy. No issues detected.*"

    return "\n".join(sections)
