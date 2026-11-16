"""Real local correction/diagnosis persistence; synthetic page OCR, no cloud."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_mistake_gate_reliability import SECOND
from test_paper_variant_line_projection_http import (
    CHILD, FORMULA_QUESTION, captured_question, parent, post, gateway_for, url, body,
)


def app(settings):
    return create_app(settings, model_gateway=type('LocalOnly', (), {'configured': False})())


def formula_review(client, artifact):
    headers = parent(client)
    result = client.get(f"/v1/capture-artifacts/{artifact['artifact_id']}/ocr-review-items",
                        params={'child_id': CHILD}, headers=headers)
    assert result.status_code == 200, result.text
    return headers, next(r for r in result.json()['items'] if r['review_kind'] == 'formula_question')


def correct(client, headers, review, question):
    result = client.put(f"/v1/ocr-review-items/{review['review_id']}", headers=headers, json={
        'child_id': CHILD, 'corrected_text': question, 'client_action_id': str(uuid4())})
    assert result.status_code == 200, result.text


def enter_study(client, sid):
    def transition(before, after):
        post(client, f'/v1/sessions/{sid}/transition', {'expected_state': before,
            'target_state': after, 'reason': 'Synthetic correction journey', 'client_action_id': str(uuid4())})
    transition('check_in', 'classroom_recall')
    post(client, f'/v1/sessions/{sid}/recalls/none', {'client_action_id': str(uuid4())})
    transition('classroom_recall', 'homework_capture')
    post(client, f'/v1/sessions/{sid}/homework', {'subject': '数学', 'title': '核对运算次序',
        'expected_minutes': 10, 'client_action_id': str(uuid4())}, expected_status=201)
    post(client, f'/v1/sessions/{sid}/plan/generate', {'client_action_id': str(uuid4())})
    transition('plan_review', 'study')


def hypotheses(client, path, version=0, supersedes=None):
    return post(client, path + '/hypotheses', {
        'child_id': CHILD, 'expected_version': version, 'client_action_id': str(uuid4()),
        'supersedes_event_id': supersedes,
        'hypotheses': [{'error_type': 'concept_gap', 'stance': 'candidate',
                       'explanation': '需要通过独立作答核对运算顺序。'},
                      {'error_type': 'incidental', 'stance': 'candidate',
                       'explanation': '也可能只是这一次计算失误。'}]})


def probe(client, path, state):
    predictions = {h['hypothesis_id']: 'incorrect' if h['error_type'] == 'concept_gap' else 'correct'
                   for h in state['hypotheses'] if h['stance'] == 'candidate'}
    return client.post(path + '/probes', json={
        'child_id': CHILD, 'expected_version': state['version'], 'client_action_id': str(uuid4()),
        'diagnostic_item_id': SECOND, 'predictions': predictions})


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_formula_correction_changes_diagnosis_not_history_or_independent_answer(captured_question):
    db, settings, sid, artifact, mid, _ = captured_question
    path = f'/v1/mistakes/{mid}/diagnosis'
    first = FORMULA_QUESTION.replace('37', '38')
    second = FORMULA_QUESTION.replace('37', '39')
    with TestClient(app(settings)) as client:
        enter_study(client, sid)
        headers, review = formula_review(client, artifact)
        correct(client, headers, review, first)
        initial = hypotheses(client, path)
        question = probe(client, path, initial)
        assert question.status_code == 200, question.text
        question = question.json()
        original = question['probes'][0]['binding']['original']
        assert original['prompt_summary'] == first
        assert original['recorded_prompt_summary'] == FORMULA_QUESTION
        assert original['question_review']['review_id'] == review['review_id']
        answered = post(client, path + '/answers', {'child_id': CHILD,
            'expected_version': question['version'], 'client_action_id': str(uuid4()),
            'probe_event_id': question['event_id'], 'answer': '42'})
        score = LearningStateService(db).submit_diagnostic(sid, SECOND, '42', str(uuid4()))
        attached = post(client, path + '/evidence', {'child_id': CHILD,
            'expected_version': answered['version'], 'client_action_id': str(uuid4()),
            'answer_event_id': answered['event_id'], 'evidence_id': score['evidence_id']})
        assert attached['assessment']['support'], (attached['assessment'], attached['probes'])
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
            'child_id': CHILD, 'client_action_id': 'before-second-correction'}, headers=headers)
        correct(client, headers, review, second)

    with TestClient(app(settings)) as client:
        current = client.get(path, params={'child_id': CHILD}).json()
        assert current['history'] == attached['history']
        assert current['version'] == attached['version']
        assert current['projection_version'] != attached['projection_version']
        assert not any(h['original_current'] for h in current['hypotheses'])
        assert current['assessment']['support'] == []
        assert current['next_action']['kind'] == 'revise_hypotheses'
        assert current['probes'][0]['binding_status'] == 'original_version_changed'
        assert probe(client, path, current).status_code == 422
        revised = hypotheses(client, path, current['version'], initial['event_id'])
        assert all(h['original_current'] for h in revised['hypotheses'])
        assert probe(client, path, revised).status_code == 200
        assert client.get(path, params={'child_id': 'child-junior-3'}).status_code == 404
        detail = MistakeBookService(db).get(mid, CHILD)
        assert detail['prompt_summary'] == second
        assert detail['recorded_prompt_summary'] == FORMULA_QUESTION
        assert detail['status'] != 'mastered'
    with db.connect() as c:
        from home_study_agent.learning_obligations import project_child_mastery_evidence
        projection = project_child_mastery_evidence(c, CHILD)['node-p4-math-arithmetic']
        assert score['evidence_id'] in {r['evidence_id'] for r in projection['effective_evidence']}
        assert c.execute('SELECT COUNT(*) FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == 1


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_formula_undo_blocks_probes_and_reconfirmation_requires_new_hypotheses(captured_question):
    _, settings, _, artifact, mid, _ = captured_question
    path = f'/v1/mistakes/{mid}/diagnosis'
    corrected = FORMULA_QUESTION.replace('37', '38')
    with TestClient(app(settings)) as client:
        headers, review = formula_review(client, artifact)
        correct(client, headers, review, corrected)
        initial = hypotheses(client, path)
        assert probe(client, path, initial).status_code == 200
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
            'child_id': CHILD, 'client_action_id': 'undo-formula'}, headers=headers)
    with TestClient(app(settings)) as client:
        current = client.get(path, params={'child_id': CHILD}).json()
        assert not any(h['original_current'] for h in current['hypotheses'])
        assert current['next_action'] == {'kind': 'blocked', 'reason': 'original_question_review_required'}
        assert probe(client, path, current).status_code == 422
        headers, review = formula_review(client, artifact)
        correct(client, headers, review, corrected)
        restored = client.get(path, params={'child_id': CHILD}).json()
        assert restored['next_action']['kind'] == 'revise_hypotheses'
        assert not any(h['original_current'] for h in restored['hypotheses'])
        revised = hypotheses(client, path, restored['version'], initial['event_id'])
        assert all(h['original_current'] for h in revised['hypotheses'])


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_unreviewed_formula_cannot_authorize_diagnostic_probe(captured_question):
    _, settings, _, _, mid, _ = captured_question
    path = f'/v1/mistakes/{mid}/diagnosis'
    with TestClient(app(settings)) as client:
        state = hypotheses(client, path)
        assert not any(h['original_current'] for h in state['hypotheses'])
        assert state['next_action'] == {'kind': 'blocked', 'reason': 'original_question_review_required'}
        assert probe(client, path, state).status_code == 422


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_current_dialogue_does_not_restore_a_probe_after_original_correction(captured_question):
    db, settings, sid, artifact, mid, _ = captured_question
    path = f'/v1/mistakes/{mid}/diagnosis'
    first = FORMULA_QUESTION.replace('37', '38')
    second = FORMULA_QUESTION.replace('37', '39')
    with TestClient(app(settings)) as client:
        enter_study(client, sid)
        headers, review = formula_review(client, artifact)
        correct(client, headers, review, first)
        initial = hypotheses(client, path)
        assert probe(client, path, initial).status_code == 200
        dialogue = MistakeDialogueService(db)
        opened = dialogue.open_review(sid, 'open-formula-review', mistake_id=mid)
        assert opened['phase'] == 'diagnosis_probe'
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
            'child_id': CHILD, 'client_action_id': 'before-dialogue-correction'}, headers=headers)
        correct(client, headers, review, second)
    with TestClient(app(settings)) as client:
        focus = client.get(f'/v1/sessions/{sid}/mistake-dialogue').json()['dialogue']
        assert focus['phase'] == 'diagnosis'
        assert focus['answer_binding'] is None
        with db.connect() as c:
            from home_study_agent.agent_runtime import AgentRuntimeStore
            assert AgentRuntimeStore.local_mistake_answer_target(c, sid, CHILD) is None
        assert focus['state']['question_text'] == second
        assert not focus['state'].get('diagnosis_probe_id')
        assert not (focus['state'].get('next_activity') or {}).get('item')
        assert '修订' in MistakeDialogueService(db).prompt(focus)
        headers, review = formula_review(client, artifact)
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
            'child_id': CHILD, 'client_action_id': 'undo-active-review'}, headers=headers)
        focus = client.get(f'/v1/sessions/{sid}/mistake-dialogue').json()['dialogue']
        assert '核对' in MistakeDialogueService(db).prompt(focus)
        assert MistakeDialogueService(db).teaching_request(focus) is None
        with db.connect() as c:
            before = c.execute('SELECT COUNT(*) FROM remediation_step WHERE mistake_id=?', (mid,)).fetchone()[0]
        replied = MistakeDialogueService(db).respond(sid, '我懂了', 'unreviewed-not-remediated')
        assert '核对' in replied['response_text']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM remediation_step WHERE mistake_id=?', (mid,)).fetchone()[0] == before
        MistakeDialogueService(db).respond(sid, '暂停', 'pause-formula-review')
    with TestClient(app(settings)) as client:
        paused = client.get(f'/v1/sessions/{sid}/mistake-dialogue').json()['dialogue']
        assert paused['phase'] == 'paused'
        resumed = MistakeDialogueService(db).respond(sid, '继续', 'resume-formula-review')
        assert '核对' in resumed['response_text']
        assert not MistakeDialogueService(db).teaching_request(resumed)


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_revised_paper_invalidates_old_variant_mastery_without_deleting_answer(captured_question):
    db, settings, sid, artifact, mid, _ = captured_question
    corrected = FORMULA_QUESTION.replace('37', '38')
    requests = []
    with TestClient(create_app(settings, model_gateway=gateway_for(settings, requests, question=corrected))) as client:
        enter_study(client, sid)
        headers, review = formula_review(client, artifact)
        correct(client, headers, review, corrected)
        view = client.get(url(mid), headers=headers).json()
        post(client, url(mid) + '/grant', body(view), headers=headers)
        job = post(client, '/v1/parent/variant-jobs/process-next', {}, headers=headers)['job']
        assert job['status'] == 'completed'
        candidates = client.get(f'/v1/parent/children/{CHILD}/variant-candidates', headers=headers).json()['items']
        candidate = next(c for c in candidates if c['stage'] == 'near_transfer')
        answers = {'计算18与7的和乘以2，只回答一个整数。': '50',
                   '先把90除以3再减去8，只回答结果。': '22',
                   '把50的一半加上14，只回答一个整数。': '39',
                   '求三个12相加的和，只回答一个整数。': '36'}
        approved = post(client, f"/v1/parent/variant-candidates/{candidate['candidate_id']}/review",
            {'decision': 'approve', 'note': 'Synthetic independently checked arithmetic',
             'client_action_id': 'approve-formula-variant'}, headers=headers)
        book = MistakeBookService(db)
        book.add_remediation(mid, CHILD, 'active_recall', 'completed', None, 'formula-method-recalled')
        scored = book.submit_attempt(mid, CHILD, 'near_transfer', approved['diagnostic_item_id'], answers[candidate['prompt']], 0,
                                     'formula-variant-answer', _session_id=sid)
        assert scored['attempt_result']['correctness'] == 1
        assert scored['mastery_gate']['satisfied'].get('near_transfer') == 1, scored['mastery_gate']
        from home_study_agent.child_model import ChildModelService
        before_model = ChildModelService(db).snapshot(CHILD)
        node = next(n for n in before_model['payload']['learning']['reliable_feedback']['nodes']
                    if n['knowledge_node_id'] == 'node-p4-math-arithmetic')
        assert node['independent_correct'] == 1
        with db.connect() as c:
            before = dict(c.execute('SELECT * FROM mistake_attempt WHERE client_action_id=?', ('formula-variant-answer',)).fetchone())
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
            'child_id': CHILD, 'client_action_id': 'undo-scored-formula'}, headers=headers)
        correct(client, headers, review, corrected.replace('38', '39'))
    with TestClient(app(settings)):
        current = MistakeBookService(db).get(mid, CHILD)
        assert 'near_transfer' in current['mastery_gate']['missing']
        assert current['mastery_gate']['satisfied'].get('near_transfer', 0) == 0
        assert current['status'] != 'mastered'
        after_model = ChildModelService(db).snapshot(CHILD)
        nodes = after_model['payload']['learning']['reliable_feedback']['nodes']
        assert all(n['independent_correct'] == 0 for n in nodes if n['knowledge_node_id'] == 'node-p4-math-arithmetic')
        with db.connect() as c:
            after = dict(c.execute('SELECT * FROM mistake_attempt WHERE client_action_id=?', ('formula-variant-answer',)).fetchone())
            assert after == before
            from home_study_agent.learning_obligations import project_child_mastery_evidence
            evidence = project_child_mastery_evidence(c, CHILD)['node-p4-math-arithmetic']
            assert before['evidence_id'] not in {r['evidence_id'] for r in evidence['effective_evidence']}
            assert before['evidence_id'] in {r['evidence_id'] for r in evidence['excluded_evidence']}
    assert len(requests) == 1
