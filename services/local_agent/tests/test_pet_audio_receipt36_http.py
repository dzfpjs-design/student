from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time

import pytest

from home_study_agent.app import create_app
from home_study_agent.database import Database
from test_pet_audio_vertical_http import tcp_client
from test_pet_audio_work import loopback
from test_pet_oral_supply_http import setup, post, CHILD
from test_oral_runtime_feedback_http import OralGateway, begin_oral, ANSWER
from test_oral_followup_execution_http import answer


@pytest.mark.parametrize('boundary', ['before_delivery_callback', 'after_completed_evaluation_reply', 'receipt_reader_unavailable'])
def test_unknown_delivery_restart_only_checks_original_runtime_receipt(tmp_path, monkeypatch, loopback, boundary):
    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'A'*32)
    db = Database(initial.path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    planner = OralGateway()
    kwargs = dict(model_gateway=planner, pet_audio_gateway=loopback['gateway'])
    headers = {'X-HSC-Session':settings.session_token}
    app = create_app(settings, **kwargs)
    service = app.state.pet_audio_teaching
    pending = service.process_pending
    calls = []
    mark_attempts = []

    async def fail_delivery(deliver):
        async def uncertain(turn):
            calls.append(turn)
            if boundary != 'after_completed_evaluation_reply':
                raise ValueError('No runtime receipt received')
            return await deliver(turn)
        return await pending(uncertain)

    monkeypatch.setattr(service, 'process_pending', fail_delivery)
    if boundary == 'after_completed_evaluation_reply':
        def lost_delivery_mark(turn, result):
            mark_attempts.append(result)
            raise OSError('Runtime committed; delivery-mark write unavailable')
        monkeypatch.setattr(service, 'record_runtime_delivery', lost_delivery_mark)

    with tcp_client(app, headers) as client:
        client.headers['X-HSC-Parent'] = post(client,'/v1/parent/setup',{'pin':'7391'})['parent_token']
        assert client.put('/v1/parent/privacy',json={'raw_media_storage_allowed':True,
            'cloud_model_text_allowed':True,'cloud_audio_allowed':True}).status_code == 200
        post(client,f'/v1/parent/children/{CHILD}/pet-audio/authorization',dict(allowed=True,
            expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
            day_limit_micros=100000,request_limit_micros=10000,expected_revision=0,action_id='grant36'))
        sid,prompt,_,_ = begin_oral(client,db,planner)
        saved,_ = answer(client,settings,monkeypatch,sid,prompt,planner,ANSWER,'receipt36-original')
        wid = saved['original_audio_teaching']['work_id']
        turn = saved['local_learning_evaluation']['feedback_turn_id']
        deadline = time.monotonic()+12
        while not calls and time.monotonic()<deadline:
            time.sleep(.03)
        assert calls == [turn]
        # PET admission already requires a completed local evaluation. This is
        # the non-scoring response delivery boundary, not an unexecuted answer.
        if boundary == 'after_completed_evaluation_reply':
            while not mark_attempts and time.monotonic()<deadline:
                time.sleep(.03)
            assert mark_attempts[0]['result']['oral_feedback']['audio_work_ref']['work_id'] == wid
        status = client.get(f'/v1/agent-turns/{turn}/execution-status')
        assert status.status_code == 200 and status.json()['status']=='unknown', status.text
        assert status.json()['original_audio_teaching']['delivery_status']=='awaiting_receipt'
        with db.connect() as c:
            work = dict(c.execute('SELECT * FROM pet_audio_work WHERE work_id=?',(wid,)).fetchone())
            assert work['state']=='response_saved' and work['error_code']=='SAVED_DELIVERY_UNCONFIRMED'
            steps = [tuple(r) for r in c.execute('SELECT step_id,status,result_json,error_code FROM agent_step ORDER BY step_id')]
            counts = [c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
                      ('diagnostic_attempt','rubric_assessment','agent_turn','agent_continuation','pet_audio_dispatch')]

    second = create_app(settings, **kwargs)
    restarted = second.state.pet_audio_teaching
    recover = restarted._recover_saved_delivery
    reads = []
    def inspect_receipt(*args):
        reads.append(args)
        return recover(*args)
    monkeypatch.setattr(restarted, '_recover_saved_delivery', inspect_receipt)
    if boundary == 'receipt_reader_unavailable':
        def unavailable(turn):
            raise OSError('Completed runtime receipt temporarily unreadable')
        monkeypatch.setattr(restarted, 'runtime_receipt_reader', unavailable)
    process = restarted.process_pending
    forbidden = []
    async def inspect_only(deliver):
        async def no_reexecution(turn):
            forbidden.append(turn)
            raise AssertionError('Unknown delivery executed again after restart')
        await process(no_reexecution)
    monkeypatch.setattr(restarted, 'process_pending', inspect_only)
    with tcp_client(second, headers) as client:
        deadline = time.monotonic()+12
        while time.monotonic()<deadline:
            with db.connect() as c:
                state = c.execute('SELECT state FROM pet_audio_work WHERE work_id=?',(wid,)).fetchone()[0]
            if reads and (boundary=='receipt_reader_unavailable' or state=='delivered'):
                break
            time.sleep(.05)
        assert reads and not forbidden
        response = client.get(f'/v1/agent-turns/{turn}/execution-status')
        assert response.status_code == 200
        if boundary != 'receipt_reader_unavailable':
            assert state=='delivered' and response.json()['status']=='completed'
        else:
            assert state=='response_saved' and response.json()['status']=='unknown'
        with db.connect() as c:
            assert [tuple(r) for r in c.execute('SELECT step_id,status,result_json,error_code FROM agent_step ORDER BY step_id')] == steps
            assert [c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
                    ('diagnostic_attempt','rubric_assessment','agent_turn','agent_continuation','pet_audio_dispatch')] == counts
            assert c.execute('SELECT result_json FROM pet_audio_work WHERE work_id=?',(wid,)).fetchone()[0] == work['result_json']
    assert len(loopback['posts']) == 1 and calls == [turn]
