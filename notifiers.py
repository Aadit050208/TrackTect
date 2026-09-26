"""Pluggable notification interface.

When the triage agent flags a high-priority change, `notify_all()` fans the
alert out to every registered notifier. New channels (Slack, email, SMS, ...)
are added by subclassing `Notifier` and appending to `get_notifiers()` --
the core pipeline never changes.
"""

import logging
import smtplib
from abc import ABC, abstractmethod
from email.mime.text import MIMEText
from typing import Dict, List

import requests

from config import settings

logger = logging.getLogger(__name__)


class Notifier(ABC):
    """Base class for alert channels.

    `send()` receives an alert dict:
        {"competitor": str, "severity": str, "message": str, "url": str}
    and returns True if delivery succeeded.
    """

    name = "base"

    @abstractmethod
    def send(self, alert: Dict) -> bool:
        ...


class LogNotifier(Notifier):
    """Always-on notifier that writes alerts to the application log."""

    name = "log"

    def send(self, alert: Dict) -> bool:
        logger.warning(
            "ALERT [%s] %s: %s", alert.get("severity", "high"),
            alert.get("competitor", "?"), alert.get("message", ""),
        )
        return True


class WebhookNotifier(Notifier):
    """POSTs the alert as JSON to NOTIFY_WEBHOOK_URL."""

    name = "webhook"

    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, alert: Dict) -> bool:
        payload = {
            "text": f"[TrackTect] {alert.get('severity', 'high').upper()} — "
                    f"{alert.get('competitor', '?')}: {alert.get('message', '')}",
            **{k: v for k, v in alert.items() if k != "digest_content"},
        }
        try:
            response = requests.post(self.url, json=payload, timeout=10)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as exc:
            logger.warning("Webhook notification failed: %s", exc.__class__.__name__)
            return False


class EmailNotifier(Notifier):
    """SMTP email notifier — only registered when SMTP_* env vars are set."""

    name = "email"

    def send(self, alert: Dict) -> bool:
        subject = f"[TrackTect] {alert.get('severity', 'alert')} — {alert.get('competitor', '')}"
        body = alert.get("digest_content") or (
            f"{alert.get('message', '')}\n\n{alert.get('url', '')}"
        )
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from
        msg["To"] = settings.smtp_to
        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                if settings.smtp_user and settings.smtp_password:
                    smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.sendmail(settings.smtp_from, [settings.smtp_to], msg.as_string())
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Email notification failed: %s", exc.__class__.__name__)
            return False


def get_notifiers() -> List[Notifier]:
    """Build the active notifier list from configuration."""
    notifiers: List[Notifier] = [LogNotifier()]
    if settings.notify_webhook_url:
        notifiers.append(WebhookNotifier(settings.notify_webhook_url))
    if settings.email_configured:
        notifiers.append(EmailNotifier())
    return notifiers


def notify_all(alert: Dict) -> None:
    """Send an alert through every configured channel; never raises."""
    for notifier in get_notifiers():
        try:
            notifier.send(alert)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Notifier %s raised: %s", notifier.name, exc)
