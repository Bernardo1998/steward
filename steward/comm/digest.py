#!/usr/bin/env python3
"""Send the daily digest via email.

Usage:
    python3 -m steward.comm.digest [--date YYYY-MM-DD] [--dry-run]

Exit codes:
    0 — sent successfully, or digest not found (not fatal)
    1 — send failure
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from ..utils.helpers import get_instance_root
from .email import send_email, markdown_to_pdf


def _build_task_pdfs(summaries_dir: Path) -> list[tuple[str, bytes]]:
    """Find task summary.md files and convert each to a PDF attachment."""
    attachments = []
    tasks_dir = summaries_dir / "tasks"
    if not tasks_dir.exists():
        return attachments
    for summary_md in sorted(tasks_dir.glob("*/summary.md")):
        task_id = summary_md.parent.name
        md_text = summary_md.read_text(encoding="utf-8")
        if not md_text.strip():
            continue
        pdf_bytes = markdown_to_pdf(md_text)
        if pdf_bytes:
            attachments.append((f"{task_id}_summary.pdf", pdf_bytes))
        else:
            print(f"  WARNING: Could not convert {task_id}/summary.md to PDF (weasyprint missing?)")
    return attachments


def main():
    parser = argparse.ArgumentParser(description="Email the daily digest")
    parser.add_argument("--date", default=date.today().isoformat(), help="Date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Validate and preview without sending")
    args = parser.parse_args()

    summaries_dir = get_instance_root() / "daily_summaries" / args.date
    digest_path = summaries_dir / "daily_digest.md"
    if not digest_path.exists():
        print(f"No digest found at {digest_path} — skipping email (not fatal).")
        sys.exit(0)

    body = digest_path.read_text(encoding="utf-8")
    if not body.strip():
        print("Digest is empty — skipping email.")
        sys.exit(0)

    # Convert task summaries to PDF attachments
    print("Converting task summaries to PDF...")
    attachments = _build_task_pdfs(summaries_dir)
    if attachments:
        print(f"  Attaching {len(attachments)} PDF(s): {', '.join(name for name, _ in attachments)}")
    else:
        print("  No task summaries found to attach.")

    subject = f"[Daily Digest] {args.date}"
    result = send_email(subject, body, recipient_index=0, dry_run=args.dry_run, attachments=attachments)

    if result["status"] == "dry_run":
        print(f"DRY RUN — would send to: {result['recipient']}")
        print(f"Subject: {result.get('subject', subject)}")
        print(f"Attachments: {result.get('attachments', [])}")
        print(f"Body preview:\n{result.get('body_preview', '')}")
        sys.exit(0)
    elif result["status"] == "sent":
        print(f"Digest emailed to {result['recipient']}")
        sys.exit(0)
    elif result["status"] == "disabled":
        print(f"Email disabled in config — skipping.")
        sys.exit(0)
    elif result["status"] == "rate_limited":
        print(f"Rate limited: {result.get('error', '')}")
        sys.exit(0)
    else:
        print(f"Email failed: {result.get('error', 'unknown error')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
