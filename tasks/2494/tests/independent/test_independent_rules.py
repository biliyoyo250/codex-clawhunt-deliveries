"""Independent QA by /root/opportunities; not production-author tests.
Oracle: task/bid/root-confirmed semantics recorded in ../qa-plan.json before code read.
These tests exercise pure rules/clock/routing only; they do NOT establish PostgreSQL
concurrency, persistence, migration, authorization or recovery acceptance.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from handover_reminders.clock import FrozenClock, next_scan_after
from handover_reminders.directory import Recipient
from handover_reminders.rules import Thresholds, due_levels, elapsed_minutes, notification_message

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 16, 30, tzinfo=UTC)  # Shanghai Nov21 00:30
ORG = UUID('00000000-0000-0000-0000-000000000001')
SITE = UUID('00000000-0000-0000-0000-000000000011')
CONSULTANT = UUID('00000000-0000-0000-0000-000000000031')
TARGET = UUID('00000000-0000-0000-0000-000000000041')
LEAD = UUID('00000000-0000-0000-0000-000000000051')
SUPERVISOR = UUID('00000000-0000-0000-0000-000000000061')


def handover(*, age=timedelta(hours=1), **overrides):
    values = dict(
        id=UUID('00000000-0000-0000-0000-000000000071'),
        org_id=ORG, site_id=SITE, consultant_id=CONSULTANT,
        company_name='Synthetic Firm 2494', consultant_name='Synthetic Adviser',
        signed_at=NOW-timedelta(days=5), generated_at=NOW-timedelta(days=4),
        status='pending_claim', status_changed_at=NOW-age, status_epoch=7,
        target_manager_id=None, planned_move_in_date=date(2026, 12, 1),
        deleted_at=None, claimed_at=None, notified_levels=[], urgent=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingDirectory:
    """Independent identity oracle with deliberately different role recipients."""
    def __init__(self):
        self.calls = []

    def manager_group(self, org, site):
        self.calls.append(('group', org, site))
        assert (org, site) == (ORG, SITE)
        return Recipient('group', 'synthetic-site-11-member-managers')

    def site_lead(self, org, site):
        self.calls.append(('lead', org, site))
        assert (org, site) == (ORG, SITE)
        return Recipient('user', str(LEAD))

    def consultant_supervisor(self, org, site, consultant):
        self.calls.append(('supervisor', org, site, consultant))
        assert (org, site, consultant) == (ORG, SITE, CONSULTANT)
        return Recipient('user', str(SUPERVISOR))


@pytest.mark.parametrize('age,expected', [
    (timedelta(hours=24)-timedelta(microseconds=1), []),
    (timedelta(hours=24), ['pending_24']),
    (timedelta(hours=24)+timedelta(microseconds=1), ['pending_24']),
    (timedelta(hours=48)-timedelta(microseconds=1), ['pending_24']),
    (timedelta(hours=48), ['pending_24', 'pending_48']),
    (timedelta(hours=48)+timedelta(microseconds=1), ['pending_24', 'pending_48']),
])
def test_independent_pending_exact_boundaries(age, expected):
    assert due_levels(handover(age=age), Thresholds(), NOW) == expected


@pytest.mark.parametrize('age,expected', [
    (timedelta(hours=48)-timedelta(microseconds=1), []),
    (timedelta(hours=48), ['returned_48']),
    (timedelta(hours=48)+timedelta(microseconds=1), ['returned_48']),
])
def test_independent_returned_boundary_excludes_pending_tiers(age, expected):
    assert due_levels(handover(age=age, status='returned_for_info'), Thresholds(), NOW) == expected


@pytest.mark.parametrize('state', ['pending_claim', 'returned_for_info'])
@pytest.mark.parametrize('planned,expected', [
    (date(2026, 11, 20), ['urgent']),  # overdue is included
    (date(2026, 11, 21), ['urgent']),
    (date(2026, 11, 24), ['urgent']),  # exactly3Shanghai calendar days
    (date(2026, 11, 25), []),
])
def test_independent_urgency_calendar_days_and_both_unclaimed_states(state, planned, expected):
    assert due_levels(handover(status=state, planned_move_in_date=planned), Thresholds(), NOW) == expected


def test_independent_shanghai_midnight_crosses_urgency_boundary():
    before = datetime(2026, 11, 20, 15, 59, 59, tzinfo=UTC)
    after = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)
    h = handover(status_changed_at=before-timedelta(hours=1), planned_move_in_date=date(2026,11,24))
    assert due_levels(h, Thresholds(), before) == []
    assert due_levels(h, Thresholds(), after) == ['urgent']


def test_independent_48h_and_urgent_are_separate_levels():
    h = handover(age=timedelta(hours=48), planned_move_in_date=date(2026,11,24))
    assert due_levels(h, Thresholds(), NOW) == ['pending_24', 'pending_48', 'urgent']


@pytest.mark.parametrize('changes', [
    {'status':'claimed'}, {'status':'cancelled'},
    {'deleted_at':NOW-timedelta(minutes=1)}, {'claimed_at':NOW-timedelta(minutes=1)},
])
def test_independent_terminal_deleted_or_already_claimed_never_due(changes):
    h = handover(age=timedelta(days=5), planned_move_in_date=date(2026,11,20), **changes)
    assert due_levels(h, Thresholds(), NOW) == []


@pytest.mark.parametrize('level,target,expected,expected_call', [
    ('pending_24', None, {'kind':'group','id':'synthetic-site-11-member-managers'}, 'group'),
    ('pending_24', TARGET, {'kind':'user','id':str(TARGET)}, None),
    ('pending_48', TARGET, {'kind':'user','id':str(LEAD)}, 'lead'),
    ('urgent', TARGET, {'kind':'user','id':str(LEAD)}, 'lead'),
    ('returned_48', TARGET, {'kind':'user','id':str(SUPERVISOR)}, 'supervisor'),
])
def test_independent_role_routing_and_exclusive_target(level, target, expected, expected_call):
    h = handover(age=timedelta(hours=48), target_manager_id=target)
    directory = RecordingDirectory()
    message = notification_message(h, level, NOW, directory)
    assert message['recipient'] == expected
    assert [call[0] for call in directory.calls] == ([] if expected_call is None else [expected_call])


def test_independent_48h_message_contains_all_five_information_classes():
    h = handover(age=timedelta(hours=48))
    message = notification_message(h, 'pending_48', NOW, RecordingDirectory())
    assert message['company_name'] == 'Synthetic Firm 2494'
    assert message['consultant_name'] == 'Synthetic Adviser'
    assert datetime.fromisoformat(message['signed_at']) == h.signed_at
    assert message['planned_move_in_date'] == '2026-12-01'
    assert Decimal(message['wait_duration_minutes']) == Decimal(2880)
    assert Decimal(message['wait_duration_hours']) == Decimal(48)
    assert message['org_id'] == str(ORG) and message['site_id'] == str(SITE)
    assert message['status_epoch'] == 7
    assert message['urgent'] is False


def test_independent_urgent_payload_has_true_flag():
    assert notification_message(handover(), 'urgent', NOW, RecordingDirectory())['urgent'] is True


def test_independent_configured_rules_are_used_not_hardcoded_defaults():
    config = Thresholds(claim_reminder_hours=1, claim_escalation_hours=2, returned_info_hours=3, urgent_days=1)
    h = handover(age=timedelta(hours=2), planned_move_in_date=date(2026,11,23))
    assert due_levels(h, config, NOW) == ['pending_24', 'pending_48']
    h.status = 'returned_for_info'
    assert due_levels(h, config, NOW) == []
    h.status_changed_at = NOW-timedelta(hours=3)
    assert due_levels(h, config, NOW) == ['returned_48']


@pytest.mark.parametrize('given,expected', [
    (datetime(2026,11,20,15,59,59,tzinfo=UTC), datetime(2026,11,20,16,0,tzinfo=UTC)),
    (datetime(2026,11,20,16,0,tzinfo=UTC), datetime(2026,11,20,16,30,tzinfo=UTC)),
    (datetime(2026,11,20,16,29,59,999999,tzinfo=UTC), datetime(2026,11,20,16,30,tzinfo=UTC)),
    (datetime(2026,11,20,16,30,tzinfo=UTC), datetime(2026,11,20,17,0,tzinfo=UTC)),
    (datetime(2026,11,21,0,0,tzinfo=timezone(timedelta(hours=8))), datetime(2026,11,20,16,30,tzinfo=UTC)),
])
def test_independent_schedule_next_shanghai_halfhour(given, expected):
    assert next_scan_after(given) == expected


@pytest.mark.parametrize('seconds,expected', [(90, Decimal('1.5')), (259200, Decimal('4320'))])
def test_independent_duration_exact_known_values(seconds, expected):
    assert elapsed_minutes(NOW, NOW+timedelta(seconds=seconds)) == expected


def test_independent_negative_duration_rejected():
    with pytest.raises(ValueError):
        elapsed_minutes(NOW, NOW-timedelta(seconds=1))


def test_independent_clock_preserves_instant_and_rejects_naive():
    assert FrozenClock(datetime(2026,11,21,0,30,tzinfo=timezone(timedelta(hours=8)))).now() == NOW
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026,11,21,0,30)).now()
