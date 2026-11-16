"""Rephotograph through the real capture receipt and same-case confirmation APIs."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.repository import utc_now
from home_study_agent.local_supply import validated_variant_seed, LocalSupplyError
from home_study_agent.mistake_dialogue import MistakeDialogueService
from test_mistake_source_revision_http import Gateway
from test_formula_voice_confirmation import turn
from test_paper_variant_line_projection_http import captured_question, ReorderedEngine, QUESTION, FORMULA_QUESTION, ANSWER, post, gateway_for
from test_paper_variant_authorization_http import CHILD, url, body
from test_writing_question_authorization_http import parent
from test_local_ocr_pipeline import image_bytes


def setup(client, captured):
    db, settings, sid, artifact, mid, candidate = captured
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        MistakeDialogueService._write_focus(c, {'session_id': sid, 'child_id': CHILD,
            'mistake_id': mid, 'candidate_id': candidate['candidate_id'], 'phase': 'remediation',
            'state': {'artifact_id': artifact['artifact_id'], 'question_text': QUESTION, 'subject': '数学'}})
        c.commit()
    return post(client, f'/v1/sessions/{sid}/agent-turn', {
        'transcript': '重新拍题', 'client_action_id': 'recapture-original', 'cloud_text_allowed': False})


def photograph(client, sid, question=QUESTION):
    client.app.state.ocr_workflow.adapter.ocr.engines = [ReorderedEngine(question)]
    artifact = post(client, f'/v1/sessions/{sid}/ocr', {
        'image_base64': base64.b64encode(image_bytes(width=420)).decode(), 'client_action_id': 'rephotographed-original'})
    assert any(b['text'] == question for b in artifact['details']['local_pipeline']['question_blocks'])
    return artifact


def finish(client, requested, artifact):
    receipt = {'capability': 'document.retry_capture', 'execution_location': 'client_device_bridge',
        'action_type': 'retry_capture', 'status': 'completed', 'completed_at': utc_now(),
        'output': {'command_id': requested['agent_turn_id'], 'device_status': 'recognized',
            'capture_artifact_id': artifact['artifact_id'], 'artifact_id': artifact['artifact_id'],
            'ocr': {'status': 'completed', 'artifact_id': artifact['artifact_id'], 'error_code': None}}}
    response = client.put(f"/v1/agent-turns/{requested['agent_turn_id']}/outcome", json={
        'final_response': '拍摄结果已保存，等待核对。', 'tool_status': 'completed',
        'result': {'execution_receipt': receipt, 'client_bridge_receipt': receipt}})
    assert response.status_code == 200, response.text


def begin(client, sid, artifact, key='recapture-begin'):
    return client.post(f'/v1/sessions/{sid}/mistake-dialogue', json={
        'artifact_id': artifact['artifact_id'], 'client_action_id': key})


@pytest.mark.parametrize('source_condition', ['normal', 'spoken_revision', 'missing_capture'])
def test_same_case_device_receipt_select_confirm_restart_and_seed(captured_question, source_condition):
    db, settings, sid, original, mid, old_candidate = captured_question
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        request = setup(client, captured_question)
        if source_condition == 'spoken_revision':
            turn(client, sid, '题目应该是' + QUESTION.replace('三十七', '四十一'), 'spoken-before-recapture')
            turn(client, sid, '没错，讲吧', 'spoken-confirm-before-recapture')
            with db.connect() as c:
                with pytest.raises(LocalSupplyError, match='REVIEW_REQUIRED'):
                    validated_variant_seed(c, CHILD, mid)
        if source_condition == 'missing_capture':
            with db.connect() as c:
                c.execute('DELETE FROM capture_artifact WHERE artifact_id=?', (original['artifact_id'],))
                c.commit()
        if source_condition != 'normal':
            request = post(client, f'/v1/sessions/{sid}/agent-turn', {
                'transcript': '重新拍这道错题', 'client_action_id': 'recapture-after-source-change', 'cloud_text_allowed': False})
        old_revisions = int(source_condition == 'spoken_revision')
        assert request['action']['type'] == 'retry_capture', request
        assert request['action']['mistake_recapture_target']['mistake_id'] == mid
        artifact = photograph(client, sid)
        early = begin(client, sid, artifact)
        assert early.status_code == 422 and '回执' in early.text, early.text
        assert MistakeDialogueService(db).state(sid)['mistake_id'] == mid
        finish(client, request, artifact)
        opened = begin(client, sid, artifact)
        assert opened.status_code == 200, opened.text
        assert opened.json()['mistake_id'] == mid
        assert '第几题' in opened.json()['response_text']
        assert opened.json()['state']['question_confirmation']['question'] == ''
        selected = turn(client, sid, '第一题', 'recapture-pick')
        assert QUESTION in selected['turn']['response_text']
        assert ANSWER in selected['turn']['response_text']
        assert selected['execution']['result']['mistake_id'] == mid
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == old_revisions
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        confirmed = turn(client, sid, '没错，讲吧', 'recapture-confirm', gateway=gateway)
        assert confirmed['execution'].get('tutor_turn'), confirmed
        assert confirmed['execution']['tutor_turn']['question'] == QUESTION
        assert turn(client, sid, '没错，讲吧', 'recapture-confirm', gateway=gateway) == confirmed
        with db.connect() as c:
            case = c.execute('SELECT * FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()
            assert case['source_ref'].startswith(artifact['artifact_id'] + '#')
            assert case['submitted_answer'] == ANSWER
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 1
            revisions = c.execute('SELECT * FROM mistake_source_revision').fetchall()
            assert len(revisions) == 1 + old_revisions
            assert json.loads(revisions[-1]['before_json'])['case']['source_ref'] == original['artifact_id'] + '#' + old_candidate['candidate_id']
            seed = validated_variant_seed(c, CHILD, mid)
            assert seed['prompt'] == QUESTION and seed['expected_answer_json'] == '{}'
            assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        assert len(gateway.posts) == 1
        for private in (ANSWER, mid, original['artifact_id'], '2. 计算6加7。'):
            assert private not in gateway.posts[0]
    generated = []
    with TestClient(create_app(settings, model_gateway=gateway_for(settings, generated))) as client:
        headers = parent(client)
        view = client.get(url(mid), headers=headers).json()
        assert view['status'] == 'authorization_required', view
        post(client, url(mid) + '/grant', body(view), headers=headers)
        job = post(client, '/v1/parent/variant-jobs/process-next', {}, headers=headers)['job']
        assert job['status'] == 'completed', job
        candidates = client.get(f'/v1/parent/children/{CHILD}/variant-candidates', headers=headers).json()['items']
        candidate = next(c for c in candidates if c['stage'] == 'near_transfer')
        approved = post(client, f"/v1/parent/variant-candidates/{candidate['candidate_id']}/review", {
            'decision': 'approve', 'note': 'Synthetic arithmetic independently checked',
            'client_action_id': 'recaptured-variant-approved'}, headers=headers)
        from home_study_agent.mistake_book import MistakeBookService
        book = MistakeBookService(db)
        book.add_remediation(mid, CHILD, 'active_recall', 'completed', None, 'recaptured-method-recalled')
        answers = {'计算18与7的和乘以2，只回答一个整数。': '50', '先把90除以3再减去8，只回答结果。': '22',
                   '把50的一半加上14，只回答一个整数。': '39', '求三个12相加的和，只回答一个整数。': '36'}
        result = book.submit_attempt(mid, CHILD, 'near_transfer', approved['diagnostic_item_id'],
            answers[candidate['prompt']], 0, 'recaptured-independent-answer', _session_id=sid)
        assert result['attempt_result']['correctness'] == 1
        assert result['mastery_gate']['satisfied'].get('near_transfer') == 1
        assert result['status'] != 'mastered'
        assert len(generated) == 1


@pytest.mark.parametrize('change', ['source', 'pending_answer', 'other_focus', 'superseded_capture'])
def test_late_frame_never_replaces_changed_original(captured_question, change):
    db, settings, sid, _, mid, _ = captured_question
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        request = setup(client, captured_question)
        artifact = photograph(client, sid)
        finish(client, request, artifact)
        if change == 'superseded_capture':
            post(client, f'/v1/sessions/{sid}/agent-turn', {
                'transcript': '重新拍题', 'client_action_id': 'newer-recapture-request', 'cloud_text_allowed': False})
        with db.connect() as c:
            if change == 'source':
                c.execute('UPDATE mistake_case SET prompt_summary=? WHERE mistake_id=?', ('另一份修订', mid))
            elif change != 'superseded_capture':
                focus = MistakeDialogueService(db).state(sid)
                if change == 'pending_answer':
                    focus['state']['pending_operation'] = {'action_id': 'saving-answer', 'utterance': '42'}
                else:
                    focus['mistake_id'] = None
                MistakeDialogueService._write_focus(c, focus)
            c.commit()
        result = begin(client, sid, artifact)
        assert result.status_code == 422, result.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 1


def test_changed_selected_candidate_cannot_be_confirmed(captured_question):
    db, settings, sid, original, mid, _ = captured_question
    with TestClient(create_app(settings, model_gateway=Gateway(settings))) as client:
        request = setup(client, captured_question)
        artifact = photograph(client, sid)
        finish(client, request, artifact)
        assert begin(client, sid, artifact).status_code == 200
        chosen = turn(client, sid, '第一题', 'pick-changed')['execution']['result']['state']['question_confirmation']
        with db.connect() as c:
            c.execute('UPDATE mistake_capture_candidate SET question_text=? WHERE candidate_id=?',
                      ('新内容不能按旧读回确认', chosen['recapture']['candidate_id']))
            c.commit()
        request = turn(client, sid, '没错，讲吧', 'confirm-changed', execute=False)
        response = client.post(f"/v1/agent-turns/{request['agent_turn_id']}/execute", json={})
        assert response.status_code == 422, response.text
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
            assert c.execute('SELECT source_ref FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0].startswith(original['artifact_id'])


def test_recaptured_formula_requires_independent_review_and_undo_revokes_it(captured_question):
    from home_study_agent.mistake_source_revision import current
    db, settings, sid, _, mid, _ = captured_question
    gateway = Gateway(settings)
    corrected = FORMULA_QUESTION.replace('37', '39')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        request = setup(client, captured_question)
        artifact = photograph(client, sid, FORMULA_QUESTION)
        finish(client, request, artifact)
        assert begin(client, sid, artifact).status_code == 200
        turn(client, sid, '第一题', 'formula-recapture-pick')
        turn(client, sid, '没错，讲吧', 'formula-recapture-confirm')
        headers = parent(client)
        view = client.get(url(mid), headers=headers).json()
        assert view['status'] == 'blocked', view
        rows = client.get(f"/v1/capture-artifacts/{artifact['artifact_id']}/ocr-review-items",
                          params={'child_id': CHILD}, headers=headers).json()['items']
        review = next(r for r in rows if r['review_kind'] == 'formula_question')
        response = client.put(f"/v1/ocr-review-items/{review['review_id']}", headers=headers, json={
            'child_id': CHILD, 'corrected_text': corrected, 'client_action_id': 'recapture-formula-independent-review'})
        assert response.status_code == 200, response.text
        view = client.get(url(mid), headers=headers).json()
        assert view['status'] == 'authorization_required' and view['question_preview'] == corrected, view
        with db.connect() as c:
            assert current(c, mid, CHILD)['question'] == corrected
            assert validated_variant_seed(c, CHILD, mid)['prompt'] == corrected
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
            'child_id': CHILD, 'client_action_id': 'undo-recapture-formula-review'}, headers=headers)
        with db.connect() as c:
            assert current(c, mid, CHILD)['status'] == 'invalidated'
            with pytest.raises(LocalSupplyError, match='REVIEW_REQUIRED'):
                validated_variant_seed(c, CHILD, mid)


@pytest.mark.parametrize('failure', ['confirmation', 'receipt'])
def test_recapture_transaction_rolls_back_source_link_and_recovers_once(captured_question, monkeypatch, failure):
    from home_study_agent.tutor_ocr_confirmation import OcrTutorConfirmationService
    db, settings, sid, original, mid, _ = captured_question
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        request = setup(client, captured_question)
        artifact = photograph(client, sid)
        finish(client, request, artifact)
        assert begin(client, sid, artifact).status_code == 200
        selected = turn(client, sid, '第一题', 'rollback-recapture-pick')['execution']['result']
        candidate_id = selected['state']['question_confirmation']['recapture']['candidate_id']
        accepted = turn(client, sid, '没错，讲吧', 'rollback-recapture-confirm', execute=False)
        endpoint = f"/v1/agent-turns/{accepted['agent_turn_id']}/execute"
        cls, name = (OcrTutorConfirmationService, 'confirm') if failure == 'confirmation' else (MistakeDialogueService, '_save')
        underlying = getattr(cls, name)
        def fail(*args, **kwargs):
            underlying(*args, **kwargs)
            raise OSError('synthetic recapture persistence failure')
        with monkeypatch.context() as patch:
            patch.setattr(cls, name, fail)
            assert client.post(endpoint, json={}).status_code == 500
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
            assert c.execute('SELECT source_ref FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0].startswith(original['artifact_id'])
            candidate = c.execute('SELECT status,mistake_id FROM mistake_capture_candidate WHERE candidate_id=?', (candidate_id,)).fetchone()
            assert tuple(candidate) == ('pending', None)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        gateway.configured = True
        recovered = post(client, endpoint, {})
        assert recovered['execution'].get('tutor_turn'), recovered
        assert post(client, endpoint, {}) == recovered
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM mistake_capture_candidate_event WHERE client_action_id=?',
                             ('recapture:' + accepted['agent_turn_id'],)).fetchone()[0] == 1


def test_next_day_captured_voice_reconfirms_same_recaptured_question(captured_question, monkeypatch):
    from test_voice_runtime_binding import capture, interpret
    db, settings, sid, _, mid, _ = captured_question
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        request = setup(client, captured_question)
        artifact = photograph(client, sid)
        finish(client, request, artifact)
        assert begin(client, sid, artifact).status_code == 200
        turn(client, sid, '第一题', 'nextday-recapture-pick')
        turn(client, sid, '没错，讲吧', 'nextday-recapture-confirm')
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
            c.commit()
        next_sid = post(client, '/v1/sessions', {'child_id': CHILD, 'client_action_id': 'nextday-recapture-session'}, expected_status=201)['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (next_sid,))
            c.commit()
        reopened = MistakeDialogueService(db).open_review(next_sid, 'nextday-recapture-open', mistake_id=mid)
        assert reopened['state']['question_confirmation']['question'] == QUESTION
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        text = '没错，讲吧'
        voice_id = capture(client, settings, monkeypatch, next_sid, text, key='recapture-nextday-voice', child=CHILD)
        accepted = interpret(client, next_sid, text, 'recapture-nextday-spoken-confirm', voice_id)
        assert accepted['action']['source'] == 'mistake_question_voice_confirmation'
        gateway.configured = True
        result = post(client, f"/v1/agent-turns/{accepted['agent_turn_id']}/execute", {})
        assert result['execution'].get('tutor_turn'), result
        assert result['execution']['tutor_turn']['question'] == QUESTION
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 1
