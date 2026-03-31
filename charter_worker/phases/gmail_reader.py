"""Gmail IMAP reader — fetch replies to LTT emails.

Uses the same app password from email_config.yaml (already used for SMTP sending).
Extracts both reply text and file attachments (PDF, images, docs).
"""

import email as email_lib
import imaplib
import json
import sys
from datetime import datetime, timedelta
from email.header import decode_header
from pathlib import Path

import yaml

from ..comm.email import _get_config_path

# Attachment types we accept
_ACCEPTED_EXTENSIONS = {
    ".pdf", ".txt", ".md", ".tex", ".csv", ".json", ".yaml", ".yml",
    ".py", ".bib", ".rst", ".doc", ".docx", ".png", ".jpg", ".jpeg",
}
_MAX_ATTACHMENT_SIZE = 20_000_000  # 20MB


def _load_email_config() -> dict:
    config_path = _get_config_path()
    if not config_path.exists():
        raise FileNotFoundError(f"Email config not found at {config_path}")
    with open(config_path) as f:
        return yaml.safe_load(f)


def _extract_body(msg) -> str:
    """Extract plain text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                cd = str(part.get("Content-Disposition", ""))
                # Skip text/plain parts that are attachments
                if "attachment" in cd:
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
        # Fallback to HTML if no plain text
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                cd = str(part.get("Content-Disposition", ""))
                if "attachment" in cd:
                    continue
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
    return ""


def _extract_attachments(msg) -> list[dict]:
    """Extract file attachments from email message.

    Returns list of {filename, content_type, data (bytes), size}.
    """
    attachments = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        cd = str(part.get("Content-Disposition", ""))
        if "attachment" not in cd and "inline" not in cd:
            continue
        # Skip text/plain and text/html body parts
        ct = part.get_content_type()
        if ct in ("text/plain", "text/html") and "attachment" not in cd:
            continue

        filename = part.get_filename()
        if not filename:
            # Try to decode from Content-Disposition
            filename = _decode_header_value(part.get_filename() or "")
        if not filename:
            continue

        filename = _decode_header_value(filename)

        # Check extension
        suffix = Path(filename).suffix.lower()
        if suffix not in _ACCEPTED_EXTENSIONS:
            print(f"  [gmail_reader] Skipping unsupported attachment: {filename}", file=sys.stderr)
            continue

        data = part.get_payload(decode=True)
        if not data:
            continue
        if len(data) > _MAX_ATTACHMENT_SIZE:
            print(f"  [gmail_reader] Skipping oversized attachment: {filename} ({len(data)} bytes)", file=sys.stderr)
            continue

        attachments.append({
            "filename": filename,
            "content_type": ct,
            "data": data,
            "size": len(data),
        })

    return attachments


def _decode_header_value(value: str) -> str:
    """Decode RFC 2047 encoded header."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes. Tries PyMuPDF first, falls back to PyPDF2."""
    # Try PyMuPDF (best quality)
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        text = "\n\n".join(pages)
        if text.strip():
            return text
    except Exception:
        pass

    # Fallback: PyPDF2
    try:
        import io
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                pages.append(t)
        text = "\n\n".join(pages)
        if text.strip():
            return text
    except Exception:
        pass

    # Fallback: pdfminer
    try:
        import io
        from pdfminer.high_level import extract_text as pdfminer_extract
        text = pdfminer_extract(io.BytesIO(pdf_bytes))
        if text.strip():
            return text
    except Exception:
        pass

    return "(PDF text extraction failed — could not read content)"


def save_attachments_to_context(
    attachments: list[dict],
    project_dir: Path,
) -> list[str]:
    """Save email attachments to project's context_files/ directory.

    PDFs are saved as-is AND as extracted .txt for the synthesis prompt.

    Returns list of saved file descriptions.
    """
    ctx_dir = project_dir / "context_files"
    ctx_dir.mkdir(exist_ok=True)

    saved = []
    for att in attachments:
        filename = att["filename"]
        data = att["data"]
        suffix = Path(filename).suffix.lower()

        # Timestamp prefix to avoid overwrites
        ts = datetime.now().strftime("%Y%m%d")
        safe_name = f"{ts}_{filename}"
        dest = ctx_dir / safe_name

        # Save raw file
        dest.write_bytes(data)
        saved.append(f"{safe_name} ({att['size']} bytes)")

        # For PDFs: also extract text and save as .txt
        if suffix == ".pdf":
            print(f"  [gmail_reader] Extracting text from PDF: {filename}", file=sys.stderr)
            text = extract_pdf_text(data)
            txt_name = f"{ts}_{Path(filename).stem}.pdf.txt"
            txt_dest = ctx_dir / txt_name
            txt_dest.write_text(text, encoding="utf-8")
            saved.append(f"{txt_name} (extracted text, {len(text)} chars)")

    return saved


def fetch_ltt_replies(
    since_date: str | None = None,
    subject_prefix: str = "[LTT]",
    max_results: int = 10,
    require_reply: bool = False,
) -> list[dict]:
    """Connect to Gmail via IMAP, search for messages with a subject prefix.

    By default picks up ALL matching messages — both replies to task emails
    AND fresh emails you compose to the bot address. This lets you send
    proactive instructions without needing a prior task email to reply to.

    Args:
        since_date: "YYYY-MM-DD" format. Defaults to 3 days ago.
        subject_prefix: Filter by subject line prefix.
        max_results: Max messages to return.
        require_reply: If True, only return messages that are replies
            (have an In-Reply-To header). False by default.

    Returns:
        List of {from, date, subject, body, in_reply_to, message_id, attachments} dicts.
        attachments is a list of {filename, content_type, data, size}.
    """
    cfg = _load_email_config()
    sender_addr = cfg["sender"]["address"]
    app_password = cfg["sender"]["app_password"]

    # Format date for IMAP SINCE query (DD-Mon-YYYY)
    if since_date:
        dt = datetime.strptime(since_date, "%Y-%m-%d")
    else:
        dt = datetime.now() - timedelta(days=3)
    imap_date = dt.strftime("%d-%b-%Y")

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        mail.login(sender_addr, app_password)
        mail.select("INBOX")

        # Search for emails TO the bot address with our subject prefix
        search_criteria = f'(SINCE "{imap_date}" SUBJECT "{subject_prefix}")'
        status, messages = mail.search(None, search_criteria)

        if status != "OK" or not messages[0]:
            mail.logout()
            return []

        msg_ids = messages[0].split()
        # Take the most recent N
        msg_ids = msg_ids[-max_results:]

        replies = []
        for msg_id in msg_ids:
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
            msg = email_lib.message_from_bytes(msg_data[0][1])

            from_addr = _decode_header_value(msg.get("From", ""))
            in_reply_to = msg.get("In-Reply-To", "")
            # Optionally restrict to replies only
            if require_reply and not in_reply_to:
                continue

            body = _extract_body(msg)
            attachments = _extract_attachments(msg)

            replies.append({
                "from": from_addr,
                "date": msg.get("Date", ""),
                "subject": _decode_header_value(msg.get("Subject", "")),
                "body": body,
                "in_reply_to": msg.get("In-Reply-To", ""),
                "message_id": msg.get("Message-ID", ""),
                "attachments": attachments,
            })

        mail.logout()
        return replies

    except (imaplib.IMAP4.error, OSError, TimeoutError) as e:
        print(f"[gmail_reader] IMAP error: {e}", file=sys.stderr)
        return []


if __name__ == "__main__":
    # Test connectivity
    print("Testing Gmail IMAP connection...")
    try:
        replies = fetch_ltt_replies(subject_prefix="[LTT]")
        print(f"Found {len(replies)} LTT replies")
        for r in replies:
            att_count = len(r.get("attachments", []))
            print(f"  From: {r['from']}, Subject: {r['subject']}, Attachments: {att_count}")
            for a in r.get("attachments", []):
                print(f"    - {a['filename']} ({a['size']} bytes, {a['content_type']})")
    except Exception as e:
        print(f"Error: {e}")
