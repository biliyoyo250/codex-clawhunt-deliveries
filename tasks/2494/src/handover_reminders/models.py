from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Integer, Numeric, String, Text, UniqueConstraint, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class BusinessFields:
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    org_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    site_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    created_by: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Handover(BusinessFields, Base):
    """Reference adapter model; generated_at maps to the upstream original creation time."""
    __tablename__ = "hr_handover"
    __table_args__ = (
        CheckConstraint("status IN ('pending_claim','returned_for_info','claimed','cancelled')", name="ck_hr_handover_status"),
        CheckConstraint("status_epoch >= 0", name="ck_hr_handover_epoch"),
        CheckConstraint("claimed_at IS NULL OR claimed_at >= generated_at", name="ck_hr_claim_time"),
        CheckConstraint("status_changed_at >= generated_at", name="ck_hr_state_time"),
    )
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    consultant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    consultant_name: Mapped[str] = mapped_column(String(120), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_move_in_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    target_manager_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_claim", server_default="pending_claim")
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    notified_levels: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    urgent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    handover_duration_minutes: Mapped[Decimal | None] = mapped_column(Numeric(24, 9), nullable=True)
    exceeded_24h: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    claim_history: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")


class ReminderConfig(BusinessFields, Base):
    __tablename__ = "hr_reminder_config"
    __table_args__ = (
        UniqueConstraint("org_id", "site_id", name="uq_hr_config_scope"),
        CheckConstraint("claim_reminder_hours > 0 AND claim_escalation_hours > claim_reminder_hours", name="ck_hr_config_order"),
        CheckConstraint("returned_info_hours > 0 AND urgent_days >= 0", name="ck_hr_config_positive"),
    )
    claim_reminder_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24, server_default="24")
    claim_escalation_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=48, server_default="48")
    returned_info_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=48, server_default="48")
    urgent_days: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")


class NotificationOutbox(BusinessFields, Base):
    __tablename__ = "hr_notification_outbox"
    __table_args__ = (
        UniqueConstraint("handover_id", "status_epoch", "level", name="uq_hr_notification_epoch_level"),
        UniqueConstraint("idempotency_key", name="uq_hr_outbox_key"),
        CheckConstraint("attempts >= 0", name="ck_hr_outbox_attempts"),
        Index("ix_hr_outbox_due", "next_attempt_at", postgresql_where=text("delivered_at IS NULL AND cancelled_at IS NULL AND deleted_at IS NULL")),
    )
    handover_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("hr_handover.id"), nullable=False, index=True)
    status_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    message: Mapped[dict] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NotificationReceipt(BusinessFields, Base):
    """Durable notification API STUB. No FK: its separate commit models external acceptance."""
    __tablename__ = "hr_notification_receipt_stub"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_hr_stub_notification_key"),)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    message: Mapped[dict] = mapped_column(JSONB, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

