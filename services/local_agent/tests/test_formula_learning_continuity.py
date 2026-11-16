"""Corrected synthetic paper through tutoring, independent practice and retention."""
import json
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.learning_feedback import build_learning_feedback
from home_study_agent.child_model import ChildModelService
from test_formula_voice_confirmation import arrange, turn
from test_paper_variant_line_projection_http import captured_question, gateway_for, post, FORMULA_QUESTION
from test_paper_variant_authorization_http import CHILD, body, url
from test_writing_question_authorization_http import parent
from test_mistake_voice_journey import focus


class Gateway:
    configured = False

    def __init__(self, settings):
        self.variant_posts, self.tutor_posts = [], []
        self.variants = gateway_for(settings, self.variant_posts, question=FORMULA_QUESTION.replace('37', '38'))
        async def transport(request):
            self.tutor_posts.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': '先求括号里的和，再乘四。现在用新题自己试一试。'}}]})
        self.tutor = ModelGateway(settings, transport=httpx.MockTransport(transport))

    async def complete(self, request):
        assert request.request_type != 'agent_plan', 'This test does not certify cloud NLU.'
        return await (self.variants if request.request_type == 'mistake_variant_generation' else self.tutor).complete(request)


@pytest.mark.parametrize('captured_question', [True], indirect=True)
@pytest.mark.parametrize('withdrawal', ['before_answer', 'after_answer'])
def test_corrected_formula_teaching_independence_retention_and_source_withdrawal(captured_question, monkeypatch, withdrawal):
    db, settings, sid, artifact, mid, _ = captured_question
    gateway = Gateway(settings)
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers, review, corrected = arrange(client, captured_question)
        # Initialize the real diagnosis round before the teaching continuation.
        # The readback unit fixture alone starts with a bare remediation focus.
        turn(client, sid, '继续错题', 'continuity-initialize', gateway=gateway)
        for index in range(2):
            current = focus(client, sid)
            if current['phase'] != 'diagnosis_probe':
                break
            probe = current['state']['next_activity']['item']
            with db.connect() as c:
                rubric = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                    (probe['diagnostic_item_id'],)).fetchone()[0])
            turn(client, sid, rubric['exact_answers'][0], 'continuity-probe:' + str(index), gateway=gateway)
        assert focus(client, sid)['phase'] == 'remediation'
        turn(client, sid, '我还是不会，能给我讲一下吗', 'continuity-offer', gateway=gateway)
        taught = turn(client, sid, '没错，讲吧', 'continuity-confirm', gateway=gateway)
        assert taught['execution']['tutor_turn']['question'] == corrected
        assert len(gateway.tutor_posts) == 1
        # A lesson alone must not create evidence of independent success.
        with db.connect() as c:
            assert c.execute('SELECT count(*) FROM mistake_attempt WHERE mistake_id=?', (mid,)).fetchone()[0] == 0
        view = client.get(url(mid), headers=headers).json()
        post(client, url(mid) + '/grant', body(view), headers=headers)
        job = post(client, '/v1/parent/variant-jobs/process-next', {}, headers=headers)['job']
        assert job['status'] == 'completed', job
        candidates = client.get(f'/v1/parent/children/{CHILD}/variant-candidates', headers=headers).json()['items']
        approved = []
        for item in candidates:
            result = post(client, f"/v1/parent/variant-candidates/{item['candidate_id']}/review", {
                'decision': 'approve', 'note': 'Synthetic arithmetic solved independently by test author.',
                'client_action_id': 'continuity-approve:' + item['candidate_id']}, headers=headers)
            approved.append(result['diagnostic_item_id'])
        assert len(approved) == 4
        with db.connect() as c:
            # Force this journey to use reviewed variants, not unrelated seed exercises.
            c.execute("UPDATE diagnostic_item SET status='inactive' WHERE knowledge_node_id='node-p4-math-arithmetic' AND diagnostic_item_id NOT IN (%s)" % ','.join('?' * len(approved)), approved)
            c.execute('UPDATE family_rule SET latest_end_time=NULL WHERE child_id=?', (CHILD,))
            c.commit()
        turn(client, sid, '明白了', 'continuity-understood', gateway=gateway)
        for index in range(10):
            current = focus(client, sid)
            if current['phase'] == 'scheduled':
                break
            assert current['phase'] in {'attempt', 'diagnosis_probe'}, current
            item = current['state']['next_activity']['item']
            assert item['diagnostic_item_id'] in approved
            with db.connect() as c:
                rubric = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                    (item['diagnostic_item_id'],)).fetchone()[0])
            turn(client, sid, '答案是' + rubric['exact_answers'][0], 'continuity-answer:' + str(index), gateway=gateway)
        waiting = focus(client, sid)
        assert waiting['phase'] == 'scheduled', waiting
        activity = waiting['state']['next_activity']
        with db.connect() as c:
            attempts = [dict(r) for r in c.execute('SELECT stage,correctness,independent,hint_level,submitted_answer FROM mistake_attempt WHERE mistake_id=?', (mid,))]
        assert activity['kind'] == 'scheduled' and activity['stage'] == 'delayed_48h', (activity, attempts)
        due = activity['due_at']
        with db.connect() as c:
            feedback = build_learning_feedback(c, CHILD)['nodes']
            assert feedback and feedback[0]['status'] == 'independent_observed', feedback
            assert feedback[0]['retention_success_count'] == 0
            assert build_learning_feedback(c, 'child-junior-3')['nodes'] == []
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert focus(client, sid)['state']['next_activity']['due_at'] == due
        turn(client, sid, '继续错题', 'continuity-before-due', gateway=gateway)
        assert focus(client, sid)['phase'] == 'scheduled'
        post(client, f'/v1/sessions/{sid}/transition', {'expected_state': 'study', 'target_state': 'reflection',
            'reason': '等待延迟复测', 'client_action_id': 'continuity-end'})
        post(client, f'/v1/sessions/{sid}/reflection/complete', {'skipped': True, 'client_action_id': 'continuity-close'})
    future = datetime.fromisoformat(due) + timedelta(minutes=1)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return future.astimezone(tz) if tz else future.replace(tzinfo=None)
    from home_study_agent import mistake_book, repository, learning_state, learning_obligations, child_model
    for module in (mistake_book, repository, learning_state, learning_obligations, child_model):
        monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(mistake_book, 'utc_now', lambda: future.isoformat())
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers = parent(client)
        sid2 = post(client, '/v1/sessions', {'child_id': CHILD, 'client_action_id': 'continuity-new-day'}, expected_status=201)['session_id']
        post(client, f'/v1/sessions/{sid2}/transition', {'expected_state': 'check_in', 'target_state': 'classroom_recall',
            'reason': '开始学习', 'client_action_id': 'continuity-recall'})
        post(client, f'/v1/sessions/{sid2}/recalls/none', {'client_action_id': 'continuity-no-recall'})
        post(client, f'/v1/sessions/{sid2}/transition', {'expected_state': 'classroom_recall', 'target_state': 'homework_capture',
            'reason': '核对作业', 'client_action_id': 'continuity-homework'})
        post(client, f'/v1/sessions/{sid2}/homework/none', {'client_action_id': 'continuity-no-homework'})
        workspace = post(client, f'/v1/sessions/{sid2}/plan/generate', {'client_action_id': 'continuity-plan'})
        item = next(p for p in workspace['plan_items'] if p['source_type'] == 'mistake_review' and p['source_ref'] == mid)
        assert item['status'] == 'pending'
        post(client, f'/v1/sessions/{sid2}/transition', {'expected_state': 'plan_review', 'target_state': 'study',
            'reason': '确认计划', 'client_action_id': 'continuity-study'})
        post(client, f"/v1/plan-items/{item['plan_item_id']}/start", {'client_action_id': 'continuity-start'})
        gateway.configured = False
        opened = post(client, f'/v1/sessions/{sid2}/agent-turn', {'transcript': '打开错题本',
            'client_action_id': 'continuity-open', 'cloud_text_allowed': False})
        assert opened['action']['type'] == 'open_mistake_book', opened
        post(client, f"/v1/agent-turns/{opened['agent_turn_id']}/execute", {})
        current = focus(client, sid2)
        assert current['mistake_id'] == mid
        assert current['state']['next_activity']['stage'] == 'delayed_48h'
        exercise = current['state']['next_activity']['item']
        with db.connect() as c:
            rubric = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                (exercise['diagnostic_item_id'],)).fetchone()[0])
            before = c.execute('SELECT count(*) FROM mistake_attempt WHERE mistake_id=?', (mid,)).fetchone()[0]
        admitted = turn(client, sid2, '答案是' + rubric['exact_answers'][0], 'continuity-retention', execute=False, gateway=gateway)
        if withdrawal == 'after_answer':
            result = post(client, f"/v1/agent-turns/{admitted['agent_turn_id']}/execute", {})
            assert result['execution']['result']['plan_completion']['plan_item_id'] == item['plan_item_id']
            with db.connect() as c:
                assert build_learning_feedback(c, CHILD, now=future)['nodes'][0]['retention_success_count'] >= 1
                assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()[0] == 'completed'
                assert c.execute("SELECT le.session_id FROM learning_evidence le JOIN mistake_attempt ma USING(evidence_id) WHERE ma.mistake_id=? AND ma.stage='delayed_48h'", (mid,)).fetchone()[0] == sid2
            gateway.configured = False
            post(client, f'/v1/sessions/{sid2}/transition', {'expected_state': 'study', 'target_state': 'reflection',
                'reason': '今天复测结束', 'client_action_id': 'continuity-retention-end'})
            post(client, f'/v1/sessions/{sid2}/reflection/complete', {'learned_text': '先确认顺序再计算',
                'skipped': False, 'client_action_id': 'continuity-retention-report'})
            report = client.get(f'/v1/sessions/{sid2}/report', headers=headers).json()
            assert any(r.get('retention_status') == 'retention_observed'
                       for r in report['evidence_detail']['knowledge_evidence'])
            # Unfinished planned work keeps priority over optional celebration.
            assert report['parent_coaching']['focus'] == 'unfinished_task'
    # Reopen before withdrawal: neither decision depends on in-memory state.
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers = parent(client)
        if withdrawal == 'after_answer':
            model = ChildModelService(db).snapshot(CHILD)
            assert model['payload']['learning']['reliable_feedback']['nodes'][0]['retention_success_count'] >= 1
            restored_report = client.get(f'/v1/sessions/{sid2}/report', headers=headers).json()
            assert restored_report['parent_action']['snapshot_id'] == report['parent_action']['snapshot_id']
        # Source withdrawal must remove stale evidence and prevent use of its exercises.
        post(client, f"/v1/ocr-review-items/{review['review_id']}/undo", {
            'child_id': CHILD, 'client_action_id': 'continuity-source-undo'}, headers=headers)
        if withdrawal == 'before_answer':
            result = client.post(f"/v1/agent-turns/{admitted['agent_turn_id']}/execute", json={})
            assert result.status_code in {200, 409}, result.text
        with db.connect() as c:
            assert build_learning_feedback(c, CHILD, now=future)['nodes'] == []
            assert c.execute('SELECT count(*) FROM mistake_attempt WHERE mistake_id=?', (mid,)).fetchone()[0] == before + (withdrawal == 'after_answer')
            if withdrawal == 'before_answer':
                assert c.execute('SELECT status FROM plan_item WHERE plan_item_id=?', (item['plan_item_id'],)).fetchone()[0] != 'completed'
        next_activity = client.app.state.mistake_book.next_activity(mid, CHILD)
        assert (next_activity.get('item') or {}).get('diagnostic_item_id') not in approved
        assert ChildModelService(db).snapshot(CHILD)['payload']['learning']['reliable_feedback']['nodes'] == []
        if withdrawal == 'after_answer':
            current_report = client.get(f'/v1/sessions/{sid2}/report', headers=headers).json()
            assert not any(r.get('retention_status') == 'retention_observed'
                           for r in current_report['evidence_detail']['knowledge_evidence'])
