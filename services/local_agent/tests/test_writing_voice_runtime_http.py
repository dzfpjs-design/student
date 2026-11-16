"""Synthetic pages and ASR, real HTTP/runtime; no real child or model data."""
import json
import pytest

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.repository import utc_now
from test_content_free_voice_control_http import ControlGateway
from test_diagnostic_prompt_http import environment, start, prepare, CHILD
from test_voice_runtime_binding import capture, interpret
from test_writing_response_origin import TEXT


def page(db, sid, text=TEXT, key='page'):
    monitor = DocumentMonitoringService(db)
    monitor.start(sid, 'writing-monitor')
    with db.connect() as c:
        c.execute("INSERT INTO capture_artifact VALUES(?,?,?,'document_ocr',NULL,?,.95,'{}','completed',?,?)",
                  (key, CHILD, sid, text, utc_now(), key))
        c.commit()
    return monitor.record_revision(sid, key, 'fixture-page', 'revision-' + key)


def writing_prompt(client, db, sid):
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    return prepare(client, sid, track='pet', preferred_item_id='diag-pet-writing-1')['prompt']


def read_turn(client, db, settings, monkeypatch, gateway):
    sid = start(client)
    prompt = writing_prompt(client, db, sid)
    page(db, sid)
    gateway.quote, gateway.control = '作文写完了，你帮我看看吧', 'writing.read_original'
    voice = capture(client, settings, monkeypatch, sid, gateway.quote, prompt)
    interpret(client, sid, gateway.quote, 'writing-look', voice)
    quote = '这页只有我写的答案'
    voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'scope-voice')
    turn = interpret(client, sid, quote, 'scope-answer', voice)
    return sid, prompt, turn


def readback_receipt(client, sid, turn, *, events=True, playback_id='actual-read'):
    bound = turn['action']['writing_ocr_confirmation']
    packet = client.get(f'/v1/sessions/{sid}/writing-ocr', params=bound).json()
    if events:
        for event in ('started', 'ended'):
            response = client.post(f'/v1/sessions/{sid}/writing-ocr/readback', json={**bound,
                'playback_id': playback_id, 'readback_sha256': packet['readback_sha256'],
                'event': event, 'client_action_id': playback_id + '-' + event})
            assert response.status_code == 200, response.text
    return {'client_bridge_receipt': {'capability': 'writing.read_original', 'execution_location': 'client_device_bridge',
        'action_type': 'read_writing_original', 'status': 'completed', 'completed_at': utc_now(),
        'output': {'status': 'readback_completed', **bound, 'playback_id': playback_id}}}


@pytest.mark.parametrize('fault', ['missing_events', 'different_playback', 'sha_changed', None])
def test_readback_outcome_requires_exact_persisted_started_ended(environment, monkeypatch, fault):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, _, turn = read_turn(client, db, settings, monkeypatch, gateway)
        receipt = readback_receipt(client, sid, turn, events=fault != 'missing_events')
        if fault == 'different_playback':
            receipt['client_bridge_receipt']['output']['playback_id'] = 'other-play'
        elif fault == 'sha_changed':
            with db.connect() as c:
                c.execute("UPDATE writing_ocr_event SET payload_json=json_set(payload_json,'$.readback_sha256','wrong') WHERE kind='ended'")
                c.commit()
        response = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
            'final_response': '原稿已读回', 'tool_status': 'completed', 'result': receipt})
        assert response.status_code == (200 if fault is None else 409), response.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
            status = c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?', (turn['agent_turn_id'],)).fetchone()[0]
            assert status == ('completed' if fault is None else 'pending')


@pytest.mark.parametrize('quote,accepted,interrupted,submit_new', [('yes', True, False, False), ('yes', True, True, False),
    ('yes', True, 'storage_error', False),
    ('yes', True, False, True), ('我自己写的，识别得没有错，可以用这份', True, False, False),
    ('不是我写的', False, False, False), ('我还不确定，最后一句需要改字', False, False, False)])
def test_pending_original_confirmation_never_becomes_english_answer(environment, monkeypatch, quote, accepted, interrupted, submit_new):
    settings, db = environment
    gateway = ControlGateway('')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid, prompt, turn = read_turn(client, db, settings, monkeypatch, gateway)
        receipt = readback_receipt(client, sid, turn)
        response = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
            'final_response': '原稿已读回', 'tool_status': 'completed', 'result': receipt})
        assert response.status_code == 200, response.text
        gateway.quote, gateway.control = quote, 'writing.confirm_original'
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'confirm-voice')
        heard = interpret(client, sid, quote, 'confirm-original', voice)
        assert heard['action']['type'] == ('confirm_writing_original' if accepted else 'none')
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        if accepted:
            result = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert result.status_code == 200, result.text
            follow = result.json()['turn']['local_evaluation']
            assert follow['status'] == 'local_feedback_required'
            assert follow['local_feedback_turn_id'] != follow['feedback_turn_id']
            local = client.post(f"/v1/agent-turns/{follow['local_feedback_turn_id']}/execute")
            assert local.status_code == 200, local.text
            assert local.json()['execution']['capability'] == 'conversation.respond'
            assert '本机' in local.json()['execution']['response_text']
            if quote == 'yes':
                calls = len(gateway.requests)
                reference_quote = 'please meet me at the library on Saturday morning at ten.'
                voice_ref = capture(client, settings, monkeypatch, sid, reference_quote, prompt, 'selfcheck-source-voice')
                review = interpret(client, sid, reference_quote, 'selfcheck-source', voice_ref)
                assert review['action']['source'] == 'writing_local_reference'
                ref = review['action']['writing_reference']
                assert ref['point_index'] == 1
                assert ref['entries'][0]['response_quote'] == reference_quote
                assert ref['entries'][0]['semantic_coverage_verified'] is False
                dispatched = client.post(f"/v1/agent-turns/{review['agent_turn_id']}/execute")
                assert dispatched.status_code == 200, dispatched.text
                assert '第2项' in dispatched.json()['execution']['response_text']
                assert len(gateway.requests) == calls
                gateway.quote, gateway.control = '这里还少了内容，我现在补一补再看', 'writing.revise_original'
                revision_voice = capture(client, settings, monkeypatch, sid, gateway.quote, prompt, 'revision-request-voice')
                revision_turn = interpret(client, sid, gateway.quote, 'revision-request', revision_voice)
                assert revision_turn['action']['type'] == 'revise_writing_original'
                assert revision_turn['action']['writing_revision_binding'] == {
                    'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version'],
                    'source_version': ref['source_version'], 'reference_turn_id': review['agent_turn_id']}
                assert revision_turn['action']['request_quote'] == gateway.quote
                assert len(gateway.requests) == calls + 1
                if interrupted == 'storage_error':
                    import sqlite3
                    from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
                    dispatch = AgentCapabilityExecutor._dispatch
                    def storage_failure(self, step):
                        value = dispatch(self, step)
                        if step['agent_turn_id'] == revision_turn['agent_turn_id']:
                            raise sqlite3.OperationalError('simulated disk I/O after real prepare commit')
                        return value
                    with monkeypatch.context() as patch:
                        patch.setattr(AgentCapabilityExecutor,'_dispatch',storage_failure)
                        with pytest.raises(sqlite3.OperationalError,match='simulated disk I/O'):
                            client.post(f"/v1/agent-turns/{revision_turn['agent_turn_id']}/execute")
                    with db.connect() as c:
                        next_prompt = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id!=?',
                            (prompt['prompt_id'],)).fetchone())
                        assert c.execute('SELECT tool_error_code FROM agent_turn WHERE agent_turn_id=?',
                            (revision_turn['agent_turn_id'],)).fetchone()[0] == 'OperationalError'
                elif interrupted:
                    from home_study_agent.agent_orchestrator import AgentOrchestrator
                    complete = AgentOrchestrator.complete_turn
                    def lose_receipt(self, turn_id, *args, **kwargs):
                        if turn_id == revision_turn['agent_turn_id']:
                            raise RuntimeError('simulated loss after real prepare commit')
                        return complete(self, turn_id, *args, **kwargs)
                    # Abort receipt persistence after the real prepare commits;
                    # losing the error response then leaves actual running work.
                    with db.connect() as c:
                        c.execute("""CREATE TRIGGER lose_revision_receipt BEFORE UPDATE ON agent_step
                            WHEN NEW.capability_name='writing.revise_original' AND NEW.status='completed'
                            BEGIN SELECT RAISE(ABORT, 'simulated receipt storage loss'); END""")
                        c.commit()
                    with monkeypatch.context() as patch:
                        patch.setattr(AgentOrchestrator, 'complete_turn', lose_receipt)
                        with pytest.raises(RuntimeError, match='simulated loss'):
                            client.post(f"/v1/agent-turns/{revision_turn['agent_turn_id']}/execute")
                    with db.connect() as c:
                        c.execute('DROP TRIGGER lose_revision_receipt')
                        c.commit()
                        assert c.execute('SELECT status FROM agent_plan WHERE agent_turn_id=?',
                            (revision_turn['agent_turn_id'],)).fetchone()[0] == 'running'
                        next_prompt = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id!=?',
                            (prompt['prompt_id'],)).fetchone())
                else:
                    changed = client.post(f"/v1/agent-turns/{revision_turn['agent_turn_id']}/execute")
                    assert changed.status_code == 200, changed.text
                    assert changed.json()['execution']['evidence'] == ['writing_revision_prompt_bound']
                    next_prompt = changed.json()['execution']['result']['prompt']
                assert next_prompt['prompt_id'] != prompt['prompt_id']
                assert next_prompt['diagnostic_item_id'] == prompt['diagnostic_item_id']
        with db.connect() as c:
            attempts = c.execute('SELECT submitted_answer FROM diagnostic_attempt').fetchall()
            assert [r[0] for r in attempts] == ([TEXT] if accepted else [])
            assert c.execute("SELECT count(*) FROM writing_ocr_event WHERE kind='authorize'").fetchone()[0] == int(accepted)
            if accepted:
                source_prompt = c.execute('SELECT authorized_turn_id,writing_original_artifact_id FROM diagnostic_prompt').fetchone()
                assert source_prompt['authorized_turn_id'] == heard['agent_turn_id']
                assert source_prompt['writing_original_artifact_id']
                assert c.execute('SELECT count(*) FROM learning_evaluation_execution').fetchone()[0] == 0
        for request in gateway.requests:
            assert TEXT not in json.dumps(request.messages)
    if accepted:
        with TestClient(create_app(settings, model_gateway=gateway)) as client:
            if quote == 'yes':
                if interrupted:
                    with db.connect() as c:
                        assert c.execute('SELECT tool_error_code FROM agent_turn WHERE agent_turn_id=?',
                                (revision_turn['agent_turn_id'],)).fetchone()[0] == ('OperationalError' if interrupted == 'storage_error' else 'APP_RESTARTED')
                waiting = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id':next_prompt['prompt_id']})
                assert waiting.status_code == 200 and waiting.json()['status'] == 'waiting_answer', waiting.text
                repeated = client.post(f"/v1/agent-turns/{revision_turn['agent_turn_id']}/execute")
                assert repeated.status_code == 200, repeated.text
                assert repeated.json()['execution']['result']['prompt']['prompt_id'] == next_prompt['prompt_id']
                with db.connect() as c:
                    bound = json.loads(c.execute('SELECT plan_json FROM agent_plan WHERE agent_turn_id=?',
                        (revision_turn['agent_turn_id'],)).fetchone()[0])['local_learning_binding']
                    assert bound['prompt_id'] == next_prompt['prompt_id']
                    assert c.execute('SELECT count(*) FROM writing_reference_revision').fetchone()[0] == 1
                if submit_new:
                    manuscript = 'Dear Alex, please meet me at the library on Sunday at nine. We can read English stories together. Bring your workbook and a pencil. I will bring some short stories to share. See you soon, Mia.'
                    page(db,sid,manuscript,'reference-new-manuscript')
                    def say(text, key, control):
                        gateway.quote, gateway.control = text, control
                        voice = capture(client,settings,monkeypatch,sid,text,next_prompt,key+'-voice')
                        return interpret(client,sid,text,key,voice)
                    say('新稿已经写好了，再帮我看看','new-writing-look','writing.read_original')
                    reading = say('这页只有我写的答案','new-writing-scope','writing.read_original')
                    receipt = readback_receipt(client,sid,reading,playback_id='new-manuscript-read')
                    read = client.put(f"/v1/agent-turns/{reading['agent_turn_id']}/outcome",json={
                        'final_response':'新稿已读回','tool_status':'completed','result':receipt})
                    assert read.status_code == 200, read.text
                    confirm = say('yes','new-writing-confirm','writing.confirm_original')
                    submitted = client.post(f"/v1/agent-turns/{confirm['agent_turn_id']}/execute")
                    assert submitted.status_code == 200, submitted.text
                    followup = submitted.json()['turn']['local_evaluation']
                    assert followup['status'] == 'local_feedback_required', followup
                    feedback = client.post(f"/v1/agent-turns/{followup['local_feedback_turn_id']}/execute")
                    assert feedback.status_code == 200, feedback.text
                    with db.connect() as c:
                        saved = c.execute('''SELECT a.submitted_answer,e.hint_level FROM diagnostic_prompt p
                            JOIN diagnostic_attempt a USING(attempt_id) JOIN learning_evidence e USING(evidence_id)
                            WHERE p.prompt_id=?''',(next_prompt['prompt_id'],)).fetchone()
                        assert saved['submitted_answer'] == manuscript and saved['hint_level'] >= 1
                        origin = json.loads(c.execute('SELECT context_manifest_json FROM agent_plan WHERE agent_turn_id=?',
                            (followup['feedback_turn_id'],)).fetchone()[0])['original_learning_binding']
                        assert origin['objective_id'] == bound['objective_id'] and origin['prompt_id'] == next_prompt['prompt_id']
                if not submit_new:
                    stopped = client.post(f'/v1/sessions/{sid}/agent-turn', json={
                        'transcript':'别讲了','client_action_id':'cancel-reference-revision','cloud_text_allowed':False})
                    assert stopped.status_code == 200, stopped.text
                invalid = client.get(f'/v1/sessions/{sid}/diagnostic-prompt', params={'prompt_id':next_prompt['prompt_id']})
                assert invalid.status_code == 200 and invalid.json()['status'] == ('answered' if submit_new else 'invalidated'), invalid.text
                with db.connect() as c:
                    assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',
                        (prompt['prompt_id'],)).fetchone()[0] == 'answered'
            replay = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
            assert replay.status_code == 200, replay.text
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == (2 if submit_new else 1)
                assert c.execute("SELECT count(*) FROM agent_step WHERE capability_name='learning.evaluate_response'").fetchone()[0] == (2 if submit_new else 1)
                assert c.execute("SELECT count(*) FROM agent_objective WHERE objective_type='learning' AND status='completed'").fetchone()[0] == 0
    if submit_new:
        with TestClient(create_app(settings,model_gateway=gateway)) as client:
            replay = client.post(f"/v1/agent-turns/{confirm['agent_turn_id']}/execute")
            assert replay.status_code == 200, replay.text
            assert replay.json()['turn']['local_evaluation']['local_feedback_turn_id'] == followup['local_feedback_turn_id']
            stopped = client.post(f'/v1/sessions/{sid}/agent-turn',json={
                'transcript':'别讲了','client_action_id':'cancel-answered-reference','cloud_text_allowed':False})
            assert stopped.status_code == 200, stopped.text
            cancelled_replay = client.post(f"/v1/agent-turns/{confirm['agent_turn_id']}/execute")
            assert cancelled_replay.status_code == 200, cancelled_replay.text
            recovery = cancelled_replay.json()['turn']['local_evaluation']
            assert recovery['status'] == 'blocked' and recovery['reason'] == 'original_learning_objective_changed'
            assert not recovery.get('local_feedback_turn_id')
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 2
                assert c.execute('SELECT count(*) FROM writing_reference_revision').fetchone()[0] == 1
                assert c.execute('SELECT status FROM diagnostic_prompt WHERE prompt_id=?',
                    (next_prompt['prompt_id'],)).fetchone()[0] == 'answered'
                assert c.execute('SELECT tool_status FROM agent_turn WHERE agent_turn_id=?',
                    (followup['local_feedback_turn_id'],)).fetchone()[0] == 'completed'
        for request in gateway.requests:
            assert manuscript not in json.dumps(request.messages)


def test_writing_read_request_clarifies_span_then_freezes_exact_page_after_restart(environment, monkeypatch):
    settings, db = environment
    quote = '作文写完了，你帮我看看吧'
    gateway = ControlGateway(quote, 'writing.read_original')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = start(client)
        prompt = writing_prompt(client, db, sid)
        revision = page(db, sid)
        voice = capture(client, settings, monkeypatch, sid, quote, prompt)
        heard = interpret(client, sid, quote, 'writing-look', voice)
        assert heard['action']['type'] == 'none'
        assert heard['action']['source'] == 'writing_scope_clarification'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM writing_ocr_confirmation').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        quote = '这页只有我写的答案'
        voice = capture(client, settings, monkeypatch, sid, quote, prompt, 'scope-voice')
        heard = interpret(client, sid, quote, 'scope-answer', voice)
        assert heard['action']['type'] == 'read_writing_original'
        packet = heard['action']['writing_ocr_confirmation']
        assert set(packet) == {'confirmation_id', 'binding_version'}
        with db.connect() as c:
            row = c.execute('SELECT * FROM writing_ocr_confirmation').fetchone()
            assert row['revision_id'] == revision['revision_id']
            assert row['response_start'] == 0 and row['response_end'] == len(TEXT)
            assert c.execute('SELECT count(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert interpret(client, sid, quote, 'scope-answer', voice)['agent_turn_id'] == heard['agent_turn_id']
    assert len(gateway.requests) == 1
    transmitted = json.dumps(gateway.requests[0].messages, ensure_ascii=False)
    for value in (TEXT, prompt['item']['prompt'] if 'item' in prompt else 'UNUSED', sid, revision['revision_id']):
        assert value not in transmitted
