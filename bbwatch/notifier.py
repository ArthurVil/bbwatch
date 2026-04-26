"""Push notification abstractions for the viewer connectivity watchdog."""

import json
import logging
import urllib.request
from typing import Protocol

from bbwatch.config import NotifierType, WatchdogConfig

LOGGER = logging.getLogger(__name__)


class Notifier(Protocol):
    """Protocol for sending a one-shot alert message."""

    def send(self, message: str) -> None:
        """Send message. Logs on failure — never raises."""
        ...


class NtfyNotifier:
    """Sends push notifications via ntfy (ntfy.sh or self-hosted)."""

    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, message: str) -> None:
        try:
            req = urllib.request.Request(
                self.url,
                data=message.encode(),
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            LOGGER.info(f"ntfy notification sent to {self.url}")
        except Exception as e:
            LOGGER.error(f"ntfy notification failed: {e}")


class WebhookNotifier:
    """Sends a JSON POST to an arbitrary webhook URL."""

    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, message: str) -> None:
        try:
            payload = json.dumps({"message": message}).encode()
            req = urllib.request.Request(
                self.url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5)
            LOGGER.info(f"Webhook notification sent to {self.url}")
        except Exception as e:
            LOGGER.error(f"Webhook notification failed: {e}")


class NullNotifier:
    """No-op notifier when watchdog notifications are disabled."""

    def send(self, message: str) -> None:
        pass


def make_notifier(config: WatchdogConfig) -> Notifier:
    """Construct the right Notifier from config."""
    if config.notifier == NotifierType.NTFY:
        return NtfyNotifier(config.ntfy_url)
    if config.notifier == NotifierType.WEBHOOK:
        return WebhookNotifier(config.webhook_url)
    return NullNotifier()
