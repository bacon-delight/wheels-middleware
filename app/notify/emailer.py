"""Send lifecycle emails via SES (SESv2) in the SES region (ap-south-1)."""

from __future__ import annotations

from collections.abc import Iterable

from ..config import Settings, get_settings
from .templates import RenderedEmail, render


class Emailer:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("sesv2", region_name=self.settings.ses_region)
        return self._client

    def send(self, *, to: str | Iterable[str], template: str, **ctx) -> RenderedEmail:
        email = render(template, **ctx)
        addresses = [to] if isinstance(to, str) else list(to)
        self._get_client().send_email(
            FromEmailAddress=self.settings.from_email,
            Destination={"ToAddresses": addresses},
            Content={
                "Simple": {
                    "Subject": {"Data": email.subject, "Charset": "UTF-8"},
                    "Body": {
                        "Html": {"Data": email.html, "Charset": "UTF-8"},
                        "Text": {"Data": email.text, "Charset": "UTF-8"},
                    },
                }
            },
        )
        return email
