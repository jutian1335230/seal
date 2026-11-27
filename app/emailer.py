"""Optional email. The signing link works with or without it."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def email_enabled() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def send_email(to: str, subject: str, body: str) -> tuple[bool, str]:
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return False, "Email isn't set up on this server. Use the signing link."

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("SMTP_FROM", "").strip() or user
    if not sender:
        return False, "Set SMTP_FROM to the address these emails should come from."

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=12) as server:
                if user:
                    server.login(user, password)
                server.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=12) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if user:
                    server.login(user, password)
                server.send_message(message)
    except Exception as exc:
        return False, f"Email didn't send: {exc}"
    return True, "Email sent."
