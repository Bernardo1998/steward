"""Tests for the shared email utility."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steward.comm.email import send_email


def _email_config():
    return {
        "enabled": True,
        "sender": {
            "address": "robot@example.com",
            "app_password": "secret",
            "smtp_server": "smtp.example.com",
            "smtp_port": 587,
        },
        "recipient_allowlist": ["user@example.com"],
        "rate_limit": {
            "max_sends_per_day": 5,
            "cooldown_seconds": 0,
        },
    }


class TestSendEmail:
    def test_dry_run_returns_message_id(self):
        with patch("steward.comm.email._load_email_config", return_value=_email_config()):
            result = send_email("Subject", "Body", dry_run=True)

        assert result["status"] == "dry_run"
        assert result["message_id"].startswith("<")

    def test_sent_email_returns_message_id(self):
        smtp_client = MagicMock()
        smtp_cm = MagicMock()
        smtp_cm.__enter__.return_value = smtp_client
        smtp_cm.__exit__.return_value = False

        with patch("steward.comm.email._load_email_config", return_value=_email_config()):
            with patch("steward.comm.email._check_rate_limit", return_value=True):
                with patch("steward.comm.email._log_send"):
                    with patch("steward.comm.email.smtplib.SMTP", return_value=smtp_cm):
                        result = send_email("Subject", "Body")

        assert result["status"] == "sent"
        assert result["message_id"].startswith("<")
        smtp_client.sendmail.assert_called_once()
