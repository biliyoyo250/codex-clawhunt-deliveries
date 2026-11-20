from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from handover_reminders.api import ConfigBody, create_app
from handover_reminders.clock import FrozenClock, aware_utc, next_scan_after
from handover_reminders.directory import StaticRecipientDirectory
from handover_reminders.rules import Thresholds, due_levels, elapsed_minutes, notification_message

NOW = datetime(2026, 1, 2, 16, 0, tzinfo=timezone.utc)  # Shanghai Jan 3 midnight
ORG, SITE, USER, LEAD, CONSULTANT = [UUID(int=x) for x in range(1, 6)]


def fixture_row(**overrides):
    fields = dict(id=UUID(int=10), org_id=ORG, site_id=SITE, company_name="Fictional Alpha",
                  consultant_id=CONSULTANT, consultant_name="Fictional Consultant",
                  signed_at=NOW - timedelta(days=3), generated_at=NOW - timedelta(hours=60),
                  status_changed_at=NOW - timedelta(hours=48), status_epoch=0,
                  status="pending_claim", claimed_at=None, deleted_at=None,
                  target_manager_id=None, planned_move_in_date=date(2026, 1, 6))
    return SimpleNamespace(**(fields | overrides))


@pytest.mark.parametrize("hours,expected", [(23.99, []), (24, ["pending_24"]),
    (47.99, ["pending_24"]), (48, ["pending_24", "pending_48"])])
def test_exact_reminder_boundaries(hours, expected):
    row = fixture_row(status_changed_at=NOW - timedelta(hours=hours), planned_move_in_date=None)
    assert due_levels(row, Thresholds(), NOW) == expected


@pytest.mark.parametrize("planned,urgent", [(date(2026, 1, 6), True), (date(2026, 1, 7), False),
    (date(2025, 12, 30), True), (None, False)])
@pytest.mark.parametrize("status", ["pending_claim", "returned_for_info"])
def test_shanghai_calendar_urgency(planned, urgent, status):
    row = fixture_row(status=status, status_changed_at=NOW, planned_move_in_date=planned)
    assert ("urgent" in due_levels(row, Thresholds(), NOW)) is urgent


def test_payload_and_designated_user():
    directory = StaticRecipientDirectory(groups={(ORG, SITE): "fictional-manager-group"},
        leads={(ORG, SITE): LEAD}, supervisors={(ORG, SITE, CONSULTANT): USER})
    row = fixture_row(target_manager_id=USER)
    message = notification_message(row, "pending_24", NOW, directory)
    assert message["recipient"] == {"kind": "user", "id": str(USER)}
    for key in ("company_name", "consultant_name", "signed_at", "planned_move_in_date", "wait_duration_minutes"):
        assert message[key] is not None
    assert notification_message(row, "pending_48", NOW, directory)["recipient"]["id"] == str(LEAD)
    assert notification_message(row, "returned_48", NOW, directory)["recipient"]["id"] == str(USER)


def test_naive_clocks_rejected_and_duration_exact():
    with pytest.raises(ValueError):
        aware_utc(datetime(2026, 1, 1))
    assert elapsed_minutes(NOW - timedelta(hours=25, minutes=30), NOW) == Decimal("1530.000000000")
    with pytest.raises(ValueError):
        elapsed_minutes(NOW, NOW - timedelta(microseconds=1))
    assert FrozenClock(NOW).now() == NOW


@pytest.mark.parametrize("at,expected", [(NOW, NOW + timedelta(minutes=30)),
    (NOW + timedelta(minutes=29, seconds=59), NOW + timedelta(minutes=30)),
    (NOW + timedelta(minutes=30), NOW + timedelta(hours=1))])
def test_half_hour_alignment(at, expected):
    assert next_scan_after(at) == expected


@pytest.mark.parametrize("values", [{"claim_reminder_hours": 0}, {"claim_escalation_hours": 24},
    {"urgent_days": -1}, {"returned_info_hours": True}, {"urgent_days": 2147483648}])
def test_invalid_configuration_rejected(values):
    with pytest.raises(ValueError):
        Thresholds(**values)
    with pytest.raises(ValueError):
        ConfigBody(**values)


def test_api_fails_closed_without_trusted_principal():
    client = TestClient(create_app(None, FrozenClock(NOW)))
    path = f"/orgs/{ORG}/sites/{SITE}/handover-reminder-config"
    assert client.get(path, headers={"X-Role": "site_lead"}).status_code == 401
    assert client.put(path, json={}, headers={"X-Role": "site_lead"}).status_code == 401
    assert client.get("/health").status_code == 200
