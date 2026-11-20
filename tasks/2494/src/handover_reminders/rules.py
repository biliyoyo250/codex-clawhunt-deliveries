from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .clock import SHANGHAI, aware_utc
from .directory import Recipient, RecipientDirectory

ACTIVE_UNCLAIMED = {"pending_claim", "returned_for_info"}
LEVELS = {"pending_24", "pending_48", "urgent", "returned_48"}


@dataclass(frozen=True)
class Thresholds:
    claim_reminder_hours: int = 24
    claim_escalation_hours: int = 48
    returned_info_hours: int = 48
    urgent_days: int = 3

    def __post_init__(self):
        values = (self.claim_reminder_hours, self.claim_escalation_hours, self.returned_info_hours, self.urgent_days)
        if any(type(value) is not int for value in values):
            raise ValueError("Thresholds must be integers")
        if any(value > 2_147_483_647 for value in values):
            raise ValueError("Thresholds must fit a PostgreSQL integer")
        if self.claim_reminder_hours <= 0 or self.claim_escalation_hours <= self.claim_reminder_hours or self.returned_info_hours <= 0 or self.urgent_days < 0:
            raise ValueError("Require 0 < first < escalation, returned > 0, urgent >= 0")


def elapsed_minutes(start: datetime, end: datetime) -> Decimal:
    delta = aware_utc(end) - aware_utc(start)
    if delta.total_seconds() < 0:
        raise ValueError("Time cannot precede original generation")
    microseconds = ((delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds)
    return (Decimal(microseconds) / Decimal(60_000_000)).quantize(Decimal("0.000000001"))


def due_levels(handover, config: Thresholds, now: datetime) -> list[str]:
    now = aware_utc(now)
    if handover.deleted_at is not None or handover.status not in ACTIVE_UNCLAIMED or handover.claimed_at is not None:
        return []
    start = aware_utc(handover.status_changed_at)
    elapsed = now - start
    if elapsed < timedelta(0):
        return []
    levels = []
    if handover.status == "pending_claim":
        if elapsed >= timedelta(hours=config.claim_reminder_hours):
            levels.append("pending_24")
        if elapsed >= timedelta(hours=config.claim_escalation_hours):
            levels.append("pending_48")
    elif elapsed >= timedelta(hours=config.returned_info_hours):
        levels.append("returned_48")
    # Urgency is calendar-date based in Shanghai; overdue unclaimed cases remain urgent.
    if handover.planned_move_in_date is not None:
        days = (handover.planned_move_in_date - now.astimezone(SHANGHAI).date()).days
        if days <= config.urgent_days:
            levels.append("urgent")
    return levels


def notification_message(handover, level: str, now: datetime, directory: RecipientDirectory) -> dict:
    if level not in LEVELS:
        raise ValueError("Unknown reminder level")
    if level == "pending_24":
        recipient = (Recipient("user", str(handover.target_manager_id)) if handover.target_manager_id
                     else directory.manager_group(handover.org_id, handover.site_id))
    elif level in {"pending_48", "urgent"}:
        recipient = directory.site_lead(handover.org_id, handover.site_id)
    else:
        recipient = directory.consultant_supervisor(handover.org_id, handover.site_id, handover.consultant_id)
    wait = elapsed_minutes(handover.status_changed_at, now)
    return {
        "org_id": str(handover.org_id), "site_id": str(handover.site_id), "handover_id": str(handover.id),
        "status_epoch": handover.status_epoch, "level": level, "recipient": recipient.as_dict(),
        "urgent": level == "urgent", "company_name": handover.company_name,
        "consultant_id": str(handover.consultant_id), "consultant_name": handover.consultant_name,
        "signed_at": aware_utc(handover.signed_at).isoformat(),
        "planned_move_in_date": handover.planned_move_in_date.isoformat() if handover.planned_move_in_date else None,
        "wait_duration_minutes": str(wait), "wait_duration_hours": str(wait / Decimal(60)),
        "generated_at": aware_utc(handover.generated_at).isoformat(),
        "state_entered_at": aware_utc(handover.status_changed_at).isoformat(),
    }

