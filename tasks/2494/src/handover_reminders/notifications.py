"""The stub is durable and local. Replace this boundary with platform notification API."""
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import insert

from .clock import SystemClock, aware_utc
from .models import NotificationReceipt


class NotificationGateway(Protocol):
    def send(self, message: dict, idempotency_key: str) -> None:
        """Return only after durable acceptance; same key must never accept twice."""


class DatabaseNotificationStub:
    """Separate commit intentionally models the send/ack crash window.

    Receipt has no FK back to the locked handover. Do not use an outer scanner
    session here: its rollback must not erase an already accepted notification.
    """
    def __init__(self, session_factory, clock=None):
        self.session_factory = session_factory
        self.clock = clock or SystemClock()

    def send(self, message: dict, idempotency_key: str) -> None:
        now = aware_utc(self.clock.now())
        with self.session_factory.begin() as session:
            session.execute(insert(NotificationReceipt).values(
                id=uuid4(), org_id=UUID(message["org_id"]), site_id=UUID(message["site_id"]),
                idempotency_key=idempotency_key, message=message, accepted_at=now,
                created_at=now, updated_at=now
            ).on_conflict_do_nothing(constraint="uq_hr_stub_notification_key"))
