"""Cancel a pending source replacement, not the original saved mistake."""
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_mistake_recapture_http import setup, photograph, finish, begin
from test_mistake_source_revision_http import Gateway
from test_formula_voice_confirmation import turn
from test_paper_variant_line_projection_http import captured_question, QUESTION, ANSWER, post
from test_paper_variant_authorization_http import CHILD


@pytest.mark.parametrize('kind', ['spoken', 'recapture_unselected', 'recapture_selected'])
def test_cancel_source_offer_restart_preserves_original_and_rejects_late_confirmation(captured_question, kind):
    db, settings, sid, original, mid, candidate = captured_question
    gateway = Gateway(settings)
    artifact = None
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        requested = setup(client, captured_question)
        if kind == 'spoken':
            turn(client, sid, '题目应该是' + QUESTION.replace('三十七', '四十一'), 'cancel-offer')
        else:
            artifact = photograph(client, sid)
            finish(client, requested, artifact)
            assert begin(client, sid, artifact).status_code == 200
            if kind == 'recapture_selected':
                turn(client, sid, '第一题', 'select-to-cancel')
        with db.connect() as c:
            before = dict(c.execute('SELECT * FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone())
        offered = MistakeDialogueService(db).state(sid)['state']['question_confirmation']
        late_confirmation = None
        if kind != 'recapture_unselected':
            late_confirmation = turn(client, sid, '没错，讲吧', 'confirm-before-cancel', execute=False)
        pending_cancel = turn(client, sid, '取消这次更正', 'cancel-source', execute=False)
        assert pending_cancel['action']['source'] == 'mistake_question_voice_cancellation'
        assert pending_cancel['action']['question_confirmation'] == offered
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        cancelled = post(client, f"/v1/agent-turns/{pending_cancel['agent_turn_id']}/execute", {})
        focus = cancelled['execution']['result']
        assert '取消' in cancelled['turn']['response_text']
        assert focus['mistake_id'] == mid
        assert 'question_confirmation' not in focus['state']
        assert focus['state']['question_text'] == QUESTION
        assert focus['state']['submitted_answer'] == ANSWER
        assert focus['state']['artifact_id'] == original['artifact_id']
        assert focus['candidate_id'] == candidate['candidate_id']
        assert 'tutor_turn' not in cancelled['execution']
        if late_confirmation:
            late = client.post(f"/v1/agent-turns/{late_confirmation['agent_turn_id']}/execute", json={})
            assert late.status_code == 422, late.text
        if artifact:
            late = begin(client, sid, artifact, 'reopen-cancelled-recapture')
            assert late.status_code == 422, late.text
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()) == before
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
            assert c.execute('SELECT state FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0] == 'study'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert MistakeDialogueService(db).state(sid)['mistake_id'] == mid
        replay = post(client, f"/v1/agent-turns/{pending_cancel['agent_turn_id']}/execute", {})
        assert replay == cancelled


def test_cancel_bound_to_old_offer_does_not_cancel_new_offer(captured_question):
    db, settings, sid, _, mid, _ = captured_question
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        setup(client, captured_question)
        turn(client, sid, '题目应该是' + QUESTION, 'first-offer')
        pending = turn(client, sid, '取消这次更正', 'stale-cancel', execute=False)
        replacement = turn(client, sid, '题目应该是' + QUESTION.replace('三十七', '四十一'), 'new-offer')
        response = client.post(f"/v1/agent-turns/{pending['agent_turn_id']}/execute", json={})
        assert response.status_code == 422, response.text
        current = MistakeDialogueService(db).state(sid)
        assert current['state']['question_confirmation'] == replacement['execution']['result']['state']['question_confirmation']
        assert current['mistake_id'] == mid


def test_cancel_save_failure_rolls_back_and_retries_after_restart(captured_question, monkeypatch):
    db, settings, sid, _, _, _ = captured_question
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        setup(client, captured_question)
        turn(client, sid, '题目应该是' + QUESTION, 'rollback-offer')
        before = MistakeDialogueService(db).state(sid)['state']['question_confirmation']
        pending = turn(client, sid, '取消这次更正', 'rollback-cancel', execute=False)
        endpoint = f"/v1/agent-turns/{pending['agent_turn_id']}/execute"
        underlying = MistakeDialogueService._save
        def fail(*args, **kwargs):
            underlying(*args, **kwargs)
            raise OSError('synthetic cancellation persistence failure')
        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_save', fail)
            assert client.post(endpoint, json={}).status_code == 500
        assert MistakeDialogueService(db).state(sid)['state']['question_confirmation'] == before
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM mistake_dialogue_event WHERE json_extract(result_json,'$.state.cancelled_source_repair') IS NOT NULL").fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        result = post(client, endpoint, {})
        assert 'question_confirmation' not in result['execution']['result']['state']
        assert post(client, endpoint, {}) == result


@pytest.mark.parametrize('record_before_offer', [False, True])
def test_captured_voice_cancel_requires_recording_after_current_offer(captured_question, monkeypatch, record_before_offer):
    from test_voice_runtime_binding import capture, interpret
    db, settings, sid, _, _, _ = captured_question
    gateway = Gateway(settings)
    text = '取消这次更正'
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        setup(client, captured_question)
        if record_before_offer:
            voice_id = capture(client, settings, monkeypatch, sid, text, key='old-cancel-voice', child=CHILD)
        turn(client, sid, '题目应该是' + QUESTION, 'voice-cancel-offer')
        original_offer = MistakeDialogueService(db).state(sid)['state']['question_confirmation']
        if not record_before_offer:
            voice_id = capture(client, settings, monkeypatch, sid, text, key='fresh-cancel-voice', child=CHILD)
        command = interpret(client, sid, text, 'voice-cancel-command', voice_id)
        assert command['action']['source'] == 'mistake_question_voice_cancellation'
        assert command['action']['voice_input_id'] == voice_id
        response = client.post(f"/v1/agent-turns/{command['agent_turn_id']}/execute", json={})
        assert response.status_code == (422 if record_before_offer else 200), response.text
        state = MistakeDialogueService(db).state(sid)['state']
        if record_before_offer:
            assert state['question_confirmation'] == original_offer
        else:
            assert 'question_confirmation' not in state
