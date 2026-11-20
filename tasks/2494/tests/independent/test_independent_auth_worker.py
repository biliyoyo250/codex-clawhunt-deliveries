"""Independent local HTTP/auth and scheduler orchestration, not PG acceptance."""
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from handover_reminders.api import create_app
from handover_reminders.clock import FrozenClock
from handover_reminders.rules import Thresholds
from handover_reminders.service import Actor, PermissionDenied, require_scope
import handover_reminders.worker as worker

NOW=datetime(2026,11,20,16,7,tzinfo=timezone.utc)
ORG=UUID('00000000-0000-0000-0000-000000000001')
SITE=UUID('00000000-0000-0000-0000-000000000011')
USER=UUID('00000000-0000-0000-0000-000000000021')


class NoDatabaseAllowed:
    def __call__(self,*args,**kwargs):
        raise AssertionError('An unverified/invalid request must not access DB')
    def begin(self,*args,**kwargs):
        raise AssertionError('An unverified/invalid request must not access DB')


@pytest.mark.parametrize('method,suffix,body',[
    ('get','/handover-reminder-config',None),
    ('put','/handover-reminder-config',{'claim_reminder_hours':24,'claim_escalation_hours':48,'returned_info_hours':48,'urgent_days':3}),
    ('post','/handovers/00000000-0000-0000-0000-000000000071/claim',None),
    ('post','/handovers/00000000-0000-0000-0000-000000000071/transition',{'status':'cancelled'}),
])
def test_independent_default_identity_is_fail_closed_despite_role_headers(method,suffix,body):
    app=create_app(NoDatabaseAllowed(),FrozenClock(NOW))
    with TestClient(app) as client:
        response=client.request(method,f'/orgs/{ORG}/sites/{SITE}'+suffix,
            json=body,headers={'X-Org-Id':str(ORG),'X-Site-Id':str(SITE),'X-Roles':'site_lead,handover_operator'})
    assert response.status_code==401


@pytest.mark.parametrize('field',['claim_reminder_hours','claim_escalation_hours','returned_info_hours','urgent_days'])
def test_independent_config_pg_integer_overflow_rejected_before_db(field):
    actor=Actor(ORG,SITE,USER,frozenset({'site_lead'}))
    app=create_app(NoDatabaseAllowed(),FrozenClock(NOW),principal_provider=lambda:actor)
    body={'claim_reminder_hours':24,'claim_escalation_hours':48,'returned_info_hours':48,'urgent_days':3}
    body[field]=2147483648
    with TestClient(app) as client:
        response=client.put(f'/orgs/{ORG}/sites/{SITE}/handover-reminder-config',json=body)
    assert response.status_code==422


@pytest.mark.parametrize('field',['claim_reminder_hours','claim_escalation_hours','returned_info_hours','urgent_days'])
def test_independent_threshold_domain_rejects_pg_integer_overflow(field):
    body={'claim_reminder_hours':24,'claim_escalation_hours':48,'returned_info_hours':48,'urgent_days':3}
    body[field]=2147483648
    with pytest.raises(ValueError):Thresholds(**body)


@pytest.mark.parametrize('kind',['nonlead','wrongorg','wrongsite'])
def test_independent_config_scope_guard(kind):
    actor={'nonlead':Actor(ORG,SITE,USER,frozenset({'member_manager'})),
           'wrongorg':Actor(uuid4(),SITE,USER,frozenset({'site_lead'})),
           'wrongsite':Actor(ORG,uuid4(),USER,frozenset({'site_lead'}))}[kind]
    with pytest.raises(PermissionDenied):require_scope(actor,ORG,SITE,write=True)


def test_independent_actual_worker_loop_catches_failure_and_keeps_shanghai_schedule(monkeypatch,caplog):
    clock=FrozenClock(NOW)
    instants=[];delays=[]
    def fake_business_scan(sessions,gateway,recipients,clock):
        # Only scan business logic is replaced: this test proves real worker orchestration.
        instants.append(clock.now())
        if len(instants)==1:raise RuntimeError('QA_SYNTHETIC_ERROR_MUST_NOT_BE_LOGGED')
        return {'independent_scheduler_test':True}
    def frozen_sleep(seconds):
        delays.append(seconds)
        clock.value=clock.now()+timedelta(seconds=seconds)
    monkeypatch.setattr(worker,'scan_handover_timeout',fake_business_scan)
    worker.run_worker(None,None,None,clock,stop=lambda:len(instants)>=3,sleep=frozen_sleep)
    assert instants==[NOW,datetime(2026,11,20,16,30,tzinfo=timezone.utc),datetime(2026,11,20,17,0,tzinfo=timezone.utc)]
    assert delays==[1380,1800,1800]
    assert 'handover_scan_failed' in caplog.text
    assert 'QA_SYNTHETIC_ERROR_MUST_NOT_BE_LOGGED' not in caplog.text
