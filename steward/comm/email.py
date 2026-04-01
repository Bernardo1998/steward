"""Shared email utility — security-hardened, rate-limited, audit-logged.

Usage:
    from steward.comm.email import send_email
    result = send_email("Subject", "# Markdown body", recipient_index=0)
"""

import json
import os
import smtplib
import time
from datetime import datetime, date
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml

try:
    import markdown as md_lib
except ImportError:
    md_lib = None

try:
    import weasyprint
except ImportError:
    weasyprint = None

def _get_config_path() -> Path:
    """Resolve email config path: STEWARD_EMAIL_CONFIG env var, or instance root."""
    env = os.environ.get("STEWARD_EMAIL_CONFIG")
    if env:
        return Path(env)
    from ..utils.helpers import get_instance_root
    return get_instance_root() / "email_config.yaml"


def _get_state_dir() -> Path:
    """Resolve email state directory under instance root."""
    from ..utils.helpers import get_instance_root
    return get_instance_root() / "tasks" / "_shared" / "state"


def _get_send_log() -> Path:
    return _get_state_dir() / "email_send_log.jsonl"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_email_config() -> dict:
    """Load and validate email_config.yaml. Raises on missing/invalid config."""
    config_path = _get_config_path()
    if not config_path.exists():
        raise FileNotFoundError(
            f"Email config not found at {config_path}\n"
            f"Copy email_config.example.yaml to email_config.yaml and fill in your values.\n"
            f"See the example file for setup instructions."
        )
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    # Validate required fields
    for key in ("sender", "recipient_allowlist", "rate_limit"):
        if key not in cfg:
            raise ValueError(f"Missing required config key: {key}")
    sender = cfg["sender"]
    for key in ("address", "app_password", "smtp_server", "smtp_port"):
        if key not in sender:
            raise ValueError(f"Missing sender.{key} in email config")
    if not cfg["recipient_allowlist"]:
        raise ValueError("recipient_allowlist must contain at least one address")
    return cfg


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def _check_rate_limit(cfg: dict) -> bool:
    """Return True if another send is allowed today."""
    max_per_day = cfg["rate_limit"].get("max_sends_per_day", 5)
    cooldown_s = cfg["rate_limit"].get("cooldown_seconds", 60)
    today_str = date.today().isoformat()

    send_log = _get_send_log()
    if not send_log.exists():
        return True

    today_sends = 0
    last_send_ts = 0.0
    with open(send_log) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("date") == today_str and entry.get("status") == "sent":
                today_sends += 1
                ts = entry.get("timestamp_epoch", 0)
                if ts > last_send_ts:
                    last_send_ts = ts

    if today_sends >= max_per_day:
        return False
    if last_send_ts and (time.time() - last_send_ts) < cooldown_s:
        return False
    return True


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def _log_send(recipient: str, subject: str, status: str, error: str | None = None):
    """Append a send record to the audit log."""
    state_dir = _get_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    send_log = _get_send_log()
    entry = {
        "timestamp": datetime.now().isoformat(),
        "timestamp_epoch": time.time(),
        "date": date.today().isoformat(),
        "recipient": recipient,
        "subject": subject,
        "status": status,
    }
    if error:
        entry["error"] = error
    with open(send_log, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Markdown → HTML
# ---------------------------------------------------------------------------

_HTML_WRAPPER = """\
<html><body style="font-family: 'Microsoft YaHei', '微软雅黑', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
 font-size: 14px; line-height: 1.6; color: #333; max-width: 700px; margin: 0 auto; padding: 16px;">
{body}
</body></html>"""


def _markdown_to_html(md_text: str) -> str:
    """Convert markdown to HTML with minimal inline styling."""
    if md_lib is not None:
        html_body = md_lib.markdown(md_text, extensions=["tables", "fenced_code"])
    else:
        # Fallback: wrap plain text in <pre>
        import html
        html_body = f"<pre>{html.escape(md_text)}</pre>"
    return _HTML_WRAPPER.format(body=html_body)


def markdown_to_pdf(md_text: str) -> bytes | None:
    """Convert markdown text to PDF bytes. Returns None if weasyprint is unavailable."""
    if weasyprint is None:
        return None
    html = _markdown_to_html(md_text)
    return weasyprint.HTML(string=html).write_pdf()


# ---------------------------------------------------------------------------
# Main send function
# ---------------------------------------------------------------------------

def send_email(
    subject: str,
    body_markdown: str,
    recipient_index: int = 0,
    dry_run: bool = False,
    attachments: list[tuple[str, bytes]] | None = None,
) -> dict:
    """Send an email to a recipient from the allowlist.

    Args:
        subject: Email subject line.
        body_markdown: Email body in markdown (converted to HTML + plain text).
        recipient_index: Index into recipient_allowlist (never a free-form address).
        dry_run: If True, validate and return what would be sent without sending.
        attachments: List of (filename, data_bytes) tuples to attach.

    Returns:
        {"status": "sent"|"rate_limited"|"error"|"disabled"|"dry_run",
         "recipient": "...", "error": "..."}
    """
    try:
        cfg = _load_email_config()
    except (FileNotFoundError, ValueError) as e:
        return {"status": "error", "recipient": "", "error": str(e)}

    if not cfg.get("enabled", True):
        return {"status": "disabled", "recipient": "", "error": "Email sending is disabled in config"}

    allowlist = cfg["recipient_allowlist"]
    if recipient_index < 0 or recipient_index >= len(allowlist):
        return {
            "status": "error",
            "recipient": "",
            "error": f"recipient_index {recipient_index} out of range (allowlist has {len(allowlist)} entries)",
        }
    recipient = allowlist[recipient_index]

    if dry_run:
        return {
            "status": "dry_run",
            "recipient": recipient,
            "subject": subject,
            "body_preview": body_markdown[:500],
            "attachments": [name for name, _ in (attachments or [])],
        }

    if not _check_rate_limit(cfg):
        _log_send(recipient, subject, "rate_limited")
        return {"status": "rate_limited", "recipient": recipient, "error": "Daily rate limit or cooldown reached"}

    # Build multipart message
    sender = cfg["sender"]
    msg = MIMEMultipart("mixed")
    msg["From"] = sender["address"]
    msg["To"] = recipient
    msg["Subject"] = subject

    # Body: plain text + HTML alternative
    body_part = MIMEMultipart("alternative")
    body_part.attach(MIMEText(body_markdown, "plain", "utf-8"))
    body_part.attach(MIMEText(_markdown_to_html(body_markdown), "html", "utf-8"))
    msg.attach(body_part)

    # Attachments
    for filename, data in (attachments or []):
        part = MIMEApplication(data, Name=filename)
        part["Content-Disposition"] = f'attachment; filename="{filename}"'
        msg.attach(part)

    try:
        with smtplib.SMTP(sender["smtp_server"], sender["smtp_port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender["address"], sender["app_password"])
            server.sendmail(sender["address"], [recipient], msg.as_string())
        _log_send(recipient, subject, "sent")
        return {"status": "sent", "recipient": recipient}
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        _log_send(recipient, subject, "error", error=error_msg)
        return {"status": "error", "recipient": recipient, "error": error_msg}
