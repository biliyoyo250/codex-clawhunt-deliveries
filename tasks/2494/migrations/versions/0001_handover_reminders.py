"""Reference handover model, scoped settings, persistent outbox and durable stub.

Revision ID: 2494_0001
Revises: None
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "2494_0001"
down_revision = None
branch_labels = None
depends_on = None


def business_fields():
    return [
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("site_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", pg.UUID(as_uuid=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    ]


def upgrade():
    op.create_table("hr_handover", *business_fields(),
        sa.Column("company_name", sa.String(200), nullable=False),
        sa.Column("consultant_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("consultant_name", sa.String(120), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("planned_move_in_date", sa.Date()),
        sa.Column("target_manager_id", pg.UUID(as_uuid=True)),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending_claim"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notified_levels", pg.JSONB(), nullable=False, server_default="[]"),
        sa.Column("urgent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("handover_duration_minutes", sa.Numeric(24, 9)),
        sa.Column("exceeded_24h", sa.Boolean()),
        sa.Column("claim_history", pg.JSONB(), nullable=False, server_default="[]"),
        sa.CheckConstraint("status IN ('pending_claim','returned_for_info','claimed','cancelled')", name="ck_hr_handover_status"),
        sa.CheckConstraint("status_epoch >= 0", name="ck_hr_handover_epoch"),
        sa.CheckConstraint("claimed_at IS NULL OR claimed_at >= generated_at", name="ck_hr_claim_time"),
        sa.CheckConstraint("status_changed_at >= generated_at", name="ck_hr_state_time"),
    )
    op.create_table("hr_reminder_config", *business_fields(),
        sa.Column("claim_reminder_hours", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("claim_escalation_hours", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("returned_info_hours", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("urgent_days", sa.Integer(), nullable=False, server_default="3"),
        sa.UniqueConstraint("org_id", "site_id", name="uq_hr_config_scope"),
        sa.CheckConstraint("claim_reminder_hours > 0 AND claim_escalation_hours > claim_reminder_hours", name="ck_hr_config_order"),
        sa.CheckConstraint("returned_info_hours > 0 AND urgent_days >= 0", name="ck_hr_config_positive"),
    )
    op.create_table("hr_notification_outbox", *business_fields(),
        sa.Column("handover_id", pg.UUID(as_uuid=True), sa.ForeignKey("hr_handover.id"), nullable=False),
        sa.Column("status_epoch", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(240), nullable=False),
        sa.Column("message", pg.JSONB(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.UniqueConstraint("handover_id", "status_epoch", "level", name="uq_hr_notification_epoch_level"),
        sa.UniqueConstraint("idempotency_key", name="uq_hr_outbox_key"),
        sa.CheckConstraint("attempts >= 0", name="ck_hr_outbox_attempts"),
    )
    op.create_table("hr_notification_receipt_stub", *business_fields(),
        sa.Column("idempotency_key", sa.String(240), nullable=False),
        sa.Column("message", pg.JSONB(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_hr_stub_notification_key"),
    )
    for table in ("hr_handover", "hr_reminder_config", "hr_notification_outbox", "hr_notification_receipt_stub"):
        for column in ("org_id", "site_id"):
            op.create_index(f"ix_{table}_{column}", table, [column])
    op.create_index("ix_hr_notification_outbox_handover_id", "hr_notification_outbox", ["handover_id"])
    op.create_index("ix_hr_outbox_due", "hr_notification_outbox", ["next_attempt_at"],
                    postgresql_where=sa.text("delivered_at IS NULL AND cancelled_at IS NULL AND deleted_at IS NULL"))
    op.execute("""
CREATE FUNCTION hr_guard_handover_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE at_time timestamptz;
BEGIN
  IF NEW.generated_at IS DISTINCT FROM OLD.generated_at THEN
    RAISE EXCEPTION 'Original handover generation time is immutable';
  END IF;
  IF NEW.org_id IS DISTINCT FROM OLD.org_id OR NEW.site_id IS DISTINCT FROM OLD.site_id THEN
    RAISE EXCEPTION 'Handover organization/site are immutable';
  END IF;
  IF NEW.status IS DISTINCT FROM OLD.status THEN
    at_time := COALESCE(NULLIF(current_setting('hr.transition_at', true), '')::timestamptz, clock_timestamp());
    IF at_time < OLD.status_changed_at OR at_time < OLD.generated_at THEN
      RAISE EXCEPTION 'State entry time cannot move backwards';
    END IF;
    NEW.status_changed_at := at_time;
    NEW.status_epoch := OLD.status_epoch + 1;
    NEW.notified_levels := '[]'::jsonb;
    NEW.urgent := false;
    NEW.updated_at := at_time;
    IF NEW.status = 'claimed' THEN
      IF OLD.status NOT IN ('pending_claim','returned_for_info') OR NEW.claimed_at IS NULL OR NEW.claimed_at < OLD.status_changed_at OR NEW.claimed_at < OLD.generated_at THEN
        RAISE EXCEPTION 'Claim requires an active unclaimed state and a valid claimed_at';
      END IF;
      NEW.handover_duration_minutes := round((extract(epoch FROM (NEW.claimed_at - OLD.generated_at)) / 60)::numeric, 9);
      NEW.exceeded_24h := (NEW.claimed_at - OLD.generated_at) > interval '24 hours';
      NEW.claim_history := COALESCE(OLD.claim_history, '[]'::jsonb) || jsonb_build_array(jsonb_build_object(
        'claimed_at', NEW.claimed_at, 'duration_minutes', NEW.handover_duration_minutes,
        'exceeded_24h', NEW.exceeded_24h, 'from_status_epoch', OLD.status_epoch));
    ELSIF NEW.status IN ('pending_claim','returned_for_info') THEN
      NEW.claimed_at := NULL;
      NEW.handover_duration_minutes := NULL;
      NEW.exceeded_24h := NULL;
      NEW.claim_history := OLD.claim_history;
    END IF;
    UPDATE hr_notification_outbox SET cancelled_at = at_time, updated_at = at_time
      WHERE handover_id = OLD.id AND delivered_at IS NULL AND cancelled_at IS NULL;
  ELSE
    -- Metadata edits cannot restart an epoch or erase the authoritative clock.
    NEW.status_epoch := OLD.status_epoch;
    NEW.status_changed_at := OLD.status_changed_at;
  END IF;
  IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
    NEW.urgent := false;
    UPDATE hr_notification_outbox SET cancelled_at = NEW.deleted_at, updated_at = NEW.deleted_at
      WHERE handover_id = OLD.id AND delivered_at IS NULL AND cancelled_at IS NULL;
  END IF;
  RETURN NEW;
END;
$$;
CREATE TRIGGER hr_handover_state_guard BEFORE UPDATE ON hr_handover
FOR EACH ROW EXECUTE FUNCTION hr_guard_handover_change();
""")


def downgrade():
    op.execute("DROP TRIGGER hr_handover_state_guard ON hr_handover")
    op.execute("DROP FUNCTION hr_guard_handover_change()")
    for table in ("hr_notification_receipt_stub", "hr_notification_outbox", "hr_reminder_config", "hr_handover"):
        op.drop_table(table)
