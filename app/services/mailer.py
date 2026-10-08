"""Sending email behind a small interface: a console sender for development, SMTP for real."""

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from app.config import Settings

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    """Development: print the message instead of sending it (so reset links are visible)."""

    def send(self, to: str, subject: str, body: str) -> None:
        print(f"\n--- email to {to} ---\nSubject: {subject}\n\n{body}\n--- end email ---\n")
        logger.info("Email printed to console (subject: %s)", subject)  # no address in the log


class SMTPEmailSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, to: str, subject: str, body: str) -> None:
        s = self.settings
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = s.email_from, to, subject
        message.set_content(body)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=15) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_user and s.smtp_password:
                smtp.login(s.smtp_user, s.smtp_password.get_secret_value())
            smtp.send_message(message)
        logger.info("Email sent (subject: %s)", subject)


def build_email_sender(settings: Settings) -> EmailSender:
    return SMTPEmailSender(settings) if settings.email_backend == "smtp" else ConsoleEmailSender()


async def send_in_background(sender: EmailSender, to: str, subject: str, body: str) -> None:
    """Never let a mail server problem break a request."""
    try:
        await asyncio.to_thread(sender.send, to, subject, body)
    except Exception:
        logger.warning("Could not send an email", exc_info=True)
