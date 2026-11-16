import asyncio
import sqlite3

import pytest

from home_study_agent.pet_audio_work import PetAudioTeachingService
from home_study_agent.pet_audio_projection import current_feedback
from test_pet_audio_work import audio_case, loopback, enqueue
from test_pet_media_evidence import media, CHILD


def _clone(c, table, row, **updates):
    values = {**dict(row), **updates}
    c.execute('INSERT INTO ' + table + ' (' + ','.join(values) + ') VALUES ('
              + ','.join('?' for _ in values) + ')', tuple(values.values()))


def _saved_backlog(audio_case):
    service, assessment, *_ = audio_case
    wid = enqueue(audio_case)
    assert asyncio.run(service.run(CHILD, wid))['state'] == 'response_saved'
    dispatch, results = _insert_stale_saved(service, assessment['assessment_id'], wid)
    return service, wid, dispatch, results


def _insert_stale_saved(service, assessment_id, wid):
    with service.database.connect() as c:
        source = c.execute('SELECT * FROM rubric_assessment WHERE assessment_id=?',
                           (assessment_id,)).fetchone()
        step = c.execute('SELECT * FROM agent_step WHERE step_id=?', (source['step_id'],)).fetchone()
        work = c.execute('SELECT * FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()
        for n in range(5):
            key = 'stale-saved-' + str(n)
            _clone(c, 'agent_step', step, step_id=key, step_index=100+n, idempotency_key=key)
            _clone(c, 'rubric_assessment', source, assessment_id=key, step_id=key)
            # Persist a stale original binding, not a gateway failure or new queue.
            _clone(c, 'pet_audio_work', work, work_id=key, assessment_id=key,
                   original_key=key, created_at='2000-01-01T00:00:00+00:00')
        dispatch = [tuple(r) for r in c.execute('SELECT * FROM pet_audio_dispatch')]
        results = {r['work_id']: r['result_json'] for r in c.execute('SELECT * FROM pet_audio_work')}
        c.commit()
    return dispatch, results


def test_five_invalid_saved_responses_cannot_starve_sixth_saved_response(audio_case, loopback):
    service, wid, dispatch, results = _saved_backlog(audio_case)
    delivered = []

    async def delivery(turn):
        delivered.append(turn)

    async def poll():
        for _ in range(3):
            await service.process_pending(delivery)

    asyncio.run(poll())
    assert delivered, 'Five stale response_saved rows monopolized every LIMIT 5 poll'
    assert len(loopback['posts']) == 1, 'Saved feedback recovery must never resubmit original audio'
    with service.database.connect() as c:
        stale = c.execute("SELECT state FROM pet_audio_work WHERE work_id LIKE 'stale-saved-%'").fetchall()
        assert len(stale) == 5 and {r[0] for r in stale} == {'invalidated'}
        assert [tuple(r) for r in c.execute('SELECT * FROM pet_audio_dispatch')] == dispatch
        assert {r['work_id']: r['result_json'] for r in c.execute('SELECT * FROM pet_audio_work')} == results
    reopened = PetAudioTeachingService(service.database, media_root=service.media_root,
        data_encryption_key=service.data_encryption_key, gateway=service.gateway, runtime_epoch='audit35-restart')
    assert reopened.read(CHILD, wid)['state'] == 'response_saved'
    asyncio.run(reopened.process_pending(delivery))
    assert len(loopback['posts']) == 1


def test_retiring_live_delivery_preserves_read_only_historical_feedback(audio_case, loopback, monkeypatch):
    service = audio_case[0]
    wid = enqueue(audio_case)
    assert asyncio.run(service.run(CHILD, wid))['state'] == 'response_saved'
    with service.database.connect() as c:
        sid = c.execute('SELECT session_id FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()[0]
        c.execute('BEGIN')
        historical = current_feedback(c, CHILD, sid)
        assert len(historical) == 1
        # A later local state transition changes live delivery authority, not the original answer.
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    assert service.read(CHILD, wid, live=True)['state'] == 'invalidated'
    assert service.read(CHILD, wid, live=False)['state'] == 'response_saved'
    delivered = []
    async def delivery(turn):
        delivered.append(turn)
    asyncio.run(service.process_pending(delivery))
    with service.database.connect() as c:
        c.execute('BEGIN')
        row = c.execute('SELECT * FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()
        assert row['state'] == 'response_saved'
        assert row['error_code'] == 'SAVED_DELIVERY_RETIRED'
        assert current_feedback(c, CHILD, sid) == historical
    reopened = PetAudioTeachingService(service.database, media_root=service.media_root,
        data_encryption_key=service.data_encryption_key, gateway=service.gateway, runtime_epoch='retired-restart')
    async def must_not_poll(*args, **kwargs):
        raise AssertionError('Retired delivery consumed a restarted worker slot')
    monkeypatch.setattr(reopened, 'run', must_not_poll)
    asyncio.run(reopened.process_pending(delivery))
    assert not delivered and len(loopback['posts']) == 1


def test_delivery_error_does_not_retire_valid_saved_evidence(audio_case, loopback):
    service = audio_case[0]
    wid = enqueue(audio_case)
    assert asyncio.run(service.run(CHILD, wid))['state'] == 'response_saved'
    async def unavailable(turn):
        raise ValueError('local runtime unavailable, original authority remains current')
    asyncio.run(service.process_pending(unavailable))
    with service.database.connect() as c:
        row = c.execute('SELECT state,error_code FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()
        assert tuple(row) == ('response_saved', 'SAVED_DELIVERY_UNCONFIRMED')
    delivered = []
    async def delivery(turn):
        delivered.append(turn)
    asyncio.run(service.process_pending(delivery))
    assert not delivered and len(loopback['posts']) == 1
    assert service.read(CHILD, wid)['delivery_status'] == 'awaiting_receipt'


def test_retirement_is_child_generation_fenced_and_rolls_back(audio_case, loopback):
    service, _, dispatch, _ = _saved_backlog(audio_case)
    wid = 'stale-saved-0'
    service._retire_saved_delivery('child-junior-3', wid, 0)
    service._retire_saved_delivery(CHILD, wid, -1)
    with service.database.connect() as c:
        assert c.execute('SELECT state FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()[0] == 'response_saved'
        c.execute("""CREATE TRIGGER audit35_retirement_failure AFTER UPDATE ON pet_audio_work
            WHEN NEW.work_id='stale-saved-0' BEGIN SELECT RAISE(ABORT,'audit35 rollback'); END""")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='audit35 rollback'):
        service._retire_saved_delivery(CHILD, wid, 0)
    with service.database.connect() as c:
        assert c.execute('SELECT state FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()[0] == 'response_saved'
        c.execute('DROP TRIGGER audit35_retirement_failure')
        c.commit()
    service._retire_saved_delivery(CHILD, wid, 0)
    with service.database.connect() as c:
        assert c.execute('SELECT state FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()[0] == 'invalidated'
        assert [tuple(r) for r in c.execute('SELECT * FROM pet_audio_dispatch')] == dispatch
    assert len(loopback['posts']) == 1


def test_tcp_restart_drains_stale_saved_backlog_without_audio_resubmission(tmp_path, monkeypatch, loopback):
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    import time
    from home_study_agent.app import create_app
    from home_study_agent.database import Database
    from home_study_agent.repository import StudyRepository
    from test_pet_audio_vertical_http import tcp_client
    from test_pet_oral_supply_http import setup, post
    from test_oral_runtime_feedback_http import OralGateway, begin_oral, ANSWER
    from test_oral_followup_execution_http import answer

    initial, settings = setup(tmp_path)
    settings = replace(settings, data_encryption_key=b'A'*32)
    db = Database(initial.path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    planner = OralGateway()
    kwargs = dict(model_gateway=planner, pet_audio_gateway=loopback['gateway'])
    headers = {'X-HSC-Session': settings.session_token}
    app = create_app(settings, **kwargs)
    service = app.state.pet_audio_teaching
    # Crash boundary: the worker has saved its response but has not delivered it.
    async def hold_delivery(deliver):
        pass
    monkeypatch.setattr(service, 'process_pending', hold_delivery)
    with tcp_client(app, headers) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin':'7391'})['parent_token']
        assert client.put('/v1/parent/privacy', json={'raw_media_storage_allowed':True,
            'cloud_model_text_allowed':True, 'cloud_audio_allowed':True}).status_code == 200
        post(client, f'/v1/parent/children/{CHILD}/pet-audio/authorization', dict(allowed=True,
            expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
            day_limit_micros=100000, request_limit_micros=10000, expected_revision=0, action_id='audit35-grant'))
        sid, prompt, _, _ = begin_oral(client, db, planner)
        saved, _ = answer(client, settings, monkeypatch, sid, prompt, planner, ANSWER, 'audit35-original')
        wid = saved['original_audio_teaching']['work_id']
        tid = saved['local_learning_evaluation']['feedback_turn_id']
        assert asyncio.run(service.run(CHILD, wid))['state'] == 'response_saved'
        with db.connect() as c:
            aid = c.execute('SELECT assessment_id FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()[0]
        dispatch, results = _insert_stale_saved(service, aid, wid)
    assert len(loopback['posts']) == 1
    with tcp_client(create_app(settings, **kwargs), headers) as client:
        deadline = time.monotonic()+12
        while time.monotonic() < deadline:
            response = client.get(f'/v1/agent-turns/{tid}/execution-status')
            assert response.status_code == 200
            with db.connect() as c:
                state = c.execute('SELECT state FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()[0]
            execution = response.json().get('execution') or {}
            receipt = execution.get('execution') or {}
            feedback = (receipt.get('result') or {}).get('oral_feedback')
            if state == 'delivered' and feedback is not None:
                break
            time.sleep(.05)
        assert state == 'delivered', 'Restarted managed worker starved the saved feedback'
        assert feedback is not None, 'Delivered feedback did not reach its authoritative HTTP receipt'
        assert feedback['audio_work_ref']['work_id'] == wid
        assert feedback['mastery_claim_allowed'] is False
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
            c.commit()
        repo = StudyRepository(db)
        repo.complete_with_reflection(sid, '', '', '', '', True, 'audit35-reflection')
        evidence = repo.report_evidence(sid)
        assert len(evidence['pet_audio_practice']) == 1
        assert evidence['pet_audio_practice'][0]['mastery_claim_allowed'] is False
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM pet_audio_work WHERE work_id LIKE 'stale-saved-%' AND state='invalidated'").fetchone()[0] == 5
            assert [tuple(r) for r in c.execute('SELECT * FROM pet_audio_dispatch')] == dispatch
            assert {r['work_id']: r['result_json'] for r in c.execute('SELECT * FROM pet_audio_work')} == results
    assert len(loopback['posts']) == 1
