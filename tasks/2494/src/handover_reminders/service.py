"""Transactional application hooks. Caller owns the transaction for state/config writes."""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .clock import aware_utc
from .models import Handover, NotificationOutbox, ReminderConfig
from .rules import ACTIVE_UNCLAIMED, Thresholds, due_levels, notification_message


class NotFoundError(LookupError):
    pass


class PermissionDenied(PermissionError):
    pass


@dataclass(frozen=True)
class Actor:
    org_id: UUID
    site_id: UUID
    user_id: UUID
    roles: frozenset[str]


def require_scope(actor: Actor, org_id: UUID, site_id: UUID, *, write=False):
    if actor.org_id != org_id or actor.site_id != site_id:
        raise PermissionDenied("Principal is outside the requested organization/site")
    if write and "site_lead" not in actor.roles:
        raise PermissionDenied("Only the site lead can change reminder configuration")


def get_config(session: Session, org_id: UUID, site_id: UUID) -> Thresholds:
    row = session.scalar(select(ReminderConfig).where(
        ReminderConfig.org_id == org_id, ReminderConfig.site_id == site_id,
        ReminderConfig.deleted_at.is_(None)))
    if row is None:
        return Thresholds()
    return Thresholds(row.claim_reminder_hours, row.claim_escalation_hours,
                      row.returned_info_hours, row.urgent_days)


def update_config(session: Session, org_id: UUID, site_id: UUID, values: Thresholds,
                  actor: Actor, now: datetime) -> Thresholds:
    require_scope(actor, org_id, site_id, write=True)
    now = aware_utc(now)
    changes = asdict(values)
    session.execute(insert(ReminderConfig).values(
        id=uuid4(), org_id=org_id, site_id=site_id, created_at=now, updated_at=now,
        created_by=actor.user_id, **changes).on_conflict_do_update(
            constraint="uq_hr_config_scope",
            set_={**changes, "updated_at": now, "deleted_at": None}))
    return values


def _lock_handover(session, handover_id):
    row = session.scalar(select(Handover).where(
        Handover.id == handover_id, Handover.deleted_at.is_(None)).with_for_update()
        .execution_options(populate_existing=True))
    if row is None:
        raise NotFoundError("Handover was not found")
    return row


def _set_transition_clock(session, now):
    session.execute(text("SELECT set_config('hr.transition_at', :at, true)"),
                    {"at": aware_utc(now).isoformat()})


def transition_handover(session: Session, handover_id: UUID, new_status: str,
                        now: datetime, actor_id: UUID | None = None) -> Handover:
    """Change a status; the migration trigger owns epoch, reset and cancellation.

    A same-status retry is a no-op. Claiming has its own hook to capture duration.
    Trusted callers must authorize scope before calling this lower-level hook.
    """
    if new_status not in {"pending_claim", "returned_for_info", "cancelled"}:
        raise ValueError("Use claim_handover for a claim; otherwise use a known status")
    now = aware_utc(now)
    row = _lock_handover(session, handover_id)
    if row.status == new_status:
        return row
    if now < aware_utc(row.status_changed_at):
        raise ValueError("Transition time cannot precede current state entry")
    _set_transition_clock(session, now)
    row.status = new_status
    row.updated_at = now
    session.flush()
    session.refresh(row)
    return row


def claim_handover(session: Session, handover_id: UUID, claimed_at: datetime,
                   actor_id: UUID | None = None) -> Handover:
    """Idempotent claim. Original generated_at, not state_changed_at, is the baseline."""
    claimed_at = aware_utc(claimed_at)
    row = _lock_handover(session, handover_id)
    if row.status == "claimed":
        return row
    if row.status not in ACTIVE_UNCLAIMED:
        raise ValueError("Only an active unclaimed handover can be claimed")
    if claimed_at < aware_utc(row.status_changed_at) or claimed_at < aware_utc(row.generated_at):
        raise ValueError("Claim time cannot precede generation/current state entry")
    _set_transition_clock(session, claimed_at)
    row.claimed_at = claimed_at
    row.status = "claimed"
    row.updated_at = claimed_at
    session.flush()
    session.refresh(row)
    return row


def idempotency_key(handover: Handover, level: str) -> str:
    return (f"handover:{handover.org_id}:{handover.site_id}:{handover.id}:"
            f"epoch:{handover.status_epoch}:level:{level}")


def enqueue_due_notifications(session: Session, now: datetime, recipients) -> dict:
    """Claim eligible rows using PG row locks; queue unique epoch/level snapshots.

    All callers lock handover before outbox. Directory errors are visible and are
    retried on the next scan; they never mark a reminder as delivered.
    """
    now = aware_utc(now)
    rows = session.scalars(select(Handover).where(
        Handover.status.in_(ACTIVE_UNCLAIMED), Handover.claimed_at.is_(None),
        Handover.deleted_at.is_(None)).order_by(Handover.id).with_for_update(skip_locked=True)).all()
    result = {"examined": len(rows), "enqueued": 0, "directory_errors": []}
    for row in rows:
        config = get_config(session, row.org_id, row.site_id)
        levels = due_levels(row, config, now)
        # Urgent is a current condition, not a record of notification acceptance.
        row.urgent = "urgent" in levels
        row.updated_at = now
        for level in levels:
            if level in (row.notified_levels or []):
                continue
            try:
                message = notification_message(row, level, now, recipients)
            except (KeyError, ValueError):
                result["directory_errors"].append({"handover_id": str(row.id), "level": level,
                                                   "error": "recipient_not_configured"})
                continue
            inserted = session.execute(insert(NotificationOutbox).values(
                id=uuid4(), org_id=row.org_id, site_id=row.site_id,
                handover_id=row.id, status_epoch=row.status_epoch, level=level,
                idempotency_key=idempotency_key(row, level), message=message,
                attempts=0, next_attempt_at=now, created_at=now, updated_at=now
            ).on_conflict_do_nothing(constraint="uq_hr_notification_epoch_level")
              .returning(NotificationOutbox.id)).scalar_one_or_none()
            result["enqueued"] += inserted is not None
    return result


def dispatch_outbox(session_factory, gateway, now: datetime, *, limit=1000) -> dict:
    """Retry persisted deliveries. A gateway must deduplicate the supplied key.

    The handover lock is held across send + acknowledgement. A concurrent state
    change therefore either cancels before send or follows an accepted reminder.
    """
    now = aware_utc(now)
    with session_factory() as session:
        candidates = session.execute(select(NotificationOutbox.id, NotificationOutbox.handover_id).where(
            NotificationOutbox.delivered_at.is_(None), NotificationOutbox.cancelled_at.is_(None),
            NotificationOutbox.deleted_at.is_(None), NotificationOutbox.next_attempt_at <= now
        ).order_by(NotificationOutbox.created_at, NotificationOutbox.id).limit(limit)).all()
    result = {"delivered": 0, "failed": 0, "cancelled": 0, "skipped_locked": 0}
    for outbox_id, handover_id in candidates:
        with session_factory.begin() as session:
            row = session.scalar(select(Handover).where(Handover.id == handover_id)
                                 .with_for_update(skip_locked=True))
            if row is None:
                result["skipped_locked"] += 1
                continue
            event = session.scalar(select(NotificationOutbox).where(NotificationOutbox.id == outbox_id)
                                   .with_for_update(skip_locked=True))
            if event is None or event.delivered_at is not None or event.cancelled_at is not None or event.deleted_at is not None or aware_utc(event.next_attempt_at) > now:
                continue
            if (row.deleted_at is not None or row.status not in ACTIVE_UNCLAIMED or row.claimed_at is not None
                    or row.status_epoch != event.status_epoch
                    or row.org_id != event.org_id or row.site_id != event.site_id):
                event.cancelled_at = now
                event.updated_at = now
                result["cancelled"] += 1
                continue
            event.attempts += 1
            try:
                gateway.send(event.message, event.idempotency_key)
            except Exception:
                # Do not store exception text: an upstream error may contain credentials.
                event.last_error = "notification_gateway_failed"
                event.next_attempt_at = now + timedelta(minutes=min(30, 2 ** min(event.attempts - 1, 5)))
                event.updated_at = now
                result["failed"] += 1
                continue
            event.delivered_at = now
            event.updated_at = now
            event.last_error = None
            row.notified_levels = sorted(set(row.notified_levels or []) | {event.level})
            row.updated_at = now
            result["delivered"] += 1
    return result


def scan_handover_timeout(session_factory, gateway, recipients, clock) -> dict:
    now = aware_utc(clock.now())
    with session_factory.begin() as session:
        queued = enqueue_due_notifications(session, now, recipients)
    return {"scanned_at": now.isoformat(), **queued,
            **dispatch_outbox(session_factory, gateway, now)}
