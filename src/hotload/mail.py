"""Outgoing email (verification and password reset). SMTP only: every provider offers it, so none is a dependency.

The interface is one coroutine, so tests (and a future API-based provider) substitute their own object.
"""

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from .config import Settings
from .logging_setup import log

logger = logging.getLogger("hotload.mail")


class SmtpMailer:
    def __init__(self, settings: Settings):
        self.s = settings

    def _send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()  # raises on CR/LF in any header value, so a crafted address cannot inject headers
        name, addr = parseaddr(self.s.mail_from or "")
        msg["From"] = formataddr((name, addr))
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if self.s.smtp_security == "ssl":
            client = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, timeout=15, context=ctx)
        else:
            client = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15)
        with client:
            if self.s.smtp_security == "starttls":
                client.starttls(context=ctx)
            if self.s.smtp_user:
                client.login(self.s.smtp_user, self.s.smtp_password or "")
            client.send_message(msg)

    async def send(self, to: str, subject: str, body: str) -> None:
        try:
            await asyncio.to_thread(self._send, to, subject, body)
        except Exception as exc:  # noqa: BLE001 - never let a mail outage break (or reveal anything about) a request
            # The message body holds a token, so only the failure type is logged, never the text or recipient.
            log(logger, "mail send failed", logging.WARNING, error=type(exc).__name__)


def build(settings: Settings) -> SmtpMailer | None:
    return SmtpMailer(settings) if settings.mail_enabled else None


def verify_message(link: str) -> tuple[str, str]:
    return ("Confirm your Hotload email address",
            f"Open this link to confirm your email address:\n\n{link}\n\n"
            "It expires in 3 days. If you did not create a Hotload account, ignore this message.\n")


def reset_message(link: str) -> tuple[str, str]:
    return ("Reset your Hotload password",
            f"Open this link to choose a new password:\n\n{link}\n\n"
            "It works once and expires in 1 hour. If you did not ask for this, ignore this message: "
            "your password has not changed.\n")


def recording_message(link: str, title: str | None, ok: bool, detail: str, keep_hours: int) -> tuple[str, str]:
    if ok:
        return ("Your scheduled recording is ready",
                f"Your scheduled recording is ready: {title or 'your file'}\n\nDownload it from your account:\n{link}\n\n"
                f"It stays available for about {keep_hours} hours.\n")
    return ("Your scheduled recording did not happen",
            f"Your scheduled recording could not be completed: {detail}\n\nYou can schedule it again from your account:\n{link}\n")
