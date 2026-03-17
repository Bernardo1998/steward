"""Compose multi-project LTT email in dashboard format."""


def compose_dashboard(projects_status: list[dict], global_cycle: int) -> str:
    """Build markdown email body from project statuses.

    Args:
        projects_status: List of dicts with project_id, status, definition, cycle_summary.
        global_cycle: The global LTT cycle number.

    Returns:
        Markdown string for email body.
    """
    lines = []
    lines.append(f"# Research Agent Daily Report — Cycle {global_cycle}")
    lines.append("")

    # Dashboard table
    lines.append("## Dashboard")
    lines.append("")
    lines.append("| Project | Confidence | Status | Needs Input? | Days Since Reply |")
    lines.append("|---------|-----------|--------|-------------|-----------------|")

    needs_input = []
    on_track = []

    for ps in projects_status:
        pid = ps["project_id"]
        status = ps["status"]
        conf = status.get("confidence_score", "?")
        needs = status.get("needs_human_input", False)
        days = ps.get("days_since_reply", 0)

        # Determine status label
        if needs:
            status_label = "Needs input"
        elif conf and int(conf) <= 2:
            status_label = "Low confidence"
        else:
            status_label = "On track"

        needs_label = "**YES**" if needs else "No"
        lines.append(f"| {pid} | {conf}/5 | {status_label} | {needs_label} | {days} |")

        if needs:
            needs_input.append(ps)
        else:
            on_track.append(ps)

    lines.append("")

    # Projects needing input
    if needs_input:
        lines.append("## Projects Needing Input (read these)")
        lines.append("")
        for ps in needs_input:
            lines.append(f"### {ps['project_id']}")
            lines.append("")
            summary = ps.get("cycle_summary", {})
            lines.append(summary.get("tldr", "No summary available."))
            lines.append("")
            questions = ps["status"].get("human_input_questions", [])
            if questions:
                lines.append("**Questions for you:**")
                for q in questions:
                    lines.append(f"- {q}")
                lines.append("")

    # Projects on track
    if on_track:
        lines.append("## Projects On Track (skim or skip)")
        lines.append("")
        for ps in on_track:
            lines.append(f"### {ps['project_id']}")
            lines.append("")
            summary = ps.get("cycle_summary", {})
            lines.append(summary.get("tldr", "No summary available."))
            lines.append("")

    # Full status documents
    lines.append("---")
    lines.append("")
    lines.append("## Full Status Documents")
    lines.append("")
    for ps in projects_status:
        lines.append(f"### {ps['project_id']} (Cycle {ps['status'].get('cycle_number', '?')})")
        lines.append("")
        status = ps["status"]
        lines.append(f"**Hypothesis:** {status.get('current_hypothesis', 'N/A')}")
        lines.append("")

        findings = status.get("key_findings", [])
        if findings:
            lines.append("**Key Findings:**")
            for f in findings:
                score = f.get("relevance_score", "?")
                lines.append(f"- [{score}/5] {f.get('finding', '')}")
                prov = f.get("provenance", "")
                if prov:
                    lines.append(f"  - Source: {prov}")
            lines.append("")

        questions = status.get("open_questions", [])
        if questions:
            lines.append("**Open Questions:**")
            for q in questions:
                pri = q.get("priority", "medium")
                lines.append(f"- [{pri}] {q.get('question', '')}")
            lines.append("")

        suggestions = status.get("action_suggestions", [])
        if suggestions:
            lines.append("**Suggested Actions:**")
            for s in suggestions:
                novel = " (NEW)" if s.get("novel") else ""
                lines.append(f"- {s.get('action', '')}{novel}")
                if s.get("rationale"):
                    lines.append(f"  - Rationale: {s['rationale']}")
            lines.append("")

        lines.append("---")
        lines.append("")

    return "\n".join(lines)
