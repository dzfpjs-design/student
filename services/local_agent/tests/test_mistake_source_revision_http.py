"""Spoken correction protocol with synthetic OCR/model and durable HTTP state."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.mistake_source_revision import current
from test_formula_voice_confirmation import arrange, turn
from test_paper_variant_line_projection_http import captured_question, FORMULA_QUESTION, ANSWER, OTHER, post
from test_paper_variant_authorization_http import CHILD


class Gateway:
    configured = False

    def __init__(self, settings):
        self.posts = []
        self.delegate = ModelGateway(settings, transport=httpx.MockTransport(self.reply))

    def __getattr__(self, key):
        return getattr(self.delegate, key)

    async def reply(self, request):
        self.posts.append(request.content.decode())
        return httpx.Response(200, json={'choices': [{'message': {
            'content': '先算括号里的和，再乘四。你准备先算哪一步？'}}]})


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_same_case_readback_confirm_tutor_restart_and_replay(captured_question):
    db, settings, sid, _, mid, _ = captured_question
    gateway = Gateway(settings)
    replacement = FORMULA_QUESTION.replace('37', '41').replace('\n', '')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        arrange(client, captured_question)
        offered = turn(client, sid, '题目应该是' + replacement, 'repair-offer', gateway=gateway)
        assert replacement in offered['turn']['response_text']
        assert offered['execution']['result']['state']['question_confirmation']['mode'] == 'source_repair'
        assert gateway.posts == []
        with db.connect() as c:
            assert c.execute('SELECT prompt_summary FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == FORMULA_QUESTION
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        result = turn(client, sid, '没错，这次读的是对的，就讲这道吧', 'repair-confirm', gateway=gateway)
        assert result['execution'].get('tutor_turn'), result
        assert result['execution']['tutor_turn']['question'] == replacement
        assert result['execution']['result']['mistake_id'] == mid
        assert turn(client, sid, '没错，这次读的是对的，就讲这道吧', 'repair-confirm', gateway=gateway) == result
        assert len(gateway.posts) == 1
        for private in (ANSWER, OTHER, mid, 'question_confirmation'):
            assert private not in gateway.posts[0]
        with db.connect() as c:
            rows = c.execute('SELECT * FROM mistake_source_revision').fetchall()
            assert len(rows) == 1
            assert json.loads(rows[0]['before_json'])['case']['prompt_summary'] == FORMULA_QUESTION
            case = c.execute('SELECT * FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()
            assert case['prompt_summary'] == replacement and case['submitted_answer'] == ANSWER
            assert current(c, mid, CHILD)['status'] == 'confirmed'
            assert current(c, mid, CHILD)['score_authority'] is False


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_second_correction_replaces_only_pending_offer(captured_question):
    db, settings, sid, _, mid, _ = captured_question
    gateway = Gateway(settings)
    first = FORMULA_QUESTION.replace('37', '41').replace('\n', '')
    second = first.replace('41', '42')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        arrange(client, captured_question)
        turn(client, sid, '题目应该是' + first, 'first-offer', gateway=gateway)
        offered = turn(client, sid, '题目应该是' + second, 'second-offer', gateway=gateway)
        assert offered['execution']['result']['state']['question_confirmation']['question'] == second
        assert second in offered['turn']['response_text']
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('later_session', [False, True])
def test_captured_voice_confirmation_owns_same_child_original_and_export_delete(captured_question, monkeypatch, later_session):
    from home_study_agent.mistake_dialogue import MistakeDialogueService
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.local_supply import validated_variant_seed, LocalSupplyError
    from home_study_agent.tutor_ocr_confirmation import OcrTutorConfirmationService, OcrTutorConfirmationError
    from test_voice_runtime_binding import capture, interpret
    db, settings, sid, artifact, mid, _ = captured_question
    gateway = Gateway(settings)
    replacement = FORMULA_QUESTION.replace('37', '46').replace('\n', '')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        arrange(client, captured_question)
        if later_session:
            # Controlled day boundary, not evidence of the whole daily journey.
            focus = MistakeDialogueService(db).state(sid)
            with db.connect() as c:
                c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
                c.commit()
            sid = post(client, '/v1/sessions', {'child_id': CHILD, 'client_action_id': 'later-repair-session'}, expected_status=201)['session_id']
            with db.connect() as c:
                c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
                MistakeDialogueService._write_focus(c, {**focus, 'session_id': sid})
                c.commit()
        turn(client, sid, '题目应该是' + replacement, 'voice-repair-offer')
        text = '没错，这次读的是对的，就讲这道吧'
        voice_id = capture(client, settings, monkeypatch, sid, text, key='repair-real-binding', child=CHILD)
        gateway.configured = False
        accepted = interpret(client, sid, text, 'repair-real-confirm', voice_id)
        assert accepted['action']['voice_input_id'] == voice_id
        assert accepted['action']['source'] == 'mistake_question_voice_confirmation'
        gateway.configured = True
        result = post(client, f"/v1/agent-turns/{accepted['agent_turn_id']}/execute", {})
        assert result['execution'].get('tutor_turn'), (result['execution'].get('teaching_error'), len(gateway.posts))
        assert result['execution']['tutor_turn']['question'] == replacement
        with db.connect() as c:
            revision = current(c, mid, CHILD)
            assert revision['revision_session_id'] == sid
            assert revision['artifact_id'] == artifact['artifact_id']
            with pytest.raises(LocalSupplyError, match='SUPPLY_PAPER_CORRECTION_REVIEW_REQUIRED'):
                validated_variant_seed(c, CHILD, mid)
            assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
            c.commit()
        other = post(client, '/v1/sessions', {'child_id': 'child-junior-3', 'client_action_id': 'other-repair-session'}, expected_status=201)['session_id']
        with pytest.raises(OcrTutorConfirmationError):
            OcrTutorConfirmationService(db).confirm(other, artifact['artifact_id'], replacement,
                'manual_correction', 'wrong-child-revision', expected_formula_binding={k: revision[k] for k in ('review_id', 'version')})
    privacy = PrivacyLifecycleService(db, settings.data_dir)
    exported = privacy.export_child(CHILD)
    with open(exported['path'], encoding='utf-8') as stream:
        payload = json.load(stream)
    assert len(payload['mistake_source_revisions']) == 1
    assert privacy.deletion_preview(CHILD)['mistake_source_revisions'] == 1
    assert privacy.delete_child_scope(CHILD, 'learning', '确认删除', 'delete-source-revisions')['status'] == 'completed'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_failed_tutor_continues_corrected_case_after_restart(captured_question):
    db, settings, sid, _, mid, _ = captured_question
    gateway = Gateway(settings)
    replacement = FORMULA_QUESTION.replace('37', '47').replace('\n', '')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        arrange(client, captured_question)
        turn(client, sid, '题目应该是' + replacement, 'failed-tutor-offer')
        result = turn(client, sid, '没错，讲吧', 'failed-tutor-confirm')
        assert result['execution'].get('teaching_error'), result
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        result = turn(client, sid, '重试', 'failed-tutor-resume', gateway=gateway)
        assert result['execution']['tutor_turn']['question'] == replacement
        assert result['execution']['result']['mistake_id'] == mid
        assert len(gateway.posts) == 1
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 1
            assert c.execute('SELECT prompt_summary FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == replacement


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_next_day_reconfirms_existing_revision_without_rewriting_case(captured_question):
    from home_study_agent.mistake_dialogue import MistakeDialogueService
    db, settings, sid, _, mid, _ = captured_question
    gateway = Gateway(settings)
    replacement = FORMULA_QUESTION.replace('37', '48').replace('\n', '')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        arrange(client, captured_question)
        MistakeDialogueService(db).open_review(sid, 'day-one-open', mistake_id=mid)
        first = post(client, f'/v1/sessions/{sid}/agent-turn', {
            'transcript': '题目应该是' + replacement, 'client_action_id': 'day-one-offer', 'cloud_text_allowed': False})
        assert first['action']['type'] == 'repair_mistake_question', (first['action'], first['response_text'])
        from home_study_agent.mistake_source_revision import target
        with db.connect() as c:
            record = c.execute('SELECT context_manifest_json,arguments_json FROM agent_plan JOIN agent_step USING(plan_id) WHERE agent_plan.agent_turn_id=?',
                               (first['agent_turn_id'],)).fetchone()
            expected = json.loads(record[0]).get('mistake_repair_target')
            assert expected == target(c, MistakeDialogueService(db).state(sid)), (expected, target(c, MistakeDialogueService(db).state(sid)))
            assert json.loads(record[1]) == {'question_quote': replacement}, json.loads(record[1])
        post(client, f"/v1/agent-turns/{first['agent_turn_id']}/execute", {})
        turn(client, sid, '没错，讲吧', 'day-one-confirm', gateway=gateway)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
            c.commit()
        next_sid = post(client, '/v1/sessions', {'child_id': CHILD, 'client_action_id': 'next-day-existing'}, expected_status=201)['session_id']
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (next_sid,))
            c.commit()
        offered = MistakeDialogueService(db).open_review(next_sid, 'reopen-existing', mistake_id=mid)
        assert replacement in offered['response_text']
        assert offered['state']['question_confirmation']['question'] == replacement
        accepted = turn(client, next_sid, '没错，讲吧', 'day-two-confirm', gateway=gateway)
        assert accepted['execution'].get('tutor_turn'), accepted['execution'].get('teaching_error')
        assert accepted['execution']['tutor_turn']['question'] == replacement
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM tutor_ocr_confirmation').fetchone()[0] == 2


@pytest.mark.parametrize('captured_question', [True], indirect=True)
def test_revision_keeps_only_original_case_history_and_single_capture_deletion_erases_it(captured_question):
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, settings, sid, artifact, mid, _ = captured_question
    gateway = Gateway(settings)
    replacement = FORMULA_QUESTION.replace('37', '49').replace('\n', '')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        arrange(client, captured_question)
        turn(client, sid, '题目应该是' + replacement, 'minimal-source-offer')
        turn(client, sid, '没错，讲吧', 'minimal-source-confirm')
    with db.connect() as c:
        raw = c.execute('SELECT before_json FROM mistake_source_revision WHERE mistake_id=?', (mid,)).fetchone()[0]
        assert OTHER not in raw
        saved = json.loads(raw)
        assert saved['case']['prompt_summary'] == FORMULA_QUESTION
        assert saved['case']['submitted_answer'] == ANSWER
        assert saved['source_version']
        assert 'artifact' not in saved and 'reviews' not in saved and 'candidate' not in saved
    privacy = PrivacyLifecycleService(db, settings.data_dir)
    result = privacy.delete_capture_artifact(artifact['artifact_id'], '确认删除', 'delete-only-source')
    assert result['status'] == 'completed'
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('failure', ['confirmation', 'receipt'])
def test_confirmation_failure_rolls_back_revision_then_same_turn_recovers(captured_question, monkeypatch, failure):
    from home_study_agent.mistake_dialogue import MistakeDialogueService
    from home_study_agent.tutor_ocr_confirmation import OcrTutorConfirmationService
    db, settings, sid, _, mid, _ = captured_question
    gateway = Gateway(settings)
    replacement = FORMULA_QUESTION.replace('37', '43').replace('\n', '')
    with TestClient(create_app(settings, model_gateway=gateway), raise_server_exceptions=False) as client:
        arrange(client, captured_question)
        turn(client, sid, '题目应该是' + replacement, 'atomic-offer')
        accepted = turn(client, sid, '没错，讲吧', 'atomic-confirm', execute=False)
        url = f"/v1/agent-turns/{accepted['agent_turn_id']}/execute"
        cls, name = (OcrTutorConfirmationService, 'confirm') if failure == 'confirmation' else (MistakeDialogueService, '_save')
        original = getattr(cls, name)
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('controlled transaction failure')
        with monkeypatch.context() as m:
            m.setattr(cls, name, fail)
            assert client.post(url).status_code == 500
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM tutor_ocr_confirmation').fetchone()[0] == 0
            assert c.execute('SELECT prompt_summary FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == FORMULA_QUESTION
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        gateway.configured = True
        result = post(client, url, {})
        assert result['execution']['tutor_turn']['question'] == replacement
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM mistake_status_event WHERE reason='child_confirmed_question_revision'").fetchone()[0] == 1


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('change', ['review', 'excluded', 'malformed'])
def test_changed_source_before_confirmation_does_not_mutate_or_teach(captured_question, change):
    db, settings, sid, artifact, mid, _ = captured_question
    gateway = Gateway(settings)
    replacement = FORMULA_QUESTION.replace('37', '44').replace('\n', '')
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers, review, _ = arrange(client, captured_question)
        turn(client, sid, '题目应该是' + replacement, 'changed-offer')
        accepted = turn(client, sid, '没错，讲吧', 'changed-confirm', execute=False)
        if change == 'review':
            post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
                'child_id': CHILD, 'client_action_id': 'undo-before-confirm'}, headers=headers)
        else:
            with db.connect() as c:
                if change == 'excluded':
                    c.execute("UPDATE mistake_case SET status='excluded' WHERE mistake_id=?", (mid,))
                else:
                    c.execute("UPDATE capture_artifact SET details_json='[]' WHERE artifact_id=?", (artifact['artifact_id'],))
                c.commit()
        gateway.configured = True
        result = client.post(f"/v1/agent-turns/{accepted['agent_turn_id']}/execute", json={})
        assert result.status_code in {200, 409, 422}, result.text
        assert not result.json().get('execution', {}).get('tutor_turn')
        assert gateway.posts == []
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
            assert c.execute('SELECT prompt_summary FROM mistake_case WHERE mistake_id=?', (mid,)).fetchone()[0] == FORMULA_QUESTION


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('change', ['unchanged', 'different_source', 'invented_quote'])
def test_model_plans_natural_correction_but_local_source_owns_execution(captured_question, change):
    from test_cloud_first_agent import Planner
    db, settings, sid, _, mid, _ = captured_question
    replacement = '1. 计算 (45+13)×4，只回答一个整数。'
    utterance = '你刚才看错了，我给你重新念完整的：' + replacement
    planner = Planner('mistake.repair_question', {'question_quote': replacement if change != 'invented_quote' else '模型编造的题'}, utterance)
    with TestClient(create_app(settings, model_gateway=planner)) as client:
        arrange(client, captured_question)
        planned = post(client, f'/v1/sessions/{sid}/agent-turn', {
            'transcript': utterance, 'client_action_id': 'natural-repair', 'cloud_text_allowed': True})
        assert planned['runtime_capability'] == 'mistake.repair_question', planned
        if change == 'different_source':
            with db.connect() as c:
                c.execute("UPDATE mistake_case SET submitted_answer='new answer' WHERE mistake_id=?", (mid,))
                c.commit()
        result = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute", json={})
        if change == 'unchanged':
            assert result.status_code == 200, result.text
            assert result.json()['execution']['result']['state']['question_confirmation']['question'] == replacement
            assert result.json()['execution']['requires_child_reply'] is True
        else:
            assert result.status_code in {409, 422}, result.text
        assert len(planner.requests) == 1
        wire = json.dumps(planner.requests[0].messages, ensure_ascii=False)
        assert mid not in wire and ANSWER not in wire and 'mistake_repair_target' not in wire
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_source_revision').fetchone()[0] == 0
