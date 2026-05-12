"""Phase 4 — Feedback (email).

Self-review (G6), compose email, send via SMTP with threading.
"""

import json
import sys
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid, formatdate
from pathlib import Path
from typing import Optional

import yaml

from .guardrails import g6_self_review, GuardrailResult
from .email_composer import compose_dashboard, compose_email

from ..comm.email import _get_config_path


def _resolve_report_config(ps: dict) -> dict:
    """Read `report` block from per-task charter.yaml if task_dir is provided.

    Returns {} when task_dir is missing or charter.yaml is unreadable, in
    which case the caller falls back to dashboard / `[LTT]` defaults.
    """
    task_dir = ps.get("task_dir")
    if not task_dir:
        return {}
    charter_path = Path(task_dir) / "charter.yaml"
    if not charter_path.exists():
        return {}
    try:
        with open(charter_path) as f:
            charter = yaml.safe_load(f) or {}
        return charter.get("report") or {}
    except Exception:
        return {}


def _load_email_config() -> dict:
    config_path = _get_config_path()
    with open(config_path) as f:
        return yaml.safe_load(f)


def _send_threaded_email(
    subject: str,
    body_markdown: str,
    message_id: str,
    in_reply_to: Optional[str] = None,
) -> dict:
    """Send email with threading headers via SMTP."""
    try:
        cfg = _load_email_config()
    except Exception as e:
        return {"status": "error", "error": str(e)}

    if not cfg.get("enabled", True):
        return {"status": "disabled", "error": "Email disabled in config"}

    sender = cfg["sender"]
    recipient = cfg["recipient_allowlist"][0]

    # Build message
    msg = MIMEMultipart("alternative")
    msg["From"] = sender["address"]
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to

    # Plain text
    msg.attach(MIMEText(body_markdown, "plain", "utf-8"))

    # HTML
    try:
        import markdown as md_lib
        html_body = md_lib.markdown(body_markdown, extensions=["tables", "fenced_code"])
        html = f"""<html><body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            font-size: 14px; line-height: 1.6; color: #333; max-width: 700px; margin: 0 auto; padding: 16px;">
            {html_body}</body></html>"""
        msg.attach(MIMEText(html, "html", "utf-8"))
    except ImportError:
        pass

    try:
        with smtplib.SMTP(sender["smtp_server"], sender["smtp_port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender["address"], sender["app_password"])
            server.sendmail(sender["address"], [recipient], msg.as_string())
        return {"status": "sent", "recipient": recipient, "message_id": message_id}
    except Exception as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def send_ltt_email(
    projects_status: list[dict],
    global_state: dict,
    dry_run: bool = False,
) -> tuple[dict, list[GuardrailResult]]:
    """Run self-review, compose, and send the consolidated LTT email.

    Args:
        projects_status: List of {project_id, status, definition, cycle_summary, days_since_reply}
        global_state: Global LTT state dict
        dry_run: If True, compose but don't send

    Returns:
        (send_result, guardrail_results)
    """
    guardrail_results = []
    # Derive the cycle number used in the subject + body. Multi-project LTT
    # callers set `global_state["global_cycle"]` directly; single-task
    # callers (LTT-template tasks calling send_ltt_email with their own
    # task_state.json as global_state) don't have that key, so fall back to
    # the primary project's status cycle_number, then to global_state["cycle"].
    # Without this fallback, single-task emails ship as "Cycle 0".
    global_cycle = global_state.get("global_cycle")
    if not global_cycle:
        primary_status = (projects_status[0].get("status") or {}) if projects_status else {}
        global_cycle = (
            primary_status.get("cycle_number")
            or global_state.get("cycle")
            or 0
        )

    # G6: Self-review for each project
    for ps in projects_status:
        issues, g6_result = g6_self_review(ps["status"], ps["definition"])
        guardrail_results.append(g6_result)
        if issues:
            print(f"  [phase4] G6 issues for {ps['project_id']}: {issues}", file=sys.stderr)

    # Resolve per-task report config (style + subject prefix) from each
    # project's charter.yaml. Primary project (index 0) drives composer
    # selection and subject prefix; this matches the existing single-project
    # call sites in LTT-template tasks. Multi-project callers should send one
    # email per task today.
    reports = [_resolve_report_config(ps) for ps in projects_status]
    primary_report = reports[0] if reports else {}
    style = (primary_report.get("style") or "dashboard").lower()
    narrative_spec = primary_report.get("narrative") or {}
    prefix = (
        (primary_report.get("own_email") or {}).get("prefix")
        or "[LTT]"
    )

    # Compose email body via dispatcher
    body_md = compose_email(
        projects_status,
        global_cycle,
        style=style,
        narrative_spec=narrative_spec,
    )

    # Determine subject (using charter-resolved prefix)
    any_needs_input = any(
        ps["status"].get("needs_human_input", False) for ps in projects_status
    )
    max_days = max((ps.get("days_since_reply", 0) for ps in projects_status), default=0)

    if max_days == 0:
        subject = f"{prefix} Research Report — Cycle {global_cycle}"
    elif any_needs_input:
        subject = f"{prefix} Cycle {global_cycle} — Day {max_days + 1}, needs input"
    else:
        subject = f"{prefix} Cycle {global_cycle} — Updated"

    if dry_run:
        return {
            "status": "dry_run",
            "subject": subject,
            "body_preview": body_md[:500],
            "body_length": len(body_md),
        }, guardrail_results

    # Get previous message ID for threading
    prev_message_id = global_state.get("last_message_id")
    new_message_id = make_msgid(domain="ltt.timemanagement.local")

    print(f"  [phase4] Sending email: {subject}", file=sys.stderr)
    result = _send_threaded_email(subject, body_md, new_message_id, prev_message_id)

    if result["status"] == "sent":
        # Store for threading
        result["message_id"] = new_message_id
        result["subject"] = subject

        # Save email body for reference
        global_state["last_message_id"] = new_message_id
        global_state["last_email_date"] = datetime.now().strftime("%Y-%m-%d")

    return result, guardrail_results
