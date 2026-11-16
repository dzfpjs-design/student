"""Synthetic OCR inference; actual HTTP publication, authorization and generation."""
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelGateway
from test_local_ocr_pipeline import FakeEngine, image_bytes
from test_paper_variant_authorization_http import CHILD, body, url
from test_writing_question_authorization_http import parent


QUESTION = '1. 先求三十七与十三的和，\n再乘四，只回答一个整数。'
FORMULA_QUESTION = '1. 计算 (37+13)×4，\n只回答一个整数。'
OTHER = '2. 计算6加7。'
ANSWER = '孩子原答案不能发送'


class ReorderedEngine(FakeEngine):
    def __init__(self, question=QUESTION):
        self.question = question

    def recognize(self, image, route):
        h, w = image.shape[:2]
        # The synthetic page has text in its upper third. Supply actual
        # direction-dependent test evidence instead of certifying every rotation.
        upright = h > w and float(image[:h // 3].mean()) < float(image[-h // 3:].mean()) - 1
        lines = [{'text': text, 'confidence': .99 if upright else .3, 'kind': 'printed',
            'box': [[int(w * .12), int(h * y)], [int(w * .85), int(h * y)],
                    [int(w * .85), int(h * (y + .04))], [int(w * .12), int(h * (y + .04))]]}
            for text, y in zip(self.question.splitlines() + [OTHER], (.2, .3, .5))]
        return list(reversed(lines))

    recognize_page_orientation = recognize


def post(client, path, payload, expected_status=200, **kwargs):
    response = client.post(path, json=payload, **kwargs)
    assert response.status_code == expected_status, response.text
    return response.json()


@pytest.fixture
def captured_question(tmp_path, request):
    formula = getattr(request, 'param', False)
    question = FORMULA_QUESTION if formula else QUESTION
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / 'paper.db',
        maintenance_enabled=False, data_encryption_key=b'P' * 32,
        model_base_url='https://paper.invalid', model_api_key='synthetic-0281274d98cc06346ee7')
    # No model is configured during initial capture/diagnosis; no asynchronous
    # preparation can race the explicit parent grant tested below.
    with TestClient(create_app(settings, model_gateway=type('LocalOnly', (), {'configured': False})())) as client:
        client.app.state.ocr_workflow.adapter.ocr.engines = [ReorderedEngine(question)]
        sid = post(client, '/v1/sessions', {'child_id': CHILD, 'client_action_id': 'paper-session'}, expected_status=201)['session_id']
        artifact = post(client, f'/v1/sessions/{sid}/ocr', {
            'image_base64': base64.b64encode(image_bytes()).decode(), 'client_action_id': 'reordered-paper'})
        if formula:
            assert artifact['status'] == 'quality_warning'
            assert 'formula_review_required' in artifact['details']['quality']['issues']
        else:
            assert artifact['status'] == 'completed' and artifact['confidence'] == pytest.approx(.99), artifact['details']['quality']
        assert not artifact['local_path']
        assert artifact['details']['local_pipeline']['privacy']['raw_image_uploaded'] is False
        assert question not in artifact['extracted_text']
        candidates = post(client, f"/v1/capture-artifacts/{artifact['artifact_id']}/mistake-candidates/prepare",
            {'child_id': CHILD, 'client_action_id': 'prepare-reordered'})['items']
        candidate = next(c for c in candidates if c['question_text'] == question)
        captured = post(client, f"/v1/mistake-capture-candidates/{candidate['candidate_id']}/capture", {
            'child_id': CHILD, 'client_action_id': 'capture-reordered',
            'knowledge_node_id': 'node-p4-math-arithmetic', 'subject': '数学', 'title': '四则运算', 'submitted_answer': ANSWER})
        mid = captured['mistake']['mistake_id']
        post(client, f'/v1/mistakes/{mid}/confirm', {'child_id': CHILD,
            'decision': 'confirm', 'client_action_id': 'confirm-reordered'})
        post(client, f'/v1/mistakes/{mid}/diagnose', {'child_id': CHILD, 'error_type': 'concept_gap',
            'explanation': '核对运算次序', 'confidence': .7, 'client_action_id': 'diagnose-reordered'})
        headers = parent(client, True)
        consent = client.put('/v1/parent/privacy', headers=headers, json={'cloud_model_text_allowed': True})
        assert consent.status_code == 200, consent.text
    db = Database(settings.database_path)
    with db.connect() as c:
        assert json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
            (artifact['artifact_id'],)).fetchone()[0]) == artifact['details']
    return db, settings, sid, artifact, mid, candidate


def gateway_for(settings, posts, mutate=None, question=QUESTION):
    async def transport(request):
        assert request.url.host == 'paper.invalid' and request.method == 'POST'
        payload = json.loads(request.content)
        posts.append(payload)
        context = json.loads(payload['messages'][-1]['content'])
        assert context['original_prompt'] == question
        assert context['original_rubric'] == {} and context['original_answer_verified'] is False
        assert OTHER not in request.content.decode() and ANSWER not in request.content.decode()
        prompts = ['计算18与7的和乘以2，只回答一个整数。', '先把90除以3再减去8，只回答结果。',
                   '把50的一半加上14，只回答一个整数。', '求三个12相加的和，只回答一个整数。']
        candidates = [{'stage': stage, 'prompt': prompts[i], 'answer_type': 'text',
            'expected_answer': {'exact_answers': [str([50, 22, 39, 36][i])], 'answer_format': 'rational'},
            'explanation': '按题目指定的运算顺序计算，再核对结果。', 'difficulty': 1}
            for i, stage in enumerate(context['required_stages'])]
        if mutate:
            mutate()
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'candidates': candidates})}}]})
    return ModelGateway(settings, transport=httpx.MockTransport(transport))


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_unreviewed_formula_remains_blocked_not_certified_by_line_order(captured_question):
    _, settings, _, _, mid, _ = captured_question
    posts = []
    with TestClient(create_app(settings, model_gateway=gateway_for(settings, posts))) as client:
        headers = parent(client)
        view = client.get(url(mid), headers=headers).json()
        assert view['status'] == 'blocked' and view['reason'] == 'PAPER_VARIANT_SOURCE_REVIEW_REQUIRED'
        assert view['question_version'] is None
        assert posts == []


def test_actual_ocr_reordered_question_grant_generation_restart_and_revoke(captured_question):
    db, settings, _, artifact, mid, _ = captured_question
    posts = []
    with TestClient(create_app(settings, model_gateway=gateway_for(settings, posts))) as client:
        headers = parent(client)
        view = client.get(url(mid), headers=headers).json()
        assert view['reason'] is None, view['reason']
        assert view['status'] == 'authorization_required'
        assert view['question_preview'] == QUESTION
        payload = body(view)
        allowed = post(client, url(mid) + '/grant', payload, headers=headers)
        job = post(client, '/v1/parent/variant-jobs/process-next', {}, headers=headers)['job']
        assert job['status'] == 'completed' and job['job_id'] == allowed['generation_job']['job_id']
        items = client.get(f'/v1/parent/children/{CHILD}/variant-candidates', headers=headers).json()['items']
        assert len(items) == 4
        assert len(posts) == 1
        wire = json.dumps(posts, ensure_ascii=False)
        for local in (artifact['artifact_id'], mid, 'question_projection', 'line_refs', OTHER, ANSWER):
            assert local not in wire
    with TestClient(create_app(settings, model_gateway=gateway_for(settings, posts))) as client:
        headers = parent(client)
        replay = post(client, url(mid) + '/grant', payload, headers=headers)
        assert replay['receipt'] == allowed['receipt']
        assert replay['current']['status'] == 'authorized'
        assert replay['generation_job']['status'] == 'completed'
        revoked = post(client, url(mid) + '/revoke', {'expected_revision': 1,
            'client_action_id': 'revoke-reordered'}, headers=headers)
        assert revoked['current']['status'] == 'revoked'
        assert post(client, '/v1/parent/variant-jobs/process-next', {}, headers=headers)['job'] is None
        assert client.get(url(mid, 'child-junior-3'), headers=headers).status_code == 404
    assert len(posts) == 1
    with db.connect() as c:
        row = c.execute('SELECT extracted_text,details_json FROM capture_artifact WHERE artifact_id=?',
            (artifact['artifact_id'],)).fetchone()
        assert row[0] == artifact['extracted_text'] and json.loads(row[1]) == artifact['details']


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_reviewed_formula_question_seeds_only_current_text_and_undo_invalidates(captured_question):
    db, settings, _, artifact, mid, _ = captured_question
    posts = []
    corrected = FORMULA_QUESTION.replace('37', '38')
    with TestClient(create_app(settings, model_gateway=gateway_for(settings, posts, question=corrected))) as client:
        headers = parent(client)
        reviews = client.get(f"/v1/capture-artifacts/{artifact['artifact_id']}/ocr-review-items",
                             params={'child_id': CHILD}, headers=headers).json()['items']
        review = next(r for r in reviews if r['review_kind'] == 'formula_question' and r['proposed_text'] == FORMULA_QUESTION)
        result = client.put(f"/v1/ocr-review-items/{review['review_id']}", headers=headers, json={
            'child_id': CHILD, 'corrected_text': corrected, 'client_action_id': 'formula-corrected-full'})
        assert result.status_code == 200, result.text
        view = client.get(url(mid), headers=headers).json()
        assert view['status'] == 'authorization_required', view
        assert view['question_preview'] == corrected
        granted = post(client, url(mid) + '/grant', body(view), headers=headers)
        job = post(client, '/v1/parent/variant-jobs/process-next', {}, headers=headers)['job']
        assert job['status'] == 'completed', job
        assert len(posts) == 1
        wire = json.dumps(posts, ensure_ascii=False)
        for local in (artifact['artifact_id'], review['review_id'], mid, OTHER, ANSWER, FORMULA_QUESTION):
            assert local not in wire
    with TestClient(create_app(settings, model_gateway=gateway_for(settings, posts, question=corrected))) as client:
        headers = parent(client)
        assert client.get(url(mid), headers=headers).json()['status'] == 'authorized'
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo",
             {'child_id': CHILD, 'client_action_id': 'formula-correction-undo'}, headers=headers)
        assert client.get(url(mid), headers=headers).json()['status'] == 'invalidated'
        with db.connect() as c:
            c.execute('BEGIN')
            from home_study_agent.paper_variant_authorization import validate_job_authorization
            with pytest.raises(ValueError, match='REVOKED'):
                validate_job_authorization(c, job_id=granted['generation_job']['job_id'])
        assert len(posts) == 1
    with db.connect() as c:
        stored = c.execute('SELECT extracted_text,details_json,confidence,status FROM capture_artifact WHERE artifact_id=?',
                           (artifact['artifact_id'],)).fetchone()
        assert stored['extracted_text'] == artifact['extracted_text'] and json.loads(stored['details_json']) == artifact['details']
        assert stored['confidence'] == artifact['confidence'] and stored['status'] == 'quality_warning'


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_formula_teaching_needs_separate_confirmation_and_rechecks_undo(captured_question):
    from home_study_agent.mistake_dialogue import MistakeDialogueService
    from home_study_agent.tutor_ocr_confirmation import OcrTutorConfirmationService, OcrTutorConfirmationError
    db, settings, sid, artifact, mid, _ = captured_question
    dialogue = MistakeDialogueService(db)
    focus = {'session_id': sid, 'child_id': CHILD, 'mistake_id': mid,
             'state': {'teaching_requested': True}}
    corrected = FORMULA_QUESTION.replace('37', '38')
    with TestClient(create_app(settings, model_gateway=type('LocalOnly', (), {'configured': False})())) as client:
        headers = parent(client)
        reviews = client.get(f"/v1/capture-artifacts/{artifact['artifact_id']}/ocr-review-items",
                             params={'child_id': CHILD}, headers=headers).json()['items']
        review = next(r for r in reviews if r['review_kind'] == 'formula_question')
        assert dialogue.teaching_request(focus) is None
        rejected = client.post('/v1/ocr-review-items/batch', headers=headers, json={
            'child_id': CHILD, 'review_ids': [review['review_id']], 'decision': 'confirm_proposed',
            'client_action_id': 'formula-batch-not-allowed'})
        assert rejected.status_code == 422, rejected.text
        result = client.put(f"/v1/ocr-review-items/{review['review_id']}", headers=headers, json={
            'child_id': CHILD, 'corrected_text': corrected, 'client_action_id': 'formula-teaching-correct'})
        assert result.status_code == 200, result.text
        detail = dialogue.book.get(mid, CHILD)
        assert detail['prompt_summary'] == corrected
        assert detail['recorded_prompt_summary'] == FORMULA_QUESTION
        assert detail['question_review']['cloud_transmission_allowed'] is False
        assert dialogue.teaching_request(focus) is None
        confirmation = post(client, f'/v1/sessions/{sid}/tutor-ocr-confirmations', {
            'artifact_id': artifact['artifact_id'], 'confirmed_text': corrected,
            'confirmation_kind': 'manual_correction', 'client_action_id': 'formula-tutor-confirm'})
        teaching = dialogue.teaching_request(focus)
        assert teaching['question'] == corrected
        assert teaching['ocr_confirmation_id'] == confirmation['confirmation_id']
    with TestClient(create_app(settings, model_gateway=type('LocalOnly', (), {'configured': False})())) as client:
        headers = parent(client)
        assert dialogue.teaching_request(focus) == teaching
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo",
             {'child_id': CHILD, 'client_action_id': 'formula-teaching-undo'}, headers=headers)
        assert dialogue.teaching_request(focus) is None
        with pytest.raises(OcrTutorConfirmationError, match='核对'):
            OcrTutorConfirmationService(db).resolve_for_tutor(sid, confirmation['confirmation_id'])
        result = client.put(f"/v1/ocr-review-items/{review['review_id']}", headers=headers, json={
            'child_id': CHILD, 'corrected_text': corrected, 'client_action_id': 'formula-teaching-reconfirm'})
        assert result.status_code == 200, result.text
        # Even the same correction after undo is a new review version.
        assert dialogue.teaching_request(focus) is None


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('undo_during_model', [False, True])
def test_formula_tutor_http_rejects_late_result_after_review_undo(captured_question, undo_during_model):
    db, settings, sid, artifact, mid, _ = captured_question
    posts = []
    corrected = FORMULA_QUESTION.replace('37', '38')
    async def transport(request):
        assert request.url.host == 'paper.invalid'
        posts.append(json.loads(request.content))
        wire = request.content.decode()
        assert '38+13' in wire
        for private in (OTHER, ANSWER, artifact['artifact_id'], mid, 'formula_review_json', 'source_projection'):
            assert private not in wire
        if undo_during_model:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as parent_client:
                undone = await parent_client.post(f"/v1/ocr-review-items/{review['review_id']}/undo",
                    headers=headers, json={'child_id': CHILD, 'client_action_id': 'formula-inflight-undo'})
                assert undone.status_code == 200, undone.text
        return httpx.Response(200, json={'choices': [{'message': {'content': '先计算括号里的和，再乘四。你能说明第一步吗？'}}],
                                         'usage': {'prompt_tokens': 10, 'completion_tokens': 12}})
    app = create_app(settings, model_gateway=ModelGateway(settings, transport=httpx.MockTransport(transport)))
    with TestClient(app) as client:
        headers = parent(client)
        reviews = client.get(f"/v1/capture-artifacts/{artifact['artifact_id']}/ocr-review-items",
                             params={'child_id': CHILD}, headers=headers).json()['items']
        review = next(r for r in reviews if r['review_kind'] == 'formula_question')
        changed = client.put(f"/v1/ocr-review-items/{review['review_id']}", headers=headers, json={
            'child_id': CHILD, 'corrected_text': corrected, 'client_action_id': 'formula-http-edit'})
        assert changed.status_code == 200, changed.text
        confirmation = post(client, f'/v1/sessions/{sid}/tutor-ocr-confirmations', {
            'artifact_id': artifact['artifact_id'], 'confirmed_text': corrected,
            'confirmation_kind': 'manual_correction', 'client_action_id': 'formula-http-confirm'})
        taught = client.post(f'/v1/sessions/{sid}/tutor', json={'subject': '数学', 'question': corrected,
            'hint_level': 1, 'client_action_id': 'formula-http-teaching',
            'ocr_confirmation_id': confirmation['confirmation_id']})
        assert taught.status_code == (409 if undo_during_model else 200), taught.text
        assert len(posts) == 1
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM tutor_turn WHERE session_id=?', (sid,)).fetchone()[0] == (0 if undo_during_model else 1)


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_existing_fragment_review_is_preserved_when_full_question_is_added(captured_question):
    from home_study_agent.local_ocr.review_queue import OcrReviewQueue
    from home_study_agent.paper_formula_review import for_mistake
    db, _, sid, artifact, mid, _ = captured_question
    with db.connect() as c:
        old = c.execute("SELECT review_id FROM ocr_review_item WHERE artifact_id=? AND review_kind='formula_question'",
                        (artifact['artifact_id'],)).fetchone()[0]
        c.execute("UPDATE ocr_review_item SET review_kind='formula',proposed_text='old fragment',status='confirmed',corrected_text='37+13' WHERE review_id=?", (old,))
        c.commit()
        before = dict(c.execute('SELECT * FROM ocr_review_item WHERE review_id=?', (old,)).fetchone())
        assert for_mistake(c, mid, CHILD)['status'] == 'pending'
    queue = OcrReviewQueue(db)
    prepared = queue.prepare(artifact, CHILD, sid, 'upgrade-full-question')
    full = [r for r in prepared['items'] if r['review_kind'] == 'formula_question']
    assert len(full) == 1 and full[0]['status'] == 'pending'
    assert queue.prepare(artifact, CHILD, sid, 'upgrade-full-question-again') == prepared
    with db.connect() as c:
        assert dict(c.execute('SELECT * FROM ocr_review_item WHERE review_id=?', (old,)).fetchone()) == before
