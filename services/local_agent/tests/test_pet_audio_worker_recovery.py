import asyncio
import threading

import pytest

from home_study_agent.pet_audio_work import PetAudioTeachingService
from test_pet_audio_work import audio_case, loopback, enqueue
from test_pet_media_evidence import media, CHILD, change


def reopen(service):
    return PetAudioTeachingService(service.database,media_root=service.media_root,
        data_encryption_key=service.data_encryption_key,gateway=service.gateway,runtime_epoch='restarted-worker')


def test_queued_original_reopens_and_dispatches_once_with_explicit_delivery(audio_case,loopback):
    service=audio_case[0];wid=enqueue(audio_case);restarted=reopen(service);delivered=[]
    async def delivery(turn): delivered.append(turn)
    asyncio.run(restarted.process_pending(delivery))
    assert restarted.read(CHILD,wid)['state']=='response_saved'
    assert len(delivered)==1 and len(loopback['posts'])==1
    asyncio.run(restarted.process_pending(delivery))
    assert len(loopback['posts'])==1


@pytest.mark.parametrize('fault',['paused','archived','deleted','revoked'])
def test_queued_source_change_does_not_send_or_deliver(audio_case,loopback,fault):
    service,_,_,grant,_,_=audio_case;wid=enqueue(audio_case)
    if fault=='paused':change(service.database,"UPDATE daily_session SET observation_status='paused'")
    elif fault=='archived':change(service.database,"UPDATE child_profile SET status='archived' WHERE child_id=?",(CHILD,))
    elif fault=='deleted':change(service.database,'DELETE FROM pet_audio_work WHERE work_id=?',(wid,))
    else:service.grant(CHILD,**{**grant,'allowed':False,'expected_revision':1,'action_id':'revoke-before-worker'})
    delivered=[]
    async def delivery(turn):delivered.append(turn)
    asyncio.run(reopen(service).process_pending(delivery))
    assert not delivered and not loopback['posts']


def test_cancelled_worker_reopens_unknown_without_reposting(audio_case,loopback):
    service=audio_case[0];wid=enqueue(audio_case)
    entered=threading.Event();release=threading.Event();delivered=[]
    def slow():entered.set();release.wait(5)
    loopback['callback']=slow
    async def delivery(turn):delivered.append(turn)
    async def exercise():
        task=asyncio.create_task(service.process_pending(delivery))
        try:
            for _ in range(200):
                if entered.is_set():break
                await asyncio.sleep(.01)
            assert entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
        finally:
            release.set()
    asyncio.run(exercise())
    restarted=reopen(service)
    assert restarted.read(CHILD,wid)['state']=='unknown'
    asyncio.run(restarted.process_pending(delivery))
    assert not delivered and len(loopback['posts'])==1


def test_deleted_between_enqueue_and_execution_is_not_dispatched(audio_case,monkeypatch):
    service=audio_case[0];wid=enqueue(audio_case)
    monkeypatch.setattr(service,'enqueue_for_feedback_turn',lambda _:dict(work_id=wid))
    monkeypatch.setattr(service,'work_for_feedback_turn',lambda _:None)
    assert asyncio.run(service.for_feedback_turn('deleted-turn'))['state']=='invalidated'


def test_old_undeliverable_history_cannot_starve_a_new_original(audio_case,loopback):
    service,assessment,*_=audio_case;wid=enqueue(audio_case)
    with service.database.connect() as c:
        def clone(table,row,updates):
            values={**dict(row),**updates};columns=','.join(values)
            c.execute('INSERT INTO '+table+' ('+columns+') VALUES ('+','.join('?' for _ in values)+')',tuple(values.values()))
        source=c.execute('SELECT * FROM rubric_assessment WHERE assessment_id=?',(assessment['assessment_id'],)).fetchone()
        step=c.execute('SELECT * FROM agent_step WHERE step_id=?',(source['step_id'],)).fetchone()
        work=c.execute('SELECT * FROM pet_audio_work WHERE work_id=?',(wid,)).fetchone()
        for n in range(5):
            key='historical-unusable-'+str(n)
            clone('agent_step',step,dict(step_id=key,step_index=100+n,idempotency_key=key))
            clone('rubric_assessment',source,dict(assessment_id=key,step_id=key))
            clone('pet_audio_work',work,dict(work_id=key,assessment_id=key,original_key=key,
                state='response_saved',result_json='{}',created_at='2000-01-01T00:00:00+00:00'))
        c.commit()
    delivered=[]
    async def delivery(turn):delivered.append(turn)
    asyncio.run(service.process_pending(delivery))
    assert service.read(CHILD,wid)['state']=='response_saved'
    assert len(loopback['posts'])==1 and len(delivered)==1
