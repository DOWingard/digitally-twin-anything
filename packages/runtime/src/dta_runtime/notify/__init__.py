"""Notifications of the twin: CloudEvents delivered to a webhook, or logged."""

from .notifier import (
    BACKOFF_S,
    DELIVERED,
    DRAIN_S,
    DROPPED,
    EVENT_MEDIA,
    FAILED,
    KEPT,
    LOG,
    LOGGED,
    QUEUE_CAPACITY,
    QUEUED,
    SPEC_VERSION,
    TRIES,
    UNSENT,
    WEBHOOK,
    Notice,
    Notifier,
    notifier_for,
)

SYNC_FAILED = "twin.sync.failed"

__all__ = [
    "BACKOFF_S",
    "DELIVERED",
    "DRAIN_S",
    "DROPPED",
    "EVENT_MEDIA",
    "FAILED",
    "KEPT",
    "LOG",
    "LOGGED",
    "QUEUED",
    "QUEUE_CAPACITY",
    "SPEC_VERSION",
    "SYNC_FAILED",
    "TRIES",
    "UNSENT",
    "WEBHOOK",
    "Notice",
    "Notifier",
    "notifier_for",
]
