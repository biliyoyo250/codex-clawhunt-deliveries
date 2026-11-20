from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from handover_reminders.api import create_app
from handover_reminders.clock import FrozenClock
from handover_reminders.directory import StaticRecipientDirectory
from handover_reminders.models import Handover, NotificationOutbox, NotificationReceipt
from handover_reminders.notifications import DatabaseNotificationStub
from handover_reminders.rules import Thresholds
from handover_reminders.service import (Actor, PermissionDenied, claim_handover, dispatch_outbox,
    enqueue_due_notifications, get_config, scan_handover_timeout, transition_handover, update_config)

pytestmark = pytest.mark.pg16
NOW = datetime(2026, 1, 2, 16, tzinfo=timezone.utc)
ORG, SITE, MANAGER, LEAD, CONSULTANT, SUPERVISOR = [UUID(int=x) for x in range(1, 7)]


def directory():
    return StaticRecipientDirectory(groups={(ORG, SITE): "fictional-group"},
        leads={(ORG, SITE): LEAD}, supervisors={(ORG, SITE, CONSULTANT): SUPERVISOR})


def add_handover(sessions, **kwargs):
    values = dict(id=uuid4(), org_id=ORG, site_id=SITE, company_name="Fictional Alpha",
        consultant_id=CONSULTANT, consultant_name="Fictional Consultant", signed_at=NOW-timedelta(days=3),
        generated_at=NOW-timedelta(hours=60), status_changed_at=NOW-timedelta(hours=48),
        planned_move_in_date=date(2026,1,6), status="pending_claim", status_epoch=0)
    values.update(kwargs)
    with sessions.begin() as session:
        row = Handover(**values)
        session.add(row)
    return values["id"]


def records(sessions, model):
    with sessions() as session:
        return session.scalars(select(model)).all()


def test_late_scan_catches_all_tiers_and_persists_once(pg_sessions):
    item = add_handover(pg_sessions, target_manager_id=MANAGER)
    clock=FrozenClock(NOW)
    gateway=DatabaseNotificationStub(pg_sessions, clock)
    first=scan_handover_timeout(pg_sessions,gateway,directory(),clock)
    second=scan_handover_timeout(pg_sessions,gateway,directory(),clock)
    assert first["enqueued"] == first["delivered"] == 3
    assert second["enqueued"] == second["delivered"] == 0
    receipts=records(pg_sessions,NotificationReceipt)
    assert len(receipts)==3
    messages={r.message["level"]:r.message for r in receipts}
    assert messages["pending_24"]["recipient"] == {"kind":"user","id":str(MANAGER)}
    assert messages["pending_48"]["recipient"]["id"]==str(LEAD)
    assert messages["pending_48"]["wait_duration_minutes"]=="2880.000000000"
    with pg_sessions() as s:
        row=s.get(Handover,item)
        assert set(row.notified_levels)=={"pending_24","pending_48","urgent"}
        assert row.urgent is True


def test_state_epoch_resets_and_returned_supervisor(pg_sessions):
    item=add_handover(pg_sessions,planned_move_in_date=None)
    with pg_sessions.begin() as s:
        assert enqueue_due_notifications(s,NOW,directory())["enqueued"]==2
    with pg_sessions.begin() as s:
        row=transition_handover(s,item,"returned_for_info",NOW)
        assert row.status_epoch==1 and row.status_changed_at==NOW and row.notified_levels==[]
    gateway=DatabaseNotificationStub(pg_sessions,FrozenClock(NOW))
    assert dispatch_outbox(pg_sessions,gateway,NOW)["delivered"]==0
    later=FrozenClock(NOW+timedelta(hours=48))
    assert scan_handover_timeout(pg_sessions,gateway,directory(),later)["delivered"]==1
    assert records(pg_sessions,NotificationReceipt)[0].message["recipient"]["id"]==str(SUPERVISOR)
    with pg_sessions.begin() as s:
        transition_handover(s,item,"pending_claim",later.now())
    again=FrozenClock(later.now()+timedelta(hours=24))
    assert scan_handover_timeout(pg_sessions,gateway,directory(),again)["delivered"]==1
    assert len(records(pg_sessions,NotificationReceipt))==2


@pytest.mark.parametrize("elapsed,exceeded", [(timedelta(hours=24),False),
    (timedelta(hours=24,microseconds=1),True), (timedelta(hours=48,minutes=30),True)])
def test_claim_original_generation_and_strict_24h(pg_sessions,elapsed,exceeded):
    item=add_handover(pg_sessions,generated_at=NOW-elapsed,status_changed_at=NOW-timedelta(hours=1))
    with pg_sessions.begin() as s:
        row=claim_handover(s,item,NOW)
        expected=Decimal(str(elapsed.total_seconds()))/Decimal(60)
        assert abs(row.handover_duration_minutes-expected)<Decimal("0.000000001")
        assert row.exceeded_24h is exceeded
        assert len(row.claim_history)==1
    with pg_sessions.begin() as s:
        row=claim_handover(s,item,NOW+timedelta(hours=1))
        assert row.claimed_at==NOW and len(row.claim_history)==1


def test_reopened_claim_preserves_history(pg_sessions):
    item=add_handover(pg_sessions)
    with pg_sessions.begin() as s:
        claim_handover(s,item,NOW)
        reopened=transition_handover(s,item,"pending_claim",NOW+timedelta(hours=1))
        assert reopened.claimed_at is None and len(reopened.claim_history)==1
        claimed=claim_handover(s,item,NOW+timedelta(hours=2))
        assert len(claimed.claim_history)==2 and claimed.handover_duration_minutes==Decimal("3720")


@pytest.mark.parametrize("accepted_first", [False,True])
def test_delivery_failure_and_crash_window_recover(pg_sessions,accepted_first):
    add_handover(pg_sessions,planned_move_in_date=None,status_changed_at=NOW-timedelta(hours=24))
    durable=DatabaseNotificationStub(pg_sessions,FrozenClock(NOW))
    class FailOnce:
        first=True
        def send(self,message,key):
            if self.first:
                self.first=False
                if accepted_first:
                    durable.send(message,key)
                raise RuntimeError("synthetic failure")
            durable.send(message,key)
    gateway=FailOnce()
    result=scan_handover_timeout(pg_sessions,gateway,directory(),FrozenClock(NOW))
    assert result["failed"]==1 and result["delivered"]==0
    assert len(records(pg_sessions,NotificationReceipt))==int(accepted_first)
    assert records(pg_sessions,Handover)[0].notified_levels==[]
    result=scan_handover_timeout(pg_sessions,gateway,directory(),FrozenClock(NOW+timedelta(minutes=2)))
    assert result["delivered"]==1 and len(records(pg_sessions,NotificationReceipt))==1
    event=records(pg_sessions,NotificationOutbox)[0]
    assert event.attempts==2 and event.delivered_at is not None and event.last_error is None


def test_concurrent_scanners_accept_each_key_once(pg_sessions):
    add_handover(pg_sessions)
    barrier=Barrier(2)
    gateway=DatabaseNotificationStub(pg_sessions,FrozenClock(NOW))
    def scan():
        barrier.wait(timeout=5)
        return scan_handover_timeout(pg_sessions,gateway,directory(),FrozenClock(NOW))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:scan(),range(2)))
    scan_handover_timeout(pg_sessions,gateway,directory(),FrozenClock(NOW))
    assert len(records(pg_sessions,NotificationReceipt))==3
    assert len(records(pg_sessions,NotificationOutbox))==3
    assert all(r["failed"]==0 for r in results)


def test_soft_deleted_row_not_queued(pg_sessions):
    add_handover(pg_sessions,deleted_at=NOW)
    assert scan_handover_timeout(pg_sessions,DatabaseNotificationStub(pg_sessions),directory(),FrozenClock(NOW))["enqueued"]==0


def test_scoped_configuration_and_api(pg_sessions):
    lead=Actor(ORG,SITE,LEAD,frozenset({"site_lead"}))
    with pg_sessions.begin() as s:
        assert get_config(s,ORG,SITE)==Thresholds()
        update_config(s,ORG,SITE,Thresholds(12,36,30,2),lead,NOW)
    with pg_sessions.begin() as s:
        for actor in [Actor(UUID(int=99),SITE,LEAD,lead.roles),Actor(ORG,UUID(int=98),LEAD,lead.roles),
                      Actor(ORG,SITE,MANAGER,frozenset({"manager"}))]:
            with pytest.raises(PermissionDenied):
                update_config(s,ORG,SITE,Thresholds(),actor,NOW)
        assert get_config(s,ORG,SITE)==Thresholds(12,36,30,2)
    client=TestClient(create_app(pg_sessions,FrozenClock(NOW),lambda:lead))
    path=f"/orgs/{ORG}/sites/{SITE}/handover-reminder-config"
    assert client.put(path,json={"claim_reminder_hours":8,"claim_escalation_hours":16}).status_code==200
    assert client.put(path,json={"claim_reminder_hours":2147483648,"claim_escalation_hours":2147483649}).status_code==422
    assert client.put(path.replace(str(SITE),str(UUID(int=99))),json={}).status_code==403


def test_preloaded_identity_is_refreshed_under_lock(pg_sessions):
    item=add_handover(pg_sessions)
    with pg_sessions() as stale:
        row=stale.get(Handover,item)
        assert row.status=="pending_claim"
        with pg_sessions.begin() as other:
            transition_handover(other,item,"cancelled",NOW)
        with pytest.raises(ValueError,match="active unclaimed"):
            claim_handover(stale,item,NOW+timedelta(minutes=1))
        stale.rollback()


def test_direct_sql_cannot_restart_same_state_or_move_scope(pg_sessions):
    item=add_handover(pg_sessions)
    with pg_sessions.begin() as s:
        s.execute(text("UPDATE hr_handover SET status_epoch=99, status_changed_at=:now WHERE id=:id"),{"id":item,"now":NOW})
        row=s.get(Handover,item)
        assert row.status_epoch==0 and row.status_changed_at==NOW-timedelta(hours=48)
    with pytest.raises(Exception):
        with pg_sessions.begin() as s:
            s.execute(text("UPDATE hr_handover SET org_id=:org WHERE id=:id"),{"id":item,"org":UUID(int=999)})
