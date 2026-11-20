"""Independent PG16 integration/regression QA by /root/opportunities.
Expected behavior originates in qa-plan.json, task/bid and root clarifications.
Run only against the explicitly configured synthetic test database AFTER Alembic
upgrade head. Never create_all/SQLite: migration triggers are part of acceptance.
This file can be copied unchanged to the delivery's independent/ directory.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import sessionmaker

from handover_reminders.api import create_app
from handover_reminders.clock import FrozenClock
from handover_reminders.directory import StaticRecipientDirectory
from handover_reminders.models import Handover, NotificationOutbox, NotificationReceipt, ReminderConfig
from handover_reminders.notifications import DatabaseNotificationStub
from handover_reminders.rules import Thresholds
from handover_reminders.service import (Actor, claim_handover, dispatch_outbox,
    enqueue_due_notifications, get_config, scan_handover_timeout, transition_handover,
    update_config)

pytestmark = pytest.mark.pg16
UTC = timezone.utc
NOW = datetime(2026,11,20,16,30,tzinfo=UTC)  # Shanghai Nov21 00:30


@pytest.fixture
def pg():
    url = (os.environ.get('QA2494_DATABASE_URL') or os.environ.get('TEST_DATABASE_URL')
           or os.environ.get('DATABASE_URL'))
    if not url:
        if os.environ.get('REQUIRE_POSTGRES') == '1':
            pytest.fail('CI requires PostgreSQL16 but no explicit test database URL was supplied')
        pytest.skip('Independent persistence/concurrency QA requires real migrated PG16; no local PG claim')
    if url.startswith('postgresql://'):
        url = url.replace('postgresql://','postgresql+psycopg://',1)
    if not url.startswith('postgresql+psycopg://'):
        pytest.fail('Independent PG16 acceptance does not accept SQLite or an alternate database')
    engine = create_engine(url, pool_pre_ping=True,
        connect_args={'options':'-c statement_timeout=20000 -c lock_timeout=10000'})
    sessions = sessionmaker(engine, expire_on_commit=False)
    with engine.connect() as connection:
        version = int(connection.execute(text("SHOW server_version_num")).scalar_one())
        assert 160000 <= version < 170000, 'Acceptance requires actual PostgreSQL16'
        assert connection.execute(text('SELECT version_num FROM alembic_version')).first(), 'Run Alembic migration first'
        assert connection.execute(text("SELECT 1 FROM pg_trigger WHERE tgname='hr_handover_state_guard' AND NOT tgisinternal")).first(), 'Migration state/epoch trigger is required'
    org, other_org, site, second_site, other_site = [uuid4() for _ in range(5)]
    adviser, target, lead, supervisor, user = [uuid4() for _ in range(5)]
    directory = StaticRecipientDirectory(
        groups={(org,site):'qa-group-primary',(org,second_site):'qa-group-second',(other_org,other_site):'qa-group-other'},
        leads={(org,site):lead,(org,second_site):uuid4(),(other_org,other_site):uuid4()},
        supervisors={(org,site,adviser):supervisor,(org,second_site,adviser):uuid4(),(other_org,other_site,adviser):uuid4()},
    )
    ctx = SimpleNamespace(engine=engine, sessions=sessions, org=org, other_org=other_org,
        site=site, second_site=second_site, other_site=other_site, adviser=adviser,
        target=target, lead=lead, supervisor=supervisor, user=user, directory=directory)
    try:
        yield ctx
    finally:
        # Only rows carrying this test's fresh UUID tenant IDs are removed.
        with sessions.begin() as session:
            for model in (NotificationReceipt,NotificationOutbox,ReminderConfig,Handover):
                session.execute(delete(model).where(model.org_id.in_([org,other_org])))
        engine.dispose()


def seed(pg, *, age=timedelta(hours=24), status='pending_claim', target=False,
         planned=date(2026,12,1), generated=None, org=None, site=None, deleted=False):
    generated = generated or NOW-timedelta(days=10)
    row = Handover(id=uuid4(), org_id=org or pg.org, site_id=site or pg.site,
        company_name='Synthetic QA2494 Company', consultant_id=pg.adviser,
        consultant_name='Synthetic QA2494 Adviser', signed_at=generated-timedelta(minutes=1),
        planned_move_in_date=planned, target_manager_id=pg.target if target else None,
        status=status, generated_at=generated, status_changed_at=NOW-age,
        status_epoch=0, notified_levels=[], urgent=False, created_at=generated,
        updated_at=NOW-age, deleted_at=NOW-timedelta(minutes=1) if deleted else None)
    with pg.sessions.begin() as session:
        session.add(row)
    return row.id


def getrow(pg, hid):
    with pg.sessions() as session:
        return session.get(Handover,hid)


def events(pg, hid):
    with pg.sessions() as session:
        return list(session.scalars(select(NotificationOutbox).where(NotificationOutbox.handover_id==hid)))


def receipts(pg, hid):
    with pg.sessions() as session:
        rows = session.scalars(select(NotificationReceipt).where(NotificationReceipt.org_id.in_([pg.org,pg.other_org]))).all()
        return [x for x in rows if x.message.get('handover_id')==str(hid)]


def scan(pg, at=NOW):
    clock=FrozenClock(at)
    return scan_handover_timeout(pg.sessions,DatabaseNotificationStub(pg.sessions,clock),pg.directory,clock)


@pytest.mark.parametrize('age,levels', [
    (timedelta(hours=24)-timedelta(microseconds=1),set()),
    (timedelta(hours=24),{'pending_24'}),
    (timedelta(hours=48)-timedelta(microseconds=1),{'pending_24'}),
    (timedelta(hours=48),{'pending_24','pending_48'}),
])
def test_pg_independent_thresholds_and_persisted_repeat(pg,age,levels):
    hid=seed(pg,age=age)
    scan(pg);scan(pg);scan(pg)
    assert {x.message['level'] for x in receipts(pg,hid)}==levels
    assert len(receipts(pg,hid))==len(levels)
    assert set(getrow(pg,hid).notified_levels)==levels
    assert len(events(pg,hid))==len(levels)


@pytest.mark.parametrize('target', [False,True])
def test_pg_independent_target_vs_group_and_48_payload(pg,target):
    hid=seed(pg,age=timedelta(hours=48),target=target)
    scan(pg)
    bylevel={x.message['level']:x.message for x in receipts(pg,hid)}
    assert bylevel['pending_24']['recipient']==({'kind':'user','id':str(pg.target)} if target else {'kind':'group','id':'qa-group-primary'})
    message=bylevel['pending_48']
    assert message['recipient']=={'kind':'user','id':str(pg.lead)}
    assert message['company_name']=='Synthetic QA2494 Company'
    assert message['consultant_name']=='Synthetic QA2494 Adviser'
    assert datetime.fromisoformat(message['signed_at'])==NOW-timedelta(days=10)-timedelta(minutes=1)
    assert message['planned_move_in_date']=='2026-12-01'
    assert Decimal(message['wait_duration_minutes'])==Decimal(2880)


@pytest.mark.parametrize('status',['pending_claim','returned_for_info'])
@pytest.mark.parametrize('planned,urgent',[(date(2026,11,20),True),(date(2026,11,24),True),(date(2026,11,25),False)])
def test_pg_independent_shanghai_urgent_persists_both_states(pg,status,planned,urgent):
    hid=seed(pg,age=timedelta(hours=1),status=status,planned=planned)
    scan(pg);scan(pg)
    rs=receipts(pg,hid)
    assert getrow(pg,hid).urgent is urgent
    assert len(rs)==int(urgent)
    if urgent:
        assert rs[0].message['level']=='urgent' and rs[0].message['urgent'] is True
        assert rs[0].message['recipient']=={'kind':'user','id':str(pg.lead)}


def test_pg_independent_same_lead_48_and_urgent_are_distinct_levels(pg):
    hid=seed(pg,age=timedelta(hours=48),planned=date(2026,11,24))
    scan(pg);scan(pg)
    rs=receipts(pg,hid)
    assert len(rs)==3 and {x.message['level'] for x in rs}=={'pending_24','pending_48','urgent'}
    assert sum(x.message['recipient']['id']==str(pg.lead) for x in rs)==2


@pytest.mark.parametrize('age,expected',[(timedelta(hours=48)-timedelta(microseconds=1),0),(timedelta(hours=48),1)])
def test_pg_independent_returned_supervisor_boundary(pg,age,expected):
    hid=seed(pg,age=age,status='returned_for_info')
    scan(pg)
    rs=receipts(pg,hid)
    assert len(rs)==expected
    if expected:
        assert rs[0].message['level']=='returned_48'
        assert rs[0].message['recipient']=={'kind':'user','id':str(pg.supervisor)}


def test_pg_independent_concurrent_scanners_single_durable_event(pg):
    hid=seed(pg)
    barrier=Barrier(4)
    def work():
        barrier.wait(timeout=10)
        return scan(pg)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures=[executor.submit(work) for _ in range(4)]
        for future in futures:future.result(timeout=30)
    assert len(events(pg,hid))==1
    assert len(receipts(pg,hid))==1
    assert getrow(pg,hid).notified_levels==['pending_24']


def test_pg_independent_concurrent_dispatchers_do_not_duplicate(pg):
    hid=seed(pg)
    with pg.sessions.begin() as session:enqueue_due_notifications(session,NOW,pg.directory)
    barrier=Barrier(3)
    def work():
        barrier.wait(timeout=10)
        return dispatch_outbox(pg.sessions,DatabaseNotificationStub(pg.sessions,FrozenClock(NOW)),NOW)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures=[executor.submit(work) for _ in range(3)]
        for future in futures:future.result(timeout=30)
    assert len(events(pg,hid))==1 and events(pg,hid)[0].delivered_at is not None
    assert len(receipts(pg,hid))==1


class FailOneTarget:
    def __init__(self,base,hid,after_accept=False):
        self.base=base;self.hid=str(hid);self.after_accept=after_accept;self.failed=False;self.keys=[]
    def send(self,message,key):
        if message['handover_id']!=self.hid:return self.base.send(message,key)
        self.keys.append(key)
        if not self.failed:
            self.failed=True
            if self.after_accept:self.base.send(message,key)
            raise RuntimeError('Synthetic QA delivery crash')
        return self.base.send(message,key)


@pytest.mark.parametrize('after_accept',[False,True])
def test_pg_independent_delivery_failure_restart_stable_key(pg,after_accept):
    hid=seed(pg)
    with pg.sessions.begin() as session:enqueue_due_notifications(session,NOW,pg.directory)
    before=events(pg,hid)[0]
    failing=FailOneTarget(DatabaseNotificationStub(pg.sessions,FrozenClock(NOW)),hid,after_accept)
    dispatch_outbox(pg.sessions,failing,NOW)
    assert len(receipts(pg,hid))==int(after_accept)
    pending=events(pg,hid)[0]
    assert pending.delivered_at is None and getrow(pg,hid).notified_levels==[]
    # New gateway/factory wrapper represents restarted application state.
    restarted=sessionmaker(pg.engine,expire_on_commit=False)
    later=NOW+timedelta(minutes=2)
    dispatch_outbox(restarted,DatabaseNotificationStub(restarted,FrozenClock(later)),later)
    dispatch_outbox(restarted,DatabaseNotificationStub(restarted,FrozenClock(later)),later)
    after=events(pg,hid)
    assert len(after)==1 and after[0].id==before.id
    assert after[0].idempotency_key==before.idempotency_key==failing.keys[0]
    assert after[0].delivered_at is not None
    assert len(receipts(pg,hid))==1
    assert getrow(pg,hid).notified_levels==['pending_24']
    if after_accept:assert receipts(pg,hid)[0].accepted_at==NOW


def test_pg_independent_committed_outbox_survives_service_restart(pg):
    hid=seed(pg)
    with pg.sessions.begin() as session:enqueue_due_notifications(session,NOW,pg.directory)
    old=events(pg,hid)[0]
    assert receipts(pg,hid)==[]
    fresh=sessionmaker(pg.engine,expire_on_commit=False)
    dispatch_outbox(fresh,DatabaseNotificationStub(fresh,FrozenClock(NOW)),NOW)
    assert events(pg,hid)[0].id==old.id
    assert len(receipts(pg,hid))==1


@pytest.mark.parametrize('change',['returned_for_info','claim','delete'])
def test_pg_independent_transition_cancels_old_epoch_pending_delivery(pg,change):
    hid=seed(pg)
    with pg.sessions.begin() as session:enqueue_due_notifications(session,NOW,pg.directory)
    old=events(pg,hid)[0]
    with pg.sessions.begin() as session:
        if change=='claim':claim_handover(session,hid,NOW,pg.user)
        elif change=='delete':session.get(Handover,hid).deleted_at=NOW
        else:transition_handover(session,hid,change,NOW,pg.user)
    assert events(pg,hid)[0].cancelled_at is not None
    dispatch_outbox(pg.sessions,DatabaseNotificationStub(pg.sessions,FrozenClock(NOW)),NOW)
    assert receipts(pg,hid)==[]
    if change=='returned_for_info':
        row=getrow(pg,hid)
        assert row.status_epoch==old.status_epoch+1 and row.status_changed_at==NOW
        assert row.notified_levels==[]
        scan(pg,NOW+timedelta(hours=48))
        rs=receipts(pg,hid)
        assert len(rs)==1 and rs[0].message['level']=='returned_48'


def test_pg_independent_new_epoch_rearms_but_same_state_does_not(pg):
    hid=seed(pg)
    scan(pg)
    old_clock=getrow(pg,hid).status_changed_at
    old_key=events(pg,hid)[0].idempotency_key
    with pg.sessions.begin() as session:transition_handover(session,hid,'pending_claim',NOW,pg.user)
    assert getrow(pg,hid).status_changed_at==old_clock
    scan(pg)
    assert len(receipts(pg,hid))==1
    with pg.sessions.begin() as session:transition_handover(session,hid,'returned_for_info',NOW,pg.user)
    with pg.sessions.begin() as session:transition_handover(session,hid,'pending_claim',NOW+timedelta(minutes=1),pg.user)
    assert getrow(pg,hid).notified_levels==[]
    scan(pg,NOW+timedelta(minutes=1,hours=24)-timedelta(microseconds=1))
    assert len(receipts(pg,hid))==1
    scan(pg,NOW+timedelta(minutes=1,hours=24))
    assert len(receipts(pg,hid))==2
    keys={x.idempotency_key for x in events(pg,hid)}
    assert old_key in keys and len(keys)==2


def test_pg_independent_claim_uses_original_generation_not_state_clock(pg):
    hid=seed(pg,age=timedelta(hours=1),generated=NOW-timedelta(hours=72))
    with pg.sessions.begin() as session:claim_handover(session,hid,NOW,pg.user)
    row=getrow(pg,hid)
    assert row.handover_duration_minutes==Decimal(4320)
    assert row.claimed_at==NOW and row.exceeded_24h is True
    scan(pg,NOW+timedelta(days=2))
    assert receipts(pg,hid)==[]


@pytest.mark.parametrize('extra,exceeded',[(timedelta(0),False),(timedelta(microseconds=1),True)])
def test_pg_independent_claim_exceeded24_is_strict(pg,extra,exceeded):
    age=timedelta(hours=24)+extra
    hid=seed(pg,age=age,generated=NOW-age)
    with pg.sessions.begin() as session:claim_handover(session,hid,NOW,pg.user)
    row=getrow(pg,hid)
    assert row.exceeded_24h is exceeded
    if not extra:assert row.handover_duration_minutes==Decimal(1440)


def test_pg_independent_soft_deleted_due_row_is_skipped(pg):
    hid=seed(pg,age=timedelta(hours=80),planned=date(2026,11,20),deleted=True)
    scan(pg)
    assert events(pg,hid)==[] and receipts(pg,hid)==[]


def test_pg_independent_config_defaults_scoped_update_and_effect(pg):
    with pg.sessions() as session:assert get_config(session,pg.org,pg.site)==Thresholds()
    actor=Actor(pg.org,pg.site,pg.user,frozenset({'site_lead'}))
    with pg.sessions.begin() as session:
        update_config(session,pg.org,pg.site,Thresholds(1,2,3,1),actor,NOW)
    with pg.sessions() as session:
        assert get_config(session,pg.org,pg.site)==Thresholds(1,2,3,1)
        assert get_config(session,pg.org,pg.second_site)==Thresholds()
    h1=seed(pg,age=timedelta(hours=2))
    h2=seed(pg,age=timedelta(hours=2),site=pg.second_site)
    scan(pg)
    assert {x.message['level'] for x in receipts(pg,h1)}=={'pending_24','pending_48'}
    assert receipts(pg,h2)==[]


@pytest.mark.parametrize('principal',['nonlead','other_org','other_site'])
def test_pg_independent_config_denied_requests_never_mutate(pg,principal):
    leader=Actor(pg.org,pg.site,pg.user,frozenset({'site_lead'}))
    with pg.sessions.begin() as session:update_config(session,pg.org,pg.site,Thresholds(),leader,NOW)
    actor={'nonlead':Actor(pg.org,pg.site,uuid4(),frozenset({'member_manager'})),
           'other_org':Actor(pg.other_org,pg.site,uuid4(),frozenset({'site_lead'})),
           'other_site':Actor(pg.org,pg.second_site,uuid4(),frozenset({'site_lead'}))}[principal]
    app=create_app(pg.sessions,FrozenClock(NOW),principal_provider=lambda:actor)
    with TestClient(app) as client:
        response=client.put(f'/orgs/{pg.org}/sites/{pg.site}/handover-reminder-config',
            json={'claim_reminder_hours':1,'claim_escalation_hours':2,'returned_info_hours':3,'urgent_days':1})
    assert response.status_code==403
    with pg.sessions() as session:
        rows=list(session.scalars(select(ReminderConfig).where(ReminderConfig.org_id.in_([pg.org,pg.other_org]))))
        assert len(rows)==1
        assert get_config(session,pg.org,pg.site)==Thresholds()


def test_pg_independent_authorized_config_http_and_overflow_rejected(pg):
    actor=Actor(pg.org,pg.site,pg.user,frozenset({'site_lead'}))
    app=create_app(pg.sessions,FrozenClock(NOW),principal_provider=lambda:actor)
    path=f'/orgs/{pg.org}/sites/{pg.site}/handover-reminder-config'
    with TestClient(app) as client:
        valid=client.put(path,json={'claim_reminder_hours':1,'claim_escalation_hours':2,'returned_info_hours':3,'urgent_days':1})
        assert valid.status_code==200
        assert client.get(path).json()=={'claim_reminder_hours':1,'claim_escalation_hours':2,'returned_info_hours':3,'urgent_days':1}
        invalid=client.put(path,json={'claim_reminder_hours':2147483648,'claim_escalation_hours':2147483649,'returned_info_hours':3,'urgent_days':1})
        assert invalid.status_code==422
    with pg.sessions() as session:assert get_config(session,pg.org,pg.site)==Thresholds(1,2,3,1)


def test_pg_independent_stale_preload_cannot_claim_after_other_session_cancel(pg):
    hid=seed(pg)
    with pg.sessions() as a:
        stale=a.get(Handover,hid)
        assert stale.status=='pending_claim'
        with pg.sessions.begin() as other:transition_handover(other,hid,'cancelled',NOW,pg.user)
        with pytest.raises(ValueError):claim_handover(a,hid,NOW+timedelta(minutes=1),pg.user)
        a.rollback()
    row=getrow(pg,hid)
    assert row.status=='cancelled' and row.claimed_at is None


def test_pg_independent_stale_preload_second_claim_is_idempotent(pg):
    hid=seed(pg)
    with pg.sessions() as a:
        stale=a.get(Handover,hid)
        assert stale.status=='pending_claim'
        with pg.sessions.begin() as other:claim_handover(other,hid,NOW,pg.user)
        before=getrow(pg,hid)
        result=claim_handover(a,hid,NOW+timedelta(minutes=1),pg.user)
        assert result.claimed_at==NOW
        a.commit()
    after=getrow(pg,hid)
    assert after.claimed_at==before.claimed_at==NOW
    assert after.handover_duration_minutes==before.handover_duration_minutes
    assert after.status_epoch==before.status_epoch and after.claim_history==before.claim_history


def test_pg_independent_stale_preload_rejects_transition_clock_regression(pg):
    hid=seed(pg)
    with pg.sessions() as a:
        stale=a.get(Handover,hid)
        assert stale.status=='pending_claim'
        with pg.sessions.begin() as other:transition_handover(other,hid,'returned_for_info',NOW+timedelta(minutes=10),pg.user)
        with pytest.raises(ValueError):transition_handover(a,hid,'pending_claim',NOW+timedelta(minutes=5),pg.user)
        a.rollback()
    row=getrow(pg,hid)
    assert row.status=='returned_for_info' and row.status_changed_at==NOW+timedelta(minutes=10)


@pytest.mark.parametrize('kind,expected',[
    ('operator',200),('nonoperator',403),('other_org',403),('other_site',403),('foreign_row',404),
])
def test_pg_independent_claim_http_role_scope_guards(pg,kind,expected):
    hid=seed(pg,age=timedelta(hours=1),generated=NOW-timedelta(hours=72))
    actor=Actor(pg.org,pg.site,pg.user,frozenset({'handover_operator'}))
    target=hid
    if kind=='nonoperator':actor=Actor(pg.org,pg.site,pg.user,frozenset({'member_manager'}))
    if kind=='other_org':actor=Actor(pg.other_org,pg.site,pg.user,frozenset({'site_lead'}))
    if kind=='other_site':actor=Actor(pg.org,pg.second_site,pg.user,frozenset({'site_lead'}))
    if kind=='foreign_row':target=seed(pg,org=pg.other_org,site=pg.other_site)
    app=create_app(pg.sessions,FrozenClock(NOW),principal_provider=lambda:actor)
    with TestClient(app) as client:
        response=client.post(f'/orgs/{pg.org}/sites/{pg.site}/handovers/{target}/claim')
    assert response.status_code==expected
    row=getrow(pg,hid)
    if expected==200:
        assert row.status=='claimed' and row.handover_duration_minutes==Decimal(4320)
    else:
        assert row.status=='pending_claim' and row.claimed_at is None
        assert getrow(pg,target).claimed_at is None


class SyntheticAcknowledgementCrash(BaseException):
    """A process-like crash deliberately outside dispatch's except Exception."""


def test_pg_independent_hard_crash_after_accept_rolls_back_ack_transaction(pg):
    hid=seed(pg)
    with pg.sessions.begin() as session:enqueue_due_notifications(session,NOW,pg.directory)
    original=events(pg,hid)[0]
    durable=DatabaseNotificationStub(pg.sessions,FrozenClock(NOW))
    class CrashAfterIndependentAccept:
        def send(self,message,key):
            durable.send(message,key)
            if message['handover_id']==str(hid):
                raise SyntheticAcknowledgementCrash('synthetic process interruption after external acceptance')
    with pytest.raises(SyntheticAcknowledgementCrash):
        dispatch_outbox(pg.sessions,CrashAfterIndependentAccept(),NOW)
    # The external acceptance committed separately, but ALL local ack writes rolled back.
    after_crash=events(pg,hid)[0]
    assert len(receipts(pg,hid))==1
    assert after_crash.id==original.id and after_crash.idempotency_key==original.idempotency_key
    assert after_crash.attempts==0 and after_crash.delivered_at is None
    assert after_crash.last_error is None
    assert getrow(pg,hid).notified_levels==[]
    restarted=sessionmaker(pg.engine,expire_on_commit=False)
    later=NOW+timedelta(seconds=1)
    dispatch_outbox(restarted,DatabaseNotificationStub(restarted,FrozenClock(later)),later)
    recovered=events(pg,hid)[0]
    assert recovered.id==original.id and recovered.idempotency_key==original.idempotency_key
    assert recovered.attempts==1 and recovered.delivered_at==later
    assert len(receipts(pg,hid))==1 and receipts(pg,hid)[0].accepted_at==NOW
    assert getrow(pg,hid).notified_levels==['pending_24']
