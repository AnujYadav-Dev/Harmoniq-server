"""Email alert when YouTube keeps blocking downloads. `python -m app.alerts` sends a test email."""

from __future__ import annotations

import logging
import smtplib
import ssl
import sys
import threading
import time
from collections.abc import Callable
from email.message import EmailMessage

from app.config import Settings, get_settings

log = logging.getLogger("harmoniq.alerts")

SUBJECT = "Harmoniq: YouTube is blocking downloads"
BODY = """\
{count} downloads were blocked by YouTube in the last {minutes} minutes on {server}.

Last error: {detail}

The cookies have probably expired. To fix it, export new cookies in an Incognito window, paste them into the
YTDLP_COOKIES secret on GitHub, then run Actions > Update cookies.

You won't get another email about this for {cooldown} hours.
"""


def send_email(settings: Settings, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_user
    message["To"] = settings.alert_email_to
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


class BlockAlerter:
    """Counts blocked downloads and sends one email when too many happen close together."""

    THRESHOLD = 3
    WINDOW = 1800
    COOLDOWN = 21600

    def __init__(self, settings: Settings, send: Callable[[Settings, str, str], None] = send_email,
                 clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self._send = send
        self._clock = clock
        self._blocked_at: list[float] = []
        self._last_alert_at: float | None = None

    def record_blocked(self, detail: str) -> bool:
        """Called from the event loop. Returns True if this block triggered an alert."""
        now = self._clock()
        self._blocked_at = [t for t in self._blocked_at if now - t < self.WINDOW] + [now]
        if len(self._blocked_at) < self.THRESHOLD:
            return False
        if self._last_alert_at is not None and now - self._last_alert_at < self.COOLDOWN:
            return False
        self._last_alert_at = now
        log.warning("%d downloads blocked by YouTube in %d min", len(self._blocked_at), self.WINDOW // 60)
        if self.settings.alerts_enabled:
            body = BODY.format(count=len(self._blocked_at), minutes=self.WINDOW // 60, detail=detail,
                               server=self.settings.public_base_url or "the server",
                               cooldown=self.COOLDOWN // 3600)
            threading.Thread(target=self._deliver, args=(body,), name="alert-email", daemon=True).start()
        return True

    def _deliver(self, body: str) -> None:
        try:
            self._send(self.settings, SUBJECT, body)
            log.info("Alert email sent to %s", self.settings.alert_email_to)
        except Exception as exc:  # an alert problem must never affect downloads
            log.warning("Could not send the alert email: %s: %s", exc.__class__.__name__, exc)


def main() -> int:
    settings = get_settings()
    if not settings.alerts_enabled:
        print("Alerts are off: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and ALERT_EMAIL_TO.")
        return 1
    try:
        send_email(settings, "Harmoniq: test alert", "This is a test. Alert emails from your server are working.\n")
    except Exception as exc:
        print(f"Failed: {exc.__class__.__name__}: {exc}")
        return 1
    print(f"Test email sent to {settings.alert_email_to}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
