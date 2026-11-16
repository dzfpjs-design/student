import json
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelResponseError
from test_tutor_check_coaching_http import CoachingGateway, heard_check
from test_voice_runtime_binding import capture, interpret

QUOTE = '重试刚才的点评'

class RetryGateway(CoachingGateway):
    async def complete(self, request):
        if request.request_type != 'voice_interaction' and not self.coaching:
            self.coaching.append(request)
            raise ModelResponseError('DECLARED_SYNTHETIC_TIMEOUT')
        return await super().complete(request)


def failure(tmp_path, monkeypatch, *, stale_pending=False):
    gateway = RetryGateway()
    client, db, settings, sid, parent, original = heard_check(tmp_path, monkeypatch, gateway)
    url = f"/v1/agent-turns/{original['agent_turn_id']}/execute"
    failed = client.post(url)
    assert failed.status_code == 200, failed.text
    assert failed.json()['execution']['coaching_status'] == 'failed'
    assert '重试刚才的点评' in failed.json()['execution']['response_text']
    replay = client.post(url)
    assert replay.status_code == 200 and len(gateway.coaching) == 1
    if stale_pending:
        from datetime import datetime, timedelta, timezone
        with db.connect() as c:
            c.execute("UPDATE tutor_request_reservation SET status='pending',updated_at=? WHERE parent_turn_id=?",
                ((datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat(), parent['turn_id']))
            c.commit()
    gateway.quote, gateway.control, gateway.role = QUOTE, 'learning.retry_check_coaching', 'control'
    target = parent['output_envelope']['local_teaching_control']['question_target']['binding']
    reference = {'kind': 'diagnostic_help', 'binding': {k: target[k] for k in ('prompt_id', 'item_version')}}
    voice = capture(client, settings, monkeypatch, sid, QUOTE, reference,
        key='retry-check-coaching-capture', child='child-junior-3')
    retry = interpret(client, sid, QUOTE, 'retry-check-coaching-command', voice)
    assert retry.get('runtime_capability') == 'tutor.retry_check_coaching', retry
    return client, db, settings, sid, parent, gateway, retry


def test_explicit_saved_voice_retries_unknown_check_coaching_once_and_replays_after_restart(tmp_path, monkeypatch):
    client, db, settings, sid, parent, gateway, retry = failure(tmp_path, monkeypatch)
    try:
        url = f"/v1/agent-turns/{retry['agent_turn_id']}/execute"
        response = client.post(url)
        assert response.status_code == 200, response.text
        result = response.json()['execution']
        assert result['coaching_status'] == 'completed'
        taught = result['tutor_turn']
        assert taught['parent_turn_id'] == parent['turn_id']
        assert taught['output_envelope']['mastery_update_allowed'] is False
        assert len(gateway.coaching) == 2
        with db.connect() as c:
            counts = {t: c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                for t in ('tutor_turn', 'tutor_feedback', 'diagnostic_attempt')}
            assert counts == {'tutor_turn': 2, 'tutor_feedback': 1, 'diagnostic_attempt': 1}
            assert not c.execute("SELECT 1 FROM diagnostic_attempt WHERE child_id='child-primary-4'").fetchone()
        for current in [client]:
            replay = current.post(url)
            assert replay.status_code == 200, replay.text
            assert replay.json()['execution']['tutor_turn']['turn_id'] == taught['turn_id']
            assert replay.json()['execution']['replay_audio_allowed'] is False
        client.__exit__(None, None, None)
        client = None
        with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token}) as restarted:
            replay = restarted.post(url)
            assert replay.status_code == 200, replay.text
            assert replay.json()['execution']['tutor_turn']['turn_id'] == taught['turn_id']
        assert len(gateway.coaching) == 2
        for request in gateway.requests:
            if request.request_type == 'voice_interaction':
                wire = json.dumps(request.messages, ensure_ascii=False)
                assert sid not in wire and parent['turn_id'] not in wire
    finally:
        if client is not None:
            client.__exit__(None, None, None)

import pytest

@pytest.mark.parametrize('change', ['voice_cancel','feedback','withdraw','consent_cycle','later_input','other_child','fresh_pending','newer_attempt'])
def test_retry_control_rechecks_local_authority_before_model_dispatch(tmp_path, monkeypatch, change):
    client, db, settings, sid, parent, gateway, retry = failure(tmp_path, monkeypatch)
    try:
        if change == 'voice_cancel':
            assert client.post(f'/v1/sessions/{sid}/asr/actions/retry-check-coaching-capture/cancel').status_code == 200
        else:
            with db.connect() as c:
                if change == 'feedback': c.execute("UPDATE tutor_feedback SET response_text='Changed synthetic original'")
                elif change == 'withdraw': c.execute("UPDATE curriculum_first_source SET state='withdrawn'")
                elif change == 'consent_cycle':
                    original = c.execute("SELECT * FROM consent_record WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1").fetchone()
                    columns = list(original.keys());values = [original[k] for k in columns];values[columns.index('consent_id')] = 'changed-retry-consent-cycle'
                    c.execute('INSERT INTO consent_record ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', values)
                elif change == 'later_input':
                    c.execute("UPDATE learning_input_arrival SET client_action_id='later-authority' WHERE client_action_id='retry-check-coaching-command'")
                elif change == 'fresh_pending':
                    from datetime import datetime, timezone
                    c.execute("UPDATE tutor_request_reservation SET status='pending',updated_at=? WHERE parent_turn_id=?",
                        (datetime.now(timezone.utc).isoformat(), parent['turn_id']))
                elif change == 'newer_attempt':
                    c.execute("UPDATE tutor_request_reservation SET attempts=attempts+1 WHERE parent_turn_id=?", (parent['turn_id'],))
                else: c.execute("UPDATE daily_session SET child_id='child-primary-4' WHERE session_id=?", (sid,))
                c.commit()
        response = client.post(f"/v1/agent-turns/{retry['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        assert len(gateway.coaching) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    finally: client.__exit__(None, None, None)

@pytest.mark.parametrize('change', ['voice_cancel','feedback','withdraw','consent_cycle'])
def test_retry_result_cannot_persist_after_authority_changes_during_provider_call(tmp_path, monkeypatch, change):
    client, db, settings, sid, parent, gateway, retry = failure(tmp_path, monkeypatch)
    def mutate():
        if change == 'voice_cancel':
            with db.connect() as c:
                c.execute("UPDATE voice_capture_binding SET status='cancelled' WHERE client_action_id='retry-check-coaching-capture'")
                c.commit()
        else:
            with db.connect() as c:
                if change == 'feedback': c.execute("UPDATE tutor_feedback SET response_text='Changed while awaiting provider'")
                elif change == 'withdraw': c.execute("UPDATE curriculum_first_source SET state='withdrawn'")
                else:
                    original = c.execute("SELECT * FROM consent_record WHERE scope='cloud_model_text_allowed' ORDER BY rowid DESC LIMIT 1").fetchone()
                    columns = list(original.keys());values = [original[k] for k in columns];values[columns.index('consent_id')] = 'changed-provider-retry-cycle'
                    c.execute('INSERT INTO consent_record ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', values)
                c.commit()
    gateway.hook = mutate
    try:
        response = client.post(f"/v1/agent-turns/{retry['agent_turn_id']}/execute")
        assert response.status_code == 409, response.text
        assert len(gateway.coaching) == 2
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_turn').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    finally: client.__exit__(None, None, None)

@pytest.mark.parametrize('text', ['不要重试刚才的点评','如果重试刚才的点评会怎样','他说“重试刚才的点评”','重试刚才的点评，然后重新评分','刚才的点评是什么'])
def test_quoted_denied_hypothetical_or_mixed_feedback_retry_cannot_authorize(text):
    from home_study_agent.voice_control_intent import VoiceControlIntent
    value = dict(role='control', control='learning.retry_check_coaching', quote=text, confidence=.99)
    assert VoiceControlIntent.parse(json.dumps(value, ensure_ascii=False), text).get('status') != 'control'


def test_replay_of_a_retry_that_timed_out_does_not_retry_unknown_outcome_again(tmp_path, monkeypatch):
    client, db, settings, sid, parent, gateway, retry = failure(tmp_path, monkeypatch)
    def second_timeout(): raise ModelResponseError('DECLARED_SECOND_SYNTHETIC_TIMEOUT')
    gateway.hook = second_timeout
    try:
        url = f"/v1/agent-turns/{retry['agent_turn_id']}/execute"
        response = client.post(url)
        assert response.status_code == 409, response.text
        response = client.post(url)
        assert response.status_code == 409, response.text
        assert len(gateway.coaching) == 2
        with db.connect() as c:
            row = c.execute('SELECT status,attempts FROM tutor_request_reservation WHERE parent_turn_id=?', (parent['turn_id'],)).fetchone()
            assert tuple(row) == ('interrupted', 2)
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        gateway.hook = None
        target = parent['output_envelope']['local_teaching_control']['question_target']['binding']
        reference = {'kind': 'diagnostic_help', 'binding': {k: target[k] for k in ('prompt_id', 'item_version')}}
        fresh = capture(client, settings, monkeypatch, sid, QUOTE, reference,
            key='fresh-after-second-timeout-capture', child='child-junior-3')
        requested = interpret(client, sid, QUOTE, 'fresh-after-second-timeout-command', fresh)
        assert requested.get('runtime_capability') == 'tutor.retry_check_coaching', requested
        result = client.post(f"/v1/agent-turns/{requested['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        assert len(gateway.coaching) == 3
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    finally: client.__exit__(None, None, None)


def test_explicit_voice_can_retry_a_stale_pending_canonical_coaching_request(tmp_path, monkeypatch):
    client, db, settings, sid, parent, gateway, retry = failure(tmp_path, monkeypatch, stale_pending=True)
    try:
        assert retry['action']['confirm_unknown_outcome_retry'] is True
        response = client.post(f"/v1/agent-turns/{retry['agent_turn_id']}/execute")
        assert response.status_code == 200, response.text
        assert response.json()['execution']['coaching_status'] == 'completed'
        assert len(gateway.coaching) == 2
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    finally: client.__exit__(None, None, None)


def test_completed_retry_receipt_cannot_expose_withdrawn_source(tmp_path, monkeypatch):
    client, db, settings, sid, parent, gateway, retry = failure(tmp_path, monkeypatch)
    try:
        url = f"/v1/agent-turns/{retry['agent_turn_id']}/execute"
        assert client.post(url).status_code == 200
        with db.connect() as c:
            c.execute("UPDATE curriculum_first_source SET state='withdrawn'")
            c.commit()
        response = client.post(url)
        assert response.status_code == 409, response.text
        assert len(gateway.coaching) == 2
    finally: client.__exit__(None, None, None)


def test_retry_snapshot_never_reinterprets_edited_feedback_as_the_original_saved_asr(tmp_path, monkeypatch):
    from home_study_agent.check_coaching_retry_control import snapshot
    client, db, settings, sid, parent, gateway, retry = failure(tmp_path, monkeypatch)
    try:
        with db.connect() as c:
            c.execute("UPDATE tutor_feedback SET response_text='Different from the original saved ASR'")
            c.commit()
            assert snapshot(c, sid, 'child-junior-3') is None
        assert len(gateway.coaching) == 1
    finally: client.__exit__(None, None, None)

@pytest.mark.parametrize('text', ['重试刚才的点评','请帮我重试一下刚才的点评。','可以帮我重试刚才的点评吗？','能不能再试一次刚才的点评？','麻烦你重试这次的点评','重新请求上次的点评'])
def test_current_explicit_feedback_retry_accepts_polite_requests(text):
    from home_study_agent.voice_control_intent import VoiceControlIntent
    value = dict(role='control', control='learning.retry_check_coaching', quote=text, confidence=.99)
    assert VoiceControlIntent.parse(json.dumps(value, ensure_ascii=False), text).get('status') == 'control'


def test_known_pre_dispatch_failure_retries_saved_check_without_unknown_outcome_confirmation(tmp_path, monkeypatch):
    class ConfiguredGateway(CoachingGateway):
        enabled = True
        @property
        def configured(self): return self.enabled
    gateway = ConfiguredGateway()
    client, db, settings, sid, parent, original = heard_check(tmp_path, monkeypatch, gateway)
    try:
        gateway.enabled = False
        failed = client.post(f"/v1/agent-turns/{original['agent_turn_id']}/execute")
        assert failed.status_code == 200 and failed.json()['execution']['coaching_status'] == 'failed'
        assert len(gateway.coaching) == 0
        gateway.enabled = True
        gateway.quote, gateway.control, gateway.role = QUOTE, 'learning.retry_check_coaching', 'control'
        target = parent['output_envelope']['local_teaching_control']['question_target']['binding']
        reference = {'kind': 'diagnostic_help', 'binding': {k: target[k] for k in ('prompt_id', 'item_version')}}
        voice = capture(client, settings, monkeypatch, sid, QUOTE, reference, key='known-retry-capture', child='child-junior-3')
        retry = interpret(client, sid, QUOTE, 'known-retry-command', voice)
        assert retry.get('runtime_capability') == 'tutor.retry_check_coaching', retry
        assert retry['action']['confirm_unknown_outcome_retry'] is False
        result = client.post(f"/v1/agent-turns/{retry['agent_turn_id']}/execute")
        assert result.status_code == 200, result.text
        assert len(gateway.coaching) == 1
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM tutor_feedback').fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
    finally: client.__exit__(None, None, None)
