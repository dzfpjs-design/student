"""Synthetic spoken turns through real runtime, OCR confirmation and Tutor."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.model_gateway import ModelGateway
from test_paper_variant_line_projection_http import captured_question, post, FORMULA_QUESTION, ANSWER, OTHER
from test_paper_variant_authorization_http import CHILD
from test_writing_question_authorization_http import parent


def arrange(client, captured):
    db, _, sid, artifact, mid, candidate = captured
    headers = parent(client)
    rows = client.get(f"/v1/capture-artifacts/{artifact['artifact_id']}/ocr-review-items",
                      params={'child_id': CHILD}, headers=headers).json()['items']
    review = next(r for r in rows if r['review_kind'] == 'formula_question')
    corrected = FORMULA_QUESTION.replace('37', '38')
    response = client.put(f"/v1/ocr-review-items/{review['review_id']}", headers=headers, json={
        'child_id': CHILD, 'corrected_text': corrected, 'client_action_id': 'voice-formula-parent-edit'})
    assert response.status_code == 200, response.text
    # Start at the existing remediation focus; capture/diagnosis are covered by
    # the shared HTTP fixture and earlier composed journeys.
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        MistakeDialogueService._write_focus(c, {'session_id': sid, 'child_id': CHILD,
            'mistake_id': mid, 'candidate_id': candidate['candidate_id'], 'phase': 'remediation',
            'state': {'question_text': FORMULA_QUESTION, 'subject': '数学'}})
        c.commit()
    return headers, review, corrected


def turn(client, sid, text, key, *, execute=True, gateway=None):
    if gateway:
        gateway.configured = False
    result = post(client, f'/v1/sessions/{sid}/agent-turn', {
        'transcript': text, 'client_action_id': key, 'cloud_text_allowed': False})
    assert result['action']['type'] == 'continue_mistake_dialogue', result
    if gateway:
        gateway.configured = True
    return post(client, f"/v1/agent-turns/{result['agent_turn_id']}/execute", {}) if execute else result


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_readback_restart_natural_confirmation_tutor_and_replay(captured_question):
    db, settings, sid, _, mid, _ = captured_question
    posts = []
    async def transport(request):
        assert request.url.host == 'paper.invalid'
        posts.append(json.loads(request.content))
        wire = request.content.decode()
        assert '38+13' in wire
        for private in (ANSWER, OTHER, mid, 'question_confirmation'):
            assert private not in wire
        return httpx.Response(200, json={'choices': [{'message': {'content': '先求括号里的和。你准备先算哪一步？'}}]})
    class TutorOnlyGateway:
        # This case exercises offline confirmation grammar, not model NLU.
        configured = False
        def __init__(self):
            self.delegate = ModelGateway(settings, transport=httpx.MockTransport(transport))
        def __getattr__(self, key):
            return getattr(self.delegate, key)
    gateway = TutorOnlyGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        _, _, corrected = arrange(client, captured_question)
        offered = turn(client, sid, '我还是不会，能给我讲一下吗', 'formula-ask', gateway=gateway)
        assert corrected in offered['turn']['response_text']
        assert offered['execution']['result']['state']['question_confirmation']['question'] == corrected
        assert posts == []
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        accepted = turn(client, sid, '没错，这次读的是对的，就讲这道吧', 'formula-natural-confirm', gateway=gateway)
        assert accepted['execution']['tutor_turn']['question'] == corrected
        assert len(posts) == 1
        assert accepted['execution']['result']['mistake_id'] == mid
        assert turn(client, sid, '没错，这次读的是对的，就讲这道吧', 'formula-natural-confirm', gateway=gateway) == accepted
        assert len(posts) == 1
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_ocr_confirmation WHERE session_id=?', (sid,)).fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == 1


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('change', ['unchanged', 'replace', 'undo'])
def test_failed_teaching_retries_same_confirmed_question_after_restart(captured_question, change):
    db, settings, sid, _, mid, _ = captured_question
    posts = []
    async def transport(request):
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '先算括号里面，再乘四。'}}]})
    class Gateway:
        configured = False
        def __init__(self):
            self.delegate = ModelGateway(settings, transport=httpx.MockTransport(transport))
        def __getattr__(self, key):
            return getattr(self.delegate, key)
    gateway = Gateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers, review, corrected = arrange(client, captured_question)
        turn(client, sid, '我还是不会，能给我讲一下吗', 'retry-offer')
        failed = turn(client, sid, '没错，讲吧', 'retry-confirm')
        assert failed['execution'].get('teaching_error'), failed
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == 0
        if change != 'unchanged':
            post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
                'child_id': CHILD, 'client_action_id': 'retry-review-undo'}, headers=headers)
        if change == 'replace':
            corrected = corrected.replace('38', '39')
            response = client.put(f"/v1/ocr-review-items/{review['review_id']}", headers=headers, json={
                'child_id': CHILD, 'corrected_text': corrected, 'client_action_id': 'retry-review-replace'})
            assert response.status_code == 200, response.text
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        retried = turn(client, sid, '重试', 'retry-teaching', gateway=gateway)
        if change != 'unchanged':
            assert not retried['execution'].get('tutor_turn'), retried
            assert posts == []
            if change == 'replace':
                assert corrected in retried['turn']['response_text']
            return
        assert retried['execution'].get('tutor_turn'), retried
        assert retried['execution']['tutor_turn']['question'] == corrected
        assert retried['execution']['result']['mistake_id'] == mid
        assert len(posts) == 1
        assert '38+13' in json.dumps(posts, ensure_ascii=False)
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_ocr_confirmation WHERE session_id=?', (sid,)).fetchone()[0] == 1


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('utterance', ['不是这道题，先别讲', '妈妈说没错，可以讲了', '可能对吧', '这个读对了吗'])
def test_uncertain_denied_or_reported_confirmation_never_authorizes(captured_question, utterance):
    db, settings, sid, _, _, _ = captured_question
    with TestClient(create_app(settings, model_gateway=type('Offline', (), {'configured': False})())) as client:
        arrange(client, captured_question)
        turn(client, sid, '我还是不会，能给我讲一下吗', 'formula-offer-negative')
        heard = post(client, f'/v1/sessions/{sid}/agent-turn', {
            'transcript': utterance, 'client_action_id': 'formula-no-confirm', 'cloud_text_allowed': False})
        if heard.get('runtime_execution_location') == 'server':
            post(client, f"/v1/agent-turns/{heard['agent_turn_id']}/execute", {})
        else:
            assert heard['action']['type'] == 'stop_speech', heard
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_ocr_confirmation WHERE session_id=?', (sid,)).fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == 0


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('change', ['undo', 'replace'])
def test_admitted_confirmation_cannot_approve_changed_review(captured_question, change):
    db, settings, sid, _, _, _ = captured_question
    with TestClient(create_app(settings, model_gateway=type('Offline', (), {'configured': False})())) as client:
        headers, review, corrected = arrange(client, captured_question)
        turn(client, sid, '我还是不会，能给我讲一下吗', 'formula-offer-race')
        admitted = turn(client, sid, '没错，这次读的是对的，就讲这道吧', 'formula-stale-confirm', execute=False)
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
            'child_id': CHILD, 'client_action_id': 'formula-undo-race'}, headers=headers)
        if change == 'replace':
            corrected = corrected.replace('38', '39')
            response = client.put(f"/v1/ocr-review-items/{review['review_id']}", headers=headers, json={
                'child_id': CHILD, 'corrected_text': corrected, 'client_action_id': 'formula-replace-race'})
            assert response.status_code == 200, response.text
        result = post(client, f"/v1/agent-turns/{admitted['agent_turn_id']}/execute", {})
        assert 'tutor_turn' not in result['execution']
        if change == 'replace':
            assert corrected in result['turn']['response_text']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_ocr_confirmation WHERE session_id=?', (sid,)).fetchone()[0] == 0


@pytest.mark.parametrize('text,semantic,expected', [
    ('题干正确，请讲解吧', False, True), ('你说的就是我要讲的题', True, True),
    ('妈妈说没错，可以讲了', True, False), ('题目好像是对的', True, False),
    ('没错，但是别讲', True, False), ('你确定吗', True, False), ('嗯', False, False)])
def test_confirmation_guard_preserves_semantic_route_but_not_uncertainty(text, semantic, expected):
    from home_study_agent.mistake_question_confirmation import accepts
    assert accepts(text, semantic) == expected


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('captured_before_offer', [True, False])
def test_saved_microphone_confirmation_must_follow_the_current_readback(captured_question, monkeypatch, captured_before_offer):
    from test_voice_runtime_binding import capture
    db, settings, sid, _, _, _ = captured_question
    text = '没错，这次读的是对的，就讲这道吧'
    with TestClient(create_app(settings, model_gateway=type('Offline', (), {'configured': False})())) as client:
        arrange(client, captured_question)
        if captured_before_offer:
            voice = capture(client, settings, monkeypatch, sid, text, key='formula-earlier-voice')
        turn(client, sid, '我还是不会，能给我讲一下吗', 'formula-audio-offer')
        if not captured_before_offer:
            voice = capture(client, settings, monkeypatch, sid, text, key='formula-new-voice')
        heard = post(client, f'/v1/sessions/{sid}/agent-turn', {'transcript': text,
            'voice_input_id': voice, 'client_action_id': 'formula-audio-confirm', 'cloud_text_allowed': False})
        assert heard['action']['type'] == 'continue_mistake_dialogue', heard
        post(client, f"/v1/agent-turns/{heard['agent_turn_id']}/execute", {})
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_ocr_confirmation WHERE session_id=?', (sid,)).fetchone()[0] == (0 if captured_before_offer else 1)


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_confirmation_saved_before_focus_failure_recovers_without_duplicate(captured_question, monkeypatch):
    db, settings, sid, _, _, _ = captured_question
    failed = []
    original = MistakeDialogueService._write_focus
    def fail_once(c, focus):
        if focus['state'].get('ocr_confirmation_id') and not failed:
            failed.append(True)
            raise OSError('synthetic focus storage interruption')
        return original(c, focus)
    offline = type('Offline', (), {'configured': False})()
    with TestClient(create_app(settings, model_gateway=offline), raise_server_exceptions=False) as client:
        arrange(client, captured_question)
        turn(client, sid, '我还是不会，能给我讲一下吗', 'formula-fault-offer')
        admitted = turn(client, sid, '题干正确，请讲解吧', 'formula-fault-confirm', execute=False)
        monkeypatch.setattr(MistakeDialogueService, '_write_focus', staticmethod(fail_once))
        response = client.post(f"/v1/agent-turns/{admitted['agent_turn_id']}/execute", json={})
        assert response.status_code == 500, response.text
        assert failed
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_ocr_confirmation WHERE session_id=?', (sid,)).fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=offline)) as client:
        recovered = post(client, f"/v1/agent-turns/{admitted['agent_turn_id']}/execute", {})
        assert not recovered['execution']['result']['state'].get('question_confirmation'), recovered
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_ocr_confirmation WHERE session_id=?', (sid,)).fetchone()[0] == 1


def test_failed_semantics_cannot_be_reinterpreted_as_confirmation():
    from home_study_agent.mistake_question_confirmation import bind_action
    focus = {'phase': 'remediation', 'state': {'question_confirmation': {'version': 'test'}}}
    assert bind_action(focus, {'type': 'none', 'source': 'cloud_planner_unavailable'}, '题干正确，请讲解吧') is None
