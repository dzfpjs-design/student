import asyncio
import json

import pytest

from home_study_agent.pet_audio_work import PetAudioTeachingService, encoded
from test_pet_audio_work import audio_case, loopback, enqueue
from test_pet_media_evidence import media, CHILD
from test_pet_audio_audit35 import _clone


def _six_saved(audio_case, clones=5):
    service, assessment, *_ = audio_case
    wid = enqueue(audio_case)
    assert asyncio.run(service.run(CHILD, wid))['state'] == 'response_saved'
    turns = []
    with service.database.connect() as c:
        source = c.execute('SELECT * FROM rubric_assessment WHERE assessment_id=?', (assessment['assessment_id'],)).fetchone()
        step = c.execute('SELECT * FROM agent_step WHERE step_id=?', (source['step_id'],)).fetchone()
        plan = c.execute('SELECT * FROM agent_plan WHERE plan_id=?', (step['plan_id'],)).fetchone()
        turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (plan['agent_turn_id'],)).fetchone()
        work = c.execute('SELECT * FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()
        for i in range(clones):
            key = 'saved36-' + str(i)
            _clone(c, 'agent_turn', turn, agent_turn_id=key, client_action_id=key)
            _clone(c, 'agent_plan', plan, plan_id=key, agent_turn_id=key)
            _clone(c, 'agent_step', step, step_id=key, plan_id=key, idempotency_key=key)
            _clone(c, 'rubric_assessment', source, assessment_id=key, step_id=key)
            original = service._source(c, CHILD, key, live=True)
            _clone(c, 'pet_audio_work', work, work_id=key, assessment_id=key, original_key=key,
                   binding_json=encoded(original['binding']), source_hash=original['source_hash'],
                   live_hash=original['live_hash'], created_at='2000-01-01T00:00:00+00:00')
            turns.append(key)
        c.commit()
    for key in [*turns, wid]:
        assert service.read(CHILD, key, live=True)['state'] == 'response_saved'
    return service, turns, plan['agent_turn_id'], wid


def _reopen(service):
    return PetAudioTeachingService(service.database, media_root=service.media_root,
        data_encryption_key=service.data_encryption_key, gateway=service.gateway, runtime_epoch='fair36-restart')


def test_five_valid_delivery_failures_cannot_starve_sixth_after_restart(audio_case, loopback):
    service, failures, target, wid = _six_saved(audio_case)
    delivered = []
    called = []
    async def delivery(turn):
        called.append(turn)
        if turn in failures:
            raise ValueError('runtime reply may have been lost')
        delivered.append(turn)
    asyncio.run(service.process_pending(delivery))
    asyncio.run(_reopen(service).process_pending(delivery))
    assert delivered == [target], 'Five valid saved sources monopolize LIMIT5 across restart'
    assert all(called.count(turn) == 1 for turn in failures), 'Unknown delivery must not execute twice'
    assert len(loopback['posts']) == 1
    with service.database.connect() as c:
        assert c.execute("SELECT count(*) FROM pet_audio_work WHERE state='invalidated'").fetchone()[0] == 0


def test_transient_original_read_error_does_not_revoke_saved_feedback(audio_case, loopback, monkeypatch):
    from datetime import timedelta
    import home_study_agent.pet_audio_work as worker
    service = audio_case[0]
    wid = enqueue(audio_case)
    assert asyncio.run(service.run(CHILD, wid))['state'] == 'response_saved'
    import home_study_agent.pet_media_evidence as evidence
    read = evidence._read_audio
    def unavailable(*args, **kwargs):
        raise OSError('temporary local disk read failure')
    monkeypatch.setattr(evidence, '_read_audio', unavailable)
    delivered = []
    async def delivery(turn):
        delivered.append(turn)
    asyncio.run(service.process_pending(delivery))
    with service.database.connect() as c:
        assert c.execute('SELECT state FROM pet_audio_work WHERE work_id=?', (wid,)).fetchone()[0] == 'response_saved'
    monkeypatch.setattr(evidence, '_read_audio', read)
    assert _reopen(service).read(CHILD, wid)['state'] == 'response_saved'
    assert not delivered and len(loopback['posts']) == 1
    later = worker.now()+timedelta(seconds=worker.DELIVERY_BACKOFF_SECONDS+1)
    monkeypatch.setattr(worker, 'now', lambda:later)
    asyncio.run(_reopen(service).process_pending(delivery))
    assert len(delivered) == 1 and len(loopback['posts']) == 1


def _second_child_saved(audio_case, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from home_study_agent.repository import StudyRepository
    from home_study_agent.diagnostic_prompt import DiagnosticPromptService
    from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
    from home_study_agent.pet_media_evidence import PetMediaEvidenceService
    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
    from test_agent_continuation import orchestrator_for
    import test_pet_media_evidence as helpers

    service, _, _, _, original_audio, _ = audio_case
    db = service.database
    child = 'fair36-child-b'
    with db.connect() as c:
        profile = c.execute('SELECT * FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()
        _clone(c, 'child_profile', profile, child_id=child, nickname='Synthetic same-grade child B')
        rule = c.execute('SELECT * FROM family_rule WHERE child_id=?', (CHILD,)).fetchone()
        _clone(c, 'family_rule', rule, child_id=child)
        c.commit()
    repo = StudyRepository(db)
    from home_study_agent.document_monitoring import DocumentMonitoringService
    DocumentMonitoringService(db)
    sid = repo.switch_active_session_child(audio_case[5][2], child, 'fair36-child-b')['session_id']
    prompts = DiagnosticPromptService(db)
    prepared = prompts.prepare(sid, track='pet', preferred_item_id='diag-pet-speaking-2',
                               client_action_id='fair36-prompt-b')
    prompt = prepared['prompt']
    assert prompt, prepared
    path = service.media_root / 'child-b.wav.enc'
    path.write_bytes(audio_case[5][4].read_bytes())
    capture = VoiceCaptureBindingService(db)
    capture.begin(sid, 'capture-b', original_audio, 'pet_speaking', {'child_id':child,
        'question_reference':{key:prompt[key] for key in ('prompt_id','item_version')}})
    answer = 'I like reading in the library because it is quiet. I read story books there after school with my friends.'
    with db.connect() as c:
        c.execute("""INSERT INTO voice_input VALUES('voice-b',?,?,'pet_speaking',?,?,
            'synthetic-no-recognizer',1000,'completed',?,'capture-b')""",
            (child,sid,answer,str(path),prompt['created_at']))
        c.execute("INSERT INTO voice_transcription_action VALUES('capture-b',?,?,'completed',?)",
                  (sid,child,prompt['created_at']))
        c.commit()
    capture.complete(sid, 'capture-b', 'voice-b')
    media_service = PetMediaEvidenceService(db, media_root=service.media_root,
        data_encryption_key=service.data_encryption_key)
    media_service.bind_speaking(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
                               voice_input_id='voice-b', client_action_id='bind-b')
    agent, gateway = orchestrator_for(db, service.media_root, {})
    heard = asyncio.run(agent.interpret(sid, answer, 'answer-b', False, voice_input_id='voice-b'))
    if heard['intent'] == 'pending_answer_clarification':
        result = AgentCapabilityExecutor(db, repo).execute(heard['agent_turn_id'])
        agent.complete_turn(heard['agent_turn_id'], result['response_text'], 'completed', None, result)
        heard = asyncio.run(agent.interpret(sid, '刚才那句就是我的答案', 'confirm-b', False))
    assert not gateway.requests
    scored = prompts.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer=answer, client_action_id='submit-b', authorized_turn_id=heard['agent_turn_id'])
    fixture = (db, media_service, sid, prompt, path, [1000])
    with monkeypatch.context() as patch:
        patch.setattr(helpers, 'CHILD', child)
        assessment = helpers.local_review(fixture, prompt, answer, approve=False, scored=scored)
    with db.connect() as c:
        c.execute("UPDATE agent_objective SET status='completed' WHERE child_id=? AND objective_id!=?",
                  (child,assessment['objective_id']))
        c.execute("UPDATE agent_turn SET tool_status='completed' WHERE agent_turn_id IN (SELECT agent_turn_id FROM agent_plan WHERE objective_id=?)",
                  (assessment['objective_id'],))
        turn = c.execute('SELECT p.agent_turn_id FROM agent_step s JOIN agent_plan p USING(plan_id) WHERE s.step_id=?',
                         (assessment['step_id'],)).fetchone()[0]
        c.commit()
    service.grant(child, allowed=True, expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),
                  day_limit_micros=20000, request_limit_micros=10000, expected_revision=0, action_id='grant-b')
    work = service.enqueue(child, assessment['assessment_id'])
    assert asyncio.run(service.run(child, work['work_id']))['state'] == 'response_saved'
    return child, turn, work['work_id']


def test_exact_six_saved_items_across_children_do_not_starve_after_restart(audio_case, loopback, monkeypatch):
    service, failures, first_turn, first_work = _six_saved(audio_case, clones=4)
    failures.append(first_turn)
    called = []
    delivered = []
    async def delivery(turn):
        called.append(turn)
        if turn in failures:
            raise ValueError('first child delivery unavailable')
        delivered.append(turn)
    asyncio.run(service.process_pending(delivery))
    child, target, wid = _second_child_saved(audio_case, monkeypatch)
    with service.database.connect() as c:
        rows = c.execute('SELECT child_id,work_id FROM pet_audio_work').fetchall()
        assert len(rows) == 6 and {r['child_id'] for r in rows} == {CHILD,child}
    for row in rows:
        assert service.read(row['child_id'],row['work_id'],live=row['child_id']==child)['state'] == 'response_saved'
    asyncio.run(_reopen(service).process_pending(delivery))
    assert delivered == [target], 'First child failures starved the other child after restart'
    assert all(called.count(turn) == 1 for turn in failures)
    assert len(loopback['posts']) == 2
    with service.database.connect() as c:
        assert c.execute("SELECT count(*) FROM pet_audio_work WHERE state='invalidated'").fetchone()[0] == 0


def test_one_transient_read_during_retirement_does_not_retire_valid_history(audio_case, loopback, monkeypatch):
    service = audio_case[0]
    wid = enqueue(audio_case)
    assert asyncio.run(service.run(CHILD, wid))['state'] == 'response_saved'
    import home_study_agent.pet_media_evidence as evidence
    original = evidence._read_audio
    calls = []
    def once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('one transient local read')
        return original(*args, **kwargs)
    monkeypatch.setattr(evidence, '_read_audio', once)
    service._retire_saved_delivery(CHILD, wid, 0)
    current = service.read(CHILD, wid, live=True)
    assert current['state'] == 'response_saved'
    assert current['error_code'] != 'SAVED_DELIVERY_RETIRED'
    assert len(loopback['posts']) == 1


def test_cancelled_delivery_reopens_for_receipt_only_after_backoff(audio_case, loopback, monkeypatch):
    from datetime import timedelta
    import home_study_agent.pet_audio_work as worker
    service = audio_case[0]
    wid = enqueue(audio_case)
    assert asyncio.run(service.run(CHILD, wid))['state'] == 'response_saved'
    called = []
    async def cancel_inside_delivery(turn):
        called.append(turn)
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.process_pending(cancel_inside_delivery))
    reopened = _reopen(service)
    reads = []
    recover = reopened._recover_saved_delivery
    def inspect_only(*args):
        reads.append(args)
        return recover(*args)
    monkeypatch.setattr(reopened, '_recover_saved_delivery', inspect_only)
    asyncio.run(reopened.process_pending(cancel_inside_delivery))
    assert not reads
    later = worker.now()+timedelta(seconds=worker.DELIVERY_BACKOFF_SECONDS+1)
    monkeypatch.setattr(worker, 'now', lambda:later)
    asyncio.run(reopened.process_pending(cancel_inside_delivery))
    assert len(called) == 1 and len(reads) == 1
    assert reopened.read(CHILD,wid)['delivery_status'] == 'awaiting_receipt'
    assert len(loopback['posts']) == 1
