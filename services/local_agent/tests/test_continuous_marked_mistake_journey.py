"""Synthetic page + controlled OCR/Tutor; real HTTP, candidate and mistake stores."""
import base64
import json
from datetime import datetime, timedelta

import cv2
import numpy as np
import pytest

from home_study_agent.database import Database
from home_study_agent.local_ocr.service import RapidOcrEngine
from home_study_agent.model_gateway import ModelResult
from test_app import client_for, advance_to_study
from test_mistake_voice_journey import ok, say, focus


QUESTION = '2. 选出划线字母读音不同的一项 A. grape B. make C. like'


class PhonicsGateway:
    configured = True

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        if request.request_type == 'voice_interaction':
            from test_content_free_voice_control_http import ControlGateway
            self.requests.append(request)
            quote = json.loads(request.messages[-1]['content'])['transcript']
            is_start = quote == '开始今天的错题复习'
            return await ControlGateway(quote, 'task.start' if is_start else None,
                                        role='control' if is_start else 'answer').complete(request)
        if request.request_type == 'agent_plan':
            from test_cloud_first_agent import Planner
            utterance = json.loads(request.messages[-1]['content'])['child_utterance']
            plan = (Planner('task.start', {}, utterance) if utterance == '开始今天的错题复习' else
                    Planner('mistake.open', {}, utterance, objective_type='learning') if utterance == '复习上次的错题'
                    else Planner('mistake.dialogue', {'utterance': utterance}, utterance, objective_type='learning'))
            return await plan.complete(request)
        self.requests.append(request)
        if request.request_type == 'mistake_variant_generation':
            context = json.loads(request.messages[-1]['content'])
            assert context['original_prompt'] == QUESTION
            assert context['original_answer_verified'] is False
            assert set(context) == {'grade_band', 'subject', 'knowledge_title', 'knowledge_description',
                'original_prompt', 'original_answer_type', 'original_rubric', 'original_explanation',
                'required_stages', 'original_answer_verified'}
            examples = [
                ('只比较单词中的元音：A. bike B. five C. fish，选出不同的一项，只回答选项字母。', 'c', 'bike和five含双元音，fish含短元音。'),
                ('A. bus B. cup C. cute，哪个词的字母u读音不同？只回答选项字母。', 'c', 'bus和cup中的u读短元音，cute不同。'),
                ('给词尾加e后，cap变为cape。元音从短音变成双元音了吗？只回答yes或no。', 'yes', 'cap的元音为短音，cape的元音为双元音。'),
                ('有人认为hope与hot的字母o发音相同。这句话对吗？只回答yes或no。', 'no', 'hope含双元音，hot含短元音，二者不同。'),
            ]
            return ModelResult(provider='controlled-only', model='phonics-variant-fixture', latency_ms=1,
                content=json.dumps({'candidates': [
                    {'stage': stage, 'prompt': examples[i][0], 'answer_type': 'text',
                     'expected_answer': {'exact_answers': [examples[i][1]], 'answer_format': 'text'},
                     'explanation': examples[i][2], 'difficulty': 1}
                    for i, stage in enumerate(context['required_stages'])]}, ensure_ascii=False))
        assert request.request_type.startswith('tutor_hint_level_')
        assert all(word in json.dumps(request.messages, ensure_ascii=False) for word in ('grape', 'make', 'like'))
        return ModelResult(provider='controlled-only', model='phonics-fixture', latency_ms=1,
            content=json.dumps({'blackboard': {'title': '先比较读音', 'segments': [
                {'narration': '先读grape和make，再读like。不要只比较字母长相。',
                 'blocks': [{'kind': 'text', 'text': 'grape / make / like'}]},
                {'narration': '你听到哪个元音不同？请说说你的比较方法。',
                 'blocks': [{'kind': 'question', 'text': '先读音，再比较。'}]},
            ]}}, ensure_ascii=False))


@pytest.mark.parametrize('child', ['child-primary-4', 'child-junior-3'])
@pytest.mark.parametrize('review_correct', [True, False])
def test_continuous_marked_page_to_original_answer_diagnosis_tutor_and_restart(
        tmp_path, monkeypatch, child, review_correct, before_verified_answer=None):
    def recognize(_engine, image, *args, **kwargs):
        upright = float(image[35:115, 35:115].mean()) < 210
        confidence = .96 if upright else .34
        return [
            {'text': '1. Choose the right answer', 'confidence': confidence,
             'box': [[80, 80], [500, 80], [500, 120], [80, 120]], 'kind': 'printed'},
            {'text': QUESTION, 'confidence': confidence,
             'box': [[80, 210], [560, 210], [560, 260], [80, 260]], 'kind': 'printed'},
        ]
    monkeypatch.setattr(RapidOcrEngine, 'recognize', recognize)
    monkeypatch.setattr(RapidOcrEngine, 'recognize_page_orientation', recognize)
    page = np.full((420, 640, 3), 245, dtype=np.uint8)
    cv2.rectangle(page, (55, 55), (95, 95), (0, 0, 0), -1)
    cv2.putText(page, '1. Choose the right answer', (80, 110), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)
    cv2.putText(page, '2. A.grape B.make C.like', (80, 245), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)
    cv2.line(page, (500, 195), (575, 275), (0, 0, 230), 8)
    cv2.line(page, (575, 195), (500, 275), (0, 0, 230), 8)
    encoded = cv2.imencode('.png', cv2.rotate(page, cv2.ROTATE_180))[1].tobytes()
    gateway = PhonicsGateway()
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid, _ = advance_to_study(client, child_id=child)
        token = ok(client.post('/v1/parent/setup', json={'pin': '8264'}))['parent_token']
        ok(client.put('/v1/parent/privacy', headers={'X-HSC-Parent': token}, json={'cloud_model_text_allowed': True}))
        ok(client.post(f'/v1/sessions/{sid}/document-monitor/start', json={'client_action_id': 'start-marked'}))
        artifact = ok(client.post(f'/v1/sessions/{sid}/document-monitor/ocr', json={
            'image_base64': base64.b64encode(encoded).decode(), 'client_action_id': 'marked-ocr'}))
        pipeline = artifact['details']['local_pipeline']
        assert pipeline['quality']['transforms']['orientation_degrees'] == 180
        assert pipeline['wrong_question_candidates'][0]['text'] == QUESTION
        assert pipeline['proven_wrong_questions'] == []
        revision = ok(client.post(f'/v1/sessions/{sid}/document-monitor/revisions', json={
            'artifact_id': artifact['artifact_id'], 'content_signature': 'synthetic-marked-page', 'client_action_id': 'marked-revision'}))
        assert revision['marked_question_count'] == 1
        db = Database(client.app.state.settings.database_path)
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 0
        opened = ok(client.post(f'/v1/sessions/{sid}/mistake-dialogue', json={
            'artifact_id': artifact['artifact_id'], 'client_action_id': 'marked-begin'}))
        assert opened['state']['question_text'] == QUESTION
        assert opened['state']['artifact_id'] == artifact['artifact_id']
        say(client, sid, '第二题', 'select-number')
        say(client, sid, '对', 'confirm-stem')
        say(client, sid, '我当时选B', 'original-answer')
        assert focus(client, sid)['state']['submitted_answer'] == 'B'
        assert not gateway.requests
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        assert focus(client, sid)['state']['artifact_id'] == artifact['artifact_id']
        saved = say(client, sid, '是的', 'confirm-answer')
        mistake = saved['execution']['result']['mistake_id']
        assert mistake
        taught = say(client, sid, '我是看拼写选的，不知道比较读音的方法', 'explain-cause')
        assert not taught['execution'].get('teaching_error'), taught['execution']
        tutor = taught['execution']['tutor_turn']
        board = tutor['output_envelope']['result']['blackboard']
        assert board['source'] == 'model_structured' and len(board['segments']) == 2
        assert QUESTION in tutor['question']
        assert '判别式' not in tutor['answer']
        assert len(gateway.requests) == 1
        say(client, sid, '明白了', 'ready-for-check')
        activity = focus(client, sid)['state']['next_activity']
        assert activity['kind'] == 'attempt' and activity['item']['prompt']
        case = client.app.state.mistake_book.get(mistake, child)
        assert case['mastery_gate']['satisfied'] == {}
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_case').fetchone()[0] == 1
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 0
            assert c.execute('SELECT count(*) FROM tutor_turn').fetchone()[0] == 1
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        restored = focus(client, sid)
        assert restored['mistake_id'] == mistake and restored['phase'] == 'attempt'
        assert restored['state']['next_activity'] == activity
        assert len(gateway.requests) == 1
        for index in range(3):
            current = focus(client, sid)
            assert current['state']['next_activity']['item'], {
                'node': current['state'].get('knowledge_node_id'), 'activity': current['state']['next_activity'],
                'first_prompt': activity['item']['prompt'], 'attempts': index}
            question = current['state']['next_activity']['item']['prompt']
            answer = ('bag /æ/ 不同，cake name /eɪ/' if 'cake' in question else
                      'sit /ɪ/ 不同，bike five /aɪ/' if 'bike' in question else
                      'hot /ɒ/ 不同，home nose /əʊ/')
            turn = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
                'transcript': answer, 'client_action_id': f'independent-{index}'}))
            assert turn['action']['source'] == 'pending_answer_clarification'
            ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
            say(client, sid, '刚才那句就是我的答案', f'independent-confirm-{index}')
        final = focus(client, sid)
        assert final['phase'] == 'scheduled'
        assert final['state']['next_activity']['stage'] == 'near_transfer'
        assert final['state']['next_activity']['kind'] in {'blocked', 'preparing_variant', 'awaiting_parent_review'}
        assert client.app.state.mistake_book.get(mistake, child)['mastery_gate']['satisfied'] == {}
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == 3
            assert c.execute('SELECT count(*) FROM mistake_case WHERE child_id != ?', (child,)).fetchone()[0] == 0
        # Only the synthetic paper is purpose-authorized; no source/grade bypass.
        from test_writing_question_authorization_http import parent
        headers = parent(client)
        path = f'/v1/parent/children/{child}/paper-variant-authorizations/{mistake}'
        view = ok(client.get(path, headers=headers))
        with db.connect() as c:
            details = json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?',
                                          (artifact['artifact_id'],)).fetchone()[0])
        assert view['question_version'] and view['source_version'], (view['status'], view['reason'])
        for mutation in ('prepared', 'unknown_field', 'permission', 'frame', 'private_root'):
            changed = json.loads(json.dumps(details))
            if mutation == 'prepared':
                changed['visual_capture']['status'] = 'prepared'
            elif mutation == 'unknown_field':
                changed['visual_capture']['family_context'] = 'Synthetic private context'
            elif mutation == 'permission':
                changed['visual_capture']['cloud_transmission_allowed'] = True
            elif mutation == 'frame':
                changed['monitor_frame_sha256'] = '0' * 64
            else:
                changed['local_only'] = True
            with db.connect() as c:
                c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',
                          (json.dumps(changed), artifact['artifact_id']))
                c.commit()
            rejected = ok(client.get(path, headers=headers))
            assert rejected['status'] == 'blocked' and rejected['question_version'] is None, mutation
        with db.connect() as c:
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',
                      (json.dumps(details), artifact['artifact_id']))
            c.commit()
        # The restored source has the same parsed identity, no mutation grants use.
        view = ok(client.get(path, headers=headers))
        granted = ok(client.post(path + '/grant', headers=headers, json={
            **{name: view[name] for name in ('question_version', 'source_version')},
            'license_basis': {'license_id': 'original_author', 'reference': 'Synthetic phonics test fixture',
                'evidence': 'Test author permits this original synthetic phonics question for isolated variant generation.'},
            'expected_revision': view['revision'], 'client_action_id': 'phonics-purpose'}))
        assert granted['generation_job']['status'] == 'pending'
        job = ok(client.post('/v1/parent/variant-jobs/process-next', headers=headers))['job']
        assert job['status'] == 'completed', job
        candidates = ok(client.get(f'/v1/parent/children/{child}/variant-candidates', headers=headers))['items']
        assert candidates
        for item in candidates:
            approved = ok(client.post(f'/v1/parent/variant-candidates/{item["candidate_id"]}/review', headers=headers,
                json={'decision': 'approve', 'note': 'Synthetic phonics answer checked by test author.',
                      'client_action_id': f'phonics-review:{item["candidate_id"]}'}))
            from home_study_agent.local_supply import LocalSupplyError, validate_item
            other = 'child-junior-3' if child == 'child-primary-4' else 'child-primary-4'
            with db.connect() as c, pytest.raises(LocalSupplyError):
                validate_item(c, other, approved['diagnostic_item_id'])
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        resumed = say(client, sid, '继续复习刚才那道错题', 'resume-verified')
        current = focus(client, sid)
        assert current['phase'] == 'attempt', resumed
        fresh = current['state']['next_activity']['item']
        assert fresh['diagnostic_item_id'].startswith('variant-')
        assert len([r for r in gateway.requests if r.request_type == 'mistake_variant_generation']) == 1
        answer = ('c' if 'A. bike' in fresh['prompt'] or 'A. bus' in fresh['prompt']
                  else 'yes' if 'cap变为cape' in fresh['prompt'] else 'no')
        if before_verified_answer:
            before_verified_answer(client, sid, db)
        say(client, sid, '答案是' + answer, 'verified-answer')
        case = client.app.state.mistake_book.get(mistake, child)
        assert case['mastery_gate']['satisfied'].get('near_transfer') == 1, case['mastery_gate']
        assert case['status'] != 'mastered'
        for index in range(1, len(candidates)):
            current = focus(client, sid)
            fresh = current['state']['next_activity']['item']
            assert current['phase'] == 'attempt' and fresh['diagnostic_item_id'].startswith('variant-'), current
            answer = ('c' if 'A. bike' in fresh['prompt'] or 'A. bus' in fresh['prompt']
                      else 'yes' if 'cap变为cape' in fresh['prompt'] else 'no')
            say(client, sid, '答案是' + answer, f'verified-answer-{index}')
        waiting = focus(client, sid)
        assert waiting['phase'] == 'scheduled'
        assert waiting['state']['next_activity']['kind'] == 'scheduled'
        due = waiting['state']['next_activity']['due_at']
        assert waiting['state']['next_activity']['stage'] == 'delayed_48h'
        with db.connect() as c:
            before_restart = c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0]
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        assert focus(client, sid)['state']['next_activity']['due_at'] == due
        say(client, sid, '继续复习刚才那道错题', 'before-delay')
        assert focus(client, sid)['phase'] == 'scheduled'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == before_restart
        from home_study_agent import mistake_book
        future = datetime.fromisoformat(due) + timedelta(minutes=1)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return future.astimezone(tz) if tz else future.replace(tzinfo=None)
        # Settle actual pending school work rather than forging a completed day.
        workspace = ok(client.get(f'/v1/sessions/{sid}'))
        carried = []
        for item in workspace['plan_items']:
            if item['status'] in {'pending', 'active', 'paused'} and item['source_type'] == 'school_homework':
                ok(client.post(f'/v1/plan-items/{item["plan_item_id"]}/reschedule', json={
                    'reason': '今晚结束，下一次先确认延期作业。', 'client_action_id': 'defer:' + item['plan_item_id']}))
                carried.append(item['plan_item_id'])
        assert carried
        ok(client.post(f'/v1/sessions/{sid}/transition', json={'expected_state': 'study', 'target_state': 'reflection',
            'reason': '本次练习结束，等待延迟复测', 'client_action_id': 'end-study'}))
        ended = ok(client.post(f'/v1/sessions/{sid}/reflection/complete', json={
            'skipped': True, 'client_action_id': 'end-day-complete'}))
        assert ended['session']['status'] == 'completed'
    from home_study_agent import repository, learning_state, learning_obligations, child_model
    for module in (mistake_book, repository, learning_state, learning_obligations, child_model):
        monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(mistake_book, 'utc_now', lambda: future.isoformat())
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        activity = client.app.state.mistake_book.next_activity(mistake, child)
        assert activity['kind'] == 'attempt' and activity['stage'] == 'delayed_48h'
        assert activity['item']['diagnostic_item_id'].startswith('variant-'), activity
        assert client.app.state.mistake_book.get(mistake, child)['status'] != 'mastered'
        sid2 = ok(client.post('/v1/sessions', json={'child_id': child, 'client_action_id': 'retention-day'}))['session_id']
        ok(client.post(f'/v1/sessions/{sid2}/transition', json={'expected_state': 'check_in', 'target_state': 'classroom_recall',
            'reason': '开始新一天', 'client_action_id': 'retention-recall'}))
        ok(client.post(f'/v1/sessions/{sid2}/recalls/none', json={'client_action_id': 'retention-recall-empty'}))
        ok(client.post(f'/v1/sessions/{sid2}/transition', json={'expected_state': 'classroom_recall', 'target_state': 'homework_capture',
            'reason': '核对今天任务', 'client_action_id': 'retention-homework'}))
        for source in carried:
            ok(client.post(f'/v1/sessions/{sid2}/deferred-tasks/{source}/review', json={
                'decision': 'continue_today', 'reason': '今天继续安排', 'client_action_id': 'carry:' + source}))
        planned = ok(client.post(f'/v1/sessions/{sid2}/plan/generate', json={'client_action_id': 'retention-plan'}))
        review = next(item for item in planned['plan_items'] if item['source_type'] == 'mistake_review' and item['source_ref'] == mistake)
        ok(client.post(f'/v1/sessions/{sid2}/transition', json={'expected_state': 'plan_review', 'target_state': 'study',
            'reason': '确认计划', 'client_action_id': 'retention-study'}))
        from test_voice_runtime_binding import capture, interpret
        def voice_execute(text, key, reference=None, session_id=None):
            active_sid = session_id or sid2
            voice_id = capture(client, client.app.state.settings, monkeypatch, active_sid, text,
                               reference, key + '-capture', child=child)
            turn = interpret(client, active_sid, text, key, voice_id)
            return ok(client.post(f'/v1/agent-turns/{turn["agent_turn_id"]}/execute'))
        started = voice_execute('开始今天的错题复习', 'retention-start')
        assert started['execution']['capability'] == 'task.start', started
        assert started['execution']['result']['mistake_dialogue']['mistake_id'] == mistake
        retained = focus(client, sid2)
        assert retained['mistake_id'] == mistake
        assert retained['state']['next_activity']['stage'] == 'delayed_48h' and retained['phase'] == 'attempt'
        prompt = retained['state']['next_activity']['item']['prompt']
        answer = ('c' if 'A. bike' in prompt or 'A. bus' in prompt else 'yes' if 'cap变为cape' in prompt else 'no')
        completed_turn = voice_execute('答案是' + (answer if review_correct else 'a'), 'retention-answer',
                                       {'kind': 'mistake', 'binding': retained['answer_binding']})
        result = client.app.state.mistake_book.get(mistake, child)
        assert result['mastery_gate']['satisfied'].get('delayed_48h', 0) == int(review_correct), result['mastery_gate']
        # This source is diagnosed as method_gap, whose configured case gate
        # ends at 48h; it is not a claim of whole-subject or child mastery.
        assert result['status'] == ('mastered' if review_correct else 'fragile')
        settled = ok(client.get(f'/v1/sessions/{sid2}'))
        settled_review = next(item for item in settled['plan_items'] if item['plan_item_id'] == review['plan_item_id'])
        assert settled_review['status'] == ('completed' if review_correct else 'active'), settled_review
        assert (settled_review['active_segment_started_at'] is None) == review_correct
        assert any(item['source_type'] == 'school_homework' and item['status'] == 'pending'
                   for item in settled['plan_items'])
        if review_correct:
            assert completed_turn['execution']['result']['plan_completion']['plan_item_id'] == review['plan_item_id']
            assert '本次复习任务已收好' in completed_turn['turn']['response_text']
            assert '下一项' in completed_turn['turn']['response_text'] or '下一步' in completed_turn['turn']['response_text']
        else:
            assert not completed_turn['execution']['result'].get('plan_completion')
        ok(client.post(f'/v1/agent-turns/{completed_turn["turn"]["agent_turn_id"]}/execute'))
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'",
                             (review['plan_item_id'],)).fetchone()[0] == int(review_correct)
        retained_gate = result['mastery_gate']
        with db.connect() as c:
            retained_attempts = c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0]
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        assert client.app.state.mistake_book.get(mistake, child)['mastery_gate'] == retained_gate
        restored = ok(client.get(f'/v1/sessions/{sid2}'))
        assert any(item['plan_item_id'] == review['plan_item_id'] and item['source_ref'] == mistake
                   and item['status'] == ('completed' if review_correct else 'paused')
                   for item in restored['plan_items'])
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == retained_attempts
        upcoming = client.app.state.mistake_book.next_activity(mistake, child)
        if review_correct:
            assert upcoming['kind'] == 'mastered', upcoming
            next_check = future + timedelta(days=1)
        else:
            assert upcoming['kind'] == 'scheduled' and upcoming['stage'] == 'delayed_48h', upcoming
            next_check = datetime.fromisoformat(upcoming['due_at']) + timedelta(minutes=1)
            assert next_check > future + timedelta(hours=48)
        # Close the real day, preserving pending work through the public routes.
        carried_again = []
        for item in restored['plan_items']:
            if item['status'] in {'pending', 'active', 'paused'}:
                ok(client.post(f'/v1/plan-items/{item["plan_item_id"]}/reschedule', json={
                    'reason': '今天结束，保留下一次复习和作业责任。',
                    'client_action_id': 'end-retention:' + item['plan_item_id']}))
                if item['source_type'] == 'school_homework':
                    carried_again.append(item['plan_item_id'])
        ok(client.post(f'/v1/sessions/{sid2}/transition', json={'expected_state': 'study', 'target_state': 'reflection',
            'reason': '保留后续复测', 'client_action_id': 'retention-reflection'}))
        ended = ok(client.post(f'/v1/sessions/{sid2}/reflection/complete', json={
            'skipped': True, 'client_action_id': 'retention-day-end'}))
        assert ended['session']['status'] == 'completed'
        # The original paper mistake belongs to an earlier day; today's
        # actual formal retest still needs its current case/gate in the report.
        with db.connect() as c:
            assert c.execute('SELECT source_evidence_id FROM mistake_case WHERE mistake_id=?', (mistake,)).fetchone()[0] is None
        day_evidence = client.app.state.repository.report_evidence(sid2)
        day_case = next(item for item in day_evidence['mistakes'] if item['mistake_id'] == mistake)
        assert day_case['mastery_gate'] == retained_gate
        assert day_case['status'] == ('mastered' if review_correct else 'fragile')
        assert any(item['evidence_type'] == 'mistake_delayed_48h'
                   and item['score_status'] == 'verified_local' for item in day_evidence['knowledge_evidence'])
    future = next_check
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid3 = ok(client.post('/v1/sessions', json={'child_id': child, 'client_action_id': 'next-retention-day'}))['session_id']
        ok(client.post(f'/v1/sessions/{sid3}/transition', json={'expected_state': 'check_in', 'target_state': 'classroom_recall',
            'reason': '新一天', 'client_action_id': 'next-recall'}))
        ok(client.post(f'/v1/sessions/{sid3}/recalls/none', json={'client_action_id': 'next-recall-empty'}))
        ok(client.post(f'/v1/sessions/{sid3}/transition', json={'expected_state': 'classroom_recall', 'target_state': 'homework_capture',
            'reason': '确认原任务', 'client_action_id': 'next-homework'}))
        for source in carried_again:
            ok(client.post(f'/v1/sessions/{sid3}/deferred-tasks/{source}/review', json={
                'decision': 'continue_today', 'reason': '继续原作业', 'client_action_id': 'next-carry:' + source}))
        planned_again = ok(client.post(f'/v1/sessions/{sid3}/plan/generate', json={'client_action_id': 'next-retention-plan'}))
        originals = [item for item in planned_again['plan_items']
                     if item['source_type'] == 'mistake_review' and item['source_ref'] == mistake]
        assert len(originals) == (0 if review_correct else 1), planned_again['plan_items']
        if originals:
            assert originals[0]['status'] == 'pending'
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == retained_attempts
            assert not c.execute('SELECT 1 FROM mistake_attempt WHERE child_id != ?', (child,)).fetchone()
        if not review_correct:
            ok(client.post(f'/v1/sessions/{sid3}/transition', json={'expected_state': 'plan_review', 'target_state': 'study',
                'reason': '开始到期复测', 'client_action_id': 'next-retention-study'}))
            restarted = voice_execute('开始今天的错题复习', 'next-retention-start', session_id=sid3)
            assert restarted['execution']['result']['mistake_dialogue']['mistake_id'] == mistake
            retry = focus(client, sid3)
            assert retry['phase'] == 'attempt' and retry['state']['next_activity']['stage'] == 'delayed_48h'
            prompt = retry['state']['next_activity']['item']['prompt']
            answer = ('c' if 'A. bike' in prompt or 'A. bus' in prompt else 'yes' if 'cap变为cape' in prompt else 'no')
            finished = voice_execute('答案是' + answer, 'next-retention-answer',
                {'kind': 'mistake', 'binding': retry['answer_binding']}, session_id=sid3)
            assert finished['execution']['result']['plan_completion']['plan_item_id'] == originals[0]['plan_item_id']
            corrected_gate = client.app.state.mistake_book.get(mistake, child)['mastery_gate']
            assert corrected_gate['satisfied']['delayed_48h'] == 1
    if not review_correct:
        with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
            assert client.app.state.mistake_book.get(mistake, child)['mastery_gate'] == corrected_gate
            restored = ok(client.get(f'/v1/sessions/{sid3}'))
            assert next(item for item in restored['plan_items'] if item['plan_item_id'] == originals[0]['plan_item_id'])['status'] == 'completed'
            with db.connect() as c:
                assert c.execute('SELECT count(*) FROM mistake_attempt').fetchone()[0] == retained_attempts + 1
