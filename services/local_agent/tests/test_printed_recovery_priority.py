"""Real saved retry work must precede bulk local supply without extra quota."""
import asyncio
from datetime import timedelta
import threading
import time
import pytest
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.paper_printed_score_execution import PaperPrintedScoreExecutionService, WORK_TYPE
from test_paper_printed_score_execution import Gateway, setup
from test_paper_printed_score_retry import retry_setup, rows
from test_school_review_start_integrity_http import _http_client

def test_known_received_retry_completes_before_original_bulk_supply_barrier(tmp_path,monkeypatch):
    import home_study_agent.app as app_module
    db,_,aid,gateway,service,now=retry_setup(tmp_path)
    first=asyncio.run(service.process_next())
    assert first['status']=='blocked'
    first_row=rows(db)[0]
    now[0]+=timedelta(seconds=121)
    original_supply=app_module.refresh_local_question_supply
    entered=threading.Event();release=threading.Event();calls=[]
    def supply_barrier(*args,**kwargs):
        entered.set()
        assert release.wait(8),'original bulk barrier was not released'
        return original_supply(*args,**kwargs)
    monkeypatch.setattr(app_module,'refresh_local_question_supply',supply_barrier)
    app=create_app(Settings(data_dir=tmp_path,database_path=db.path,runtime_mode='trial',maintenance_enabled=True),model_gateway=gateway)
    execution=app.state.paper_printed_score_execution;execution.clock=lambda:now[0]
    original_batch=execution.process_batch
    async def observed_batch(limit=2,**kwargs):
        result=await original_batch(limit,**kwargs);calls.append((limit,kwargs.get('recovery_only',False),len(result)));return result
    monkeypatch.setattr(execution,'process_batch',observed_batch)
    try:
        with _http_client(app):
            try:
                assert entered.wait(8),'original maintenance never reached bulk supply'
                with db.connect()as c:view=PaperPrintedScoreExecutionService.for_rubric(c,'child',aid)
                assert view['status']=='completed' and view['attempt_number']==2
                assert view['parent_execution_id']==first['execution_id']
                assert len(gateway.requests)==2 and rows(db)[0]==first_row
                assert calls==[(2,True,1)]
            finally:release.set()
            end=time.monotonic()+8
            while len(calls)<2 and time.monotonic()<end:time.sleep(.02)
            assert calls==[(2,True,1),(1,False,0)]
            assert sum(row[2]for row in calls)<=2
    finally:release.set()
    assert len(gateway.requests)==2

def test_recovery_only_does_not_seed_new_or_retry_unknown_dispatch(tmp_path):
    db,_,_,gateway,service,now=retry_setup(tmp_path)
    assert asyncio.run(service.process_batch(2,recovery_only=True))==[]
    assert not rows(db) and not gateway.requests
    unknown=Gateway(error=SystemExit('synthetic unknown remote outcome'))
    service.gateway=unknown
    with pytest.raises(SystemExit):asyncio.run(service.process_next())
    service.recover();now[0]+=timedelta(days=1)
    before=rows(db)
    assert before[0]['status']=='blocked'
    assert asyncio.run(service.process_batch(2,recovery_only=True))==[]
    assert asyncio.run(service.process_next())is None
    assert len(unknown.requests)==1 and rows(db)==before

def test_fresh_frozen_rubric_is_consumed_in_same_ordinary_pass(tmp_path,monkeypatch):
    import home_study_agent.app as app_module
    db,_,aid,gateway,_=setup(tmp_path,Gateway())
    original_supply=app_module.refresh_local_question_supply;entered=threading.Event();supplied=threading.Event();calls=[]
    def original_supply_with_observation(*args,**kwargs):
        assert not rows(db) and not gateway.requests
        entered.set()
        result=original_supply(*args,**kwargs)
        supplied.set()
        return result
    monkeypatch.setattr(app_module,'refresh_local_question_supply',original_supply_with_observation)
    app=create_app(Settings(data_dir=tmp_path,database_path=db.path,runtime_mode='trial',maintenance_enabled=True),model_gateway=gateway)
    execution=app.state.paper_printed_score_execution;original_batch=execution.process_batch
    async def observed_batch(limit=2,**kwargs):
        result=await original_batch(limit,**kwargs);calls.append((limit,kwargs.get('recovery_only',False),len(result)));return result
    monkeypatch.setattr(execution,'process_batch',observed_batch)
    with _http_client(app):
        # Bound the original bulk phase separately from the subsequent scoring
        # phase. Both retain eight seconds; the exact same-pass/quota assertions remain.
        assert supplied.wait(8), 'original bulk supply did not complete'
        end=time.monotonic()+8
        while time.monotonic()<end:
            with db.connect()as c:view=PaperPrintedScoreExecutionService.for_rubric(c,'child',aid)
            if view and view['status']=='completed'and len(calls)==2:break
            time.sleep(.02)
        assert entered.is_set()
        assert view['status']=='completed'and view['attempt_number']==1
        assert calls==[(2,True,0),(2,False,1)]
        assert sum(row[2]for row in calls)<=2 and len(gateway.requests)==1

def test_cancellation_retains_unknown_original_dispatch_and_does_not_reissue(tmp_path):
    db,_,_,_,service,now=retry_setup(tmp_path)
    assert asyncio.run(service.process_next())['status']=='blocked'
    now[0]+=timedelta(seconds=121)
    class HoldGateway(Gateway):
        async def complete(self,request):
            result=await super().complete(request)
            entered.set();await hold.wait();return result
    async def run():
        nonlocal entered,hold
        entered=asyncio.Event();hold=asyncio.Event();gateway=HoldGateway();service.gateway=gateway
        task=asyncio.create_task(service.process_next(recovery_only=True))
        await asyncio.wait_for(entered.wait(),2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        saved=rows(db)[-1]
        assert saved['status']=='processing'
        service.recover()
        assert rows(db)[-1]['status']=='blocked'
        assert service.work(saved['artifact_id'])['error_code']=='PRINTED_REMOTE_OUTCOME_UNKNOWN'
        assert await service.process_batch(2,recovery_only=True)==[]
        assert len(gateway.requests)==1
    entered=hold=None
    asyncio.run(run())


def test_recovery_only_accepts_original_ready_receipt_without_another_dispatch(tmp_path,monkeypatch):
    db,_,_,gateway,service=setup(tmp_path,Gateway())
    original_accept=service._accept_ready
    def stopped_after_original_receipt(*args,**kwargs):
        raise SystemExit('synthetic interruption after original durable receipt')
    monkeypatch.setattr(service,'_accept_ready',stopped_after_original_receipt)
    with pytest.raises(SystemExit):asyncio.run(service.process_next())
    assert rows(db)[0]['status']=='response_ready'and len(gateway.requests)==1
    service.recover()
    monkeypatch.setattr(service,'_accept_ready',original_accept)
    recovered=asyncio.run(service.process_batch(2,recovery_only=True))
    assert len(recovered)==1 and recovered[0]['status']=='completed'
    assert recovered[0]['attempt_number']==1 and len(gateway.requests)==1


def test_recovery_only_rechecks_cooldown_of_preexisting_pending_successor(tmp_path):
    db,_,_,gateway,service,now=retry_setup(tmp_path)
    assert asyncio.run(service.process_next())['status']=='blocked'
    now[0]+=timedelta(seconds=121)
    assert service._claim(recovery_only=True)[2]=='processing'
    service.recover()
    assert rows(db)[-1]['status']=='pending'
    now[0]-=timedelta(seconds=120)
    before=rows(db)
    assert asyncio.run(service.process_batch(2,recovery_only=True))==[]
    assert rows(db)==before and len(gateway.requests)==1
    now[0]+=timedelta(seconds=120)
    assert asyncio.run(service.process_batch(2,recovery_only=True))[0]['status']=='completed'
    assert len(gateway.requests)==2
