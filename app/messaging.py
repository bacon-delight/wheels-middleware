"""SQS enqueue + EventBridge lifecycle events (pipeline transport)."""

from __future__ import annotations

import json
import os
from typing import Any

from .config import get_settings

LIFECYCLE_SOURCE = "wheels.lifecycle"


def _sqs():
    import boto3

    return boto3.client("sqs", region_name=get_settings().core_region)


def _events():
    import boto3

    return boto3.client("events", region_name=get_settings().core_region)


def enqueue(queue_url: str, body: dict[str, Any]) -> None:
    _sqs().send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))


def enqueue_ingest(body: dict[str, Any]) -> None:
    url = os.getenv("INGEST_QUEUE_URL")
    if url:
        enqueue(url, body)


def enqueue_extract(body: dict[str, Any]) -> None:
    url = os.getenv("EXTRACT_QUEUE_URL")
    if url:
        enqueue(url, body)


def emit_lifecycle_event(detail_type: str, detail: dict[str, Any]) -> None:
    """Emit a transition event; the Notify Lambda fans it out to the right party via SES."""
    bus = os.getenv("EVENT_BUS_NAME")
    if not bus:
        return
    _events().put_events(
        Entries=[
            {
                "Source": LIFECYCLE_SOURCE,
                "DetailType": detail_type,
                "Detail": json.dumps(detail),
                "EventBusName": bus,
            }
        ]
    )
