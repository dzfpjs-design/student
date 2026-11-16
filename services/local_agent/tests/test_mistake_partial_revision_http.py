"""Partial spoken amendments preserve the other field and the original case."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_cloud_first_agent import Planner
from test_formula_voice_confirmation import turn
from test_mistake_recapture_http import setup
from test_mistake_source_revision_http import Gateway
from test_mistake_formula_diagnosis_http import formula_review, correct
from test_paper_variant_line_projection_http import captured_question, QUESTION, ANSWER, FORMULA_QUESTION, post


def propose(settings, sid, arguments, text, key, *, execute=True):
    planner = Planner('mistake.repair_question', arguments, text)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        planned = post(client, f'/v1/sessions/{sid}/agent-turn', {
            'transcript': text, 'client_action_id': key, 'cloud_text_allowed': True})
        assert planned.get('runtime_capability') == 'mistake.repair_question'
        if not execute:
            return planned
        return post(client, f"/v1/agent-turns/{planned['agent_turn_id']}/execute", {})


@pytest.mark.parametrize('sequence', ['answer_only', 'question_then_answer', 'answer_then_question', 'answer_twice'])
def test_partial_corrections_survive_restart_without_losing_other_field(captured_question, sequence):
    db, settings, sid, _, mid, _ = captured_question
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        setup(client, captured_question)
    question, answer = QUESTION, '二百零八'
    if sequence == 'question_then_answer':
        question = QUESTION.replace('三十七', '四十一')
        propose(settings, sid, {'question_quote': question}, '完整题干改为：' + question, 'partial-first')
    if sequence in {'answer_then_question', 'answer_twice'}:
        propose(settings, sid, {'original_answer_quote': '二百零七'}, '原作答记错了，我写的是二百零七', 'partial-first')
    if sequence == 'answer_then_question':
        question, answer = QUESTION.replace('三十七', '四十一'), '二百零七'
        arguments, text = {'question_quote': question}, '答案不用再改，完整题干改为：' + question
    else:
        arguments, text = {'original_answer_quote': answer}, '题干不用改，我原来的答案是二百零八'
    offered = propose(settings, sid, arguments, text, 'partial-second')
    pending = offered['execution']['result']['state']['question_confirmation']
    assert pending['question'] == question and pending['replacement_answer'] == answer
    assert question in offered['turn']['response_text'] and answer in offered['turn']['response_text']
    with db.connect() as c:
        assert tuple(c.execute('SELECT prompt_summary,submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()) == (QUESTION, ANSWER)
        assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        result = turn(client, sid, '两项都对', 'partial-confirm', gateway=gateway)
        assert result['execution']['tutor_turn']['question'] == question
        assert turn(client, sid, '两项都对', 'partial-confirm', gateway=gateway) == result
    with db.connect() as c:
        assert tuple(c.execute('SELECT prompt_summary,submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()) == (question, answer)
        rows = c.execute('SELECT before_json,offer_json FROM mistake_source_revision').fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0]['before_json'])['case']['submitted_answer'] == ANSWER
        assert json.loads(rows[0]['offer_json'])['replacement_answer'] == answer


def test_late_amendment_cannot_replace_a_newer_unconfirmed_offer(captured_question):
    db, settings, sid, _, _, _ = captured_question
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        setup(client, captured_question)
    propose(settings, sid, {'original_answer_quote': '二百零七'}, '原作答是二百零七', 'first-offer')
    late = propose(settings, sid, {'original_answer_quote': '二百零八'}, '原作答改为二百零八', 'late-offer', execute=False)
    newer = propose(settings, sid, {'original_answer_quote': '二百零九'}, '原作答改为二百零九', 'newer-offer')
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        rejected = client.post(f"/v1/agent-turns/{late['agent_turn_id']}/execute", json={})
        assert rejected.status_code in {409, 422}, rejected.text
        assert MistakeDialogueService(db).state(sid)['state']['question_confirmation'] == newer['execution']['result']['state']['question_confirmation']
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0


def test_offer_save_failure_keeps_prior_proposal_and_can_retry(captured_question, monkeypatch):
    db, settings, sid, _, _, _ = captured_question
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        setup(client, captured_question)
    original = propose(settings, sid, {'original_answer_quote': '二百零七'}, '我原来写的是二百零七', 'offer-before-failure')
    planned = propose(settings, sid, {'original_answer_quote': '二百零八'}, '刚才说错了，原作答是二百零八', 'offer-write-failure', execute=False)
    endpoint = f"/v1/agent-turns/{planned['agent_turn_id']}/execute"
    with TestClient(create_app(settings, model_gateway=Gateway(settings)), raise_server_exceptions=False) as client:
        underlying = MistakeDialogueService._save
        def fail(*args, **kwargs):
            underlying(*args, **kwargs)
            raise OSError('synthetic partial offer save failure')
        with monkeypatch.context() as patch:
            patch.setattr(MistakeDialogueService, '_save', fail)
            assert client.post(endpoint, json={}).status_code == 500
        assert MistakeDialogueService(db).state(sid)['state']['question_confirmation'] == original['execution']['result']['state']['question_confirmation']
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        result = post(client, endpoint, {})
        assert result['execution']['result']['state']['question_confirmation']['replacement_answer'] == '二百零八'
        assert post(client, endpoint, {}) == result


def test_old_joint_confirmation_does_not_confirm_amended_answer(captured_question):
    db, settings, sid, _, mid, _ = captured_question
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        setup(client, captured_question)
    propose(settings, sid, {'original_answer_quote': '二百零七'}, '我原来写的是二百零七', 'old-confirm-offer')
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        old = turn(client, sid, '两项都对', 'old-pair-confirmation', execute=False)
    newer = propose(settings, sid, {'original_answer_quote': '二百零八'}, '原作答再改为二百零八', 'new-confirm-offer')
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        response = client.post(f"/v1/agent-turns/{old['agent_turn_id']}/execute", json={})
        assert response.status_code in {409, 422}, response.text
    assert MistakeDialogueService(db).state(sid)['state']['question_confirmation'] == newer['execution']['result']['state']['question_confirmation']
    with db.connect() as c:
        assert c.execute('SELECT submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == ANSWER
        assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0


@pytest.mark.parametrize('arguments', [{}, {'original_answer_quote': None}, {'original_answer_quote': ''},
    {'original_answer_quote': '不是孩子说的答案'}, {'original_answer_quote': '二百零八', 'extra': 'untrusted'}])
def test_invalid_partial_proposal_does_not_change_pending_or_case(captured_question, arguments):
    db, settings, sid, _, mid, _ = captured_question
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        setup(client, captured_question)
    planner = Planner('mistake.repair_question', arguments, '我的原作答是二百零八')
    before = MistakeDialogueService(db).state(sid)['state'].get('question_confirmation')
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        request = post(client, f'/v1/sessions/{sid}/agent-turn', {
            'transcript': '我的原作答是二百零八', 'client_action_id': 'invalid-partial', 'cloud_text_allowed': True})
        if request.get('runtime_capability') == 'mistake.repair_question':
            rejected = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute", json={})
            assert rejected.status_code in {409, 422}, rejected.text
    assert MistakeDialogueService(db).state(sid)['state'].get('question_confirmation') == before
    with db.connect() as c:
        assert tuple(c.execute('SELECT prompt_summary,submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()) == (QUESTION, ANSWER)
        assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_answer_only_keeps_independently_corrected_formula(captured_question):
    db, settings, sid, artifact, mid, _ = captured_question
    corrected = FORMULA_QUESTION.replace('37', '38')
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        setup(client, captured_question)
        headers, review = formula_review(client, artifact)
        correct(client, headers, review, corrected)
    offered = propose(settings, sid, {'original_answer_quote': '二百零八'}, '题干保持不动，我原作答是二百零八', 'formula-answer-only')
    pending = offered['execution']['result']['state']['question_confirmation']
    assert pending['question'] == corrected
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        result = turn(client, sid, '两项都对', 'formula-answer-confirm', gateway=gateway)
        assert result['execution']['tutor_turn']['question'] == corrected
    with db.connect() as c:
        assert tuple(c.execute('SELECT prompt_summary,submitted_answer FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()) == (FORMULA_QUESTION, '二百零八')
