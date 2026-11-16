"""Delivered but unanswered work is not a completed learning obligation."""
from datetime import datetime, timedelta
import json
from synthetic_oral_app_lifecycle import fresh_oral_case, parent_headers

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_state import LearningStateService
from test_oral_supply_cross_day_execute import test_voice_resume_delivers_new_question_with_old_goal_lineage as journey
from test_writing_stage_review_consumers import advance_clock
from test_voice_runtime_binding import capture, interpret


@pytest.mark.parametrize('subject', ['数学', '物理'])
def test_unanswered_delivery_survives_closure_and_third_plan(tmp_path, monkeypatch, subject, after_retention=None,before_deferral=None):
    def close_and_plan(case):
        client, db, sid, child = case['client'], case['db'], case['next_sid'], case['child']
        root_id = case['commitment']['commitment_id']
        def post(path, body):
            r = client.post(path, json=body)
            assert r.status_code in (200, 201), r.text
            return r.json()
        with db.connect() as c:
            root_before = dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?', (root_id,)).fetchone())
            prompt_before = dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (case['new_prompt']['prompt_id'],)).fetchone())
        if before_deferral:
            before_deferral(case)
        for control, quote in [('task.pause','先暂停这道题。'),('task.reschedule','这道题延期到下次。')]:
            case['command_state'].update(control=control,quote=quote)
            voice = capture(client,case['settings'],monkeypatch,sid,quote,case['new_prompt'],
                key='unanswered-'+control+'-voice',child=child)
            turn = interpret(client,sid,quote,'unanswered-'+control,voice)
            assert turn.get('runtime_capability')==control,turn
            executed = post('/v1/agent-turns/'+turn['agent_turn_id']+'/execute',{})
            assert executed['execution']['result']['plan_item_id']==case['new_item_id']
        for item in client.get(f'/v1/sessions/{sid}').json()['plan_items']:
            if item['status'] in ('pending', 'paused'):
                post(f"/v1/plan-items/{item['plan_item_id']}/reschedule", dict(reason='今天时间到了，下次再安排。',
                    client_action_id='unanswered-defer-' + item['plan_item_id']))
        post(f'/v1/sessions/{sid}/transition', dict(expected_state='study', target_state='reflection',
            reason='今天先到这里，题目还没回答。', client_action_id='unanswered-reflect'))
        post(f'/v1/sessions/{sid}/reflection/complete', dict(learned_text='', difficult_text='', client_action_id='unanswered-close'))
        summary = LearningStateService(db).close_session_learning(sid)
        retained = [r for r in summary['remediation'] if r['source_type'] == 'oral_delivered_continuation']
        assert len(retained) == 1, summary['remediation']
        assert retained[0]['source_ref'] == root_id and retained[0]['status'] == 'needs_plan'
        assert retained[0]['mastery_update_allowed'] is False
        assert '还没回答' in summary['child_response'] and '新题还没有交付' not in summary['child_response']
        with db.connect() as c:
            ended = c.execute('SELECT ended_at FROM daily_session WHERE session_id=?', (sid,)).fetchone()[0]
        advance_clock(monkeypatch, datetime.fromisoformat(ended) + timedelta(days=1))
        case['headers']['X-HSC-Parent'] = post('/v1/parent/unlock', dict(pin='8264'))['parent_token']
        sid3 = post('/v1/sessions', dict(child_id=child, client_action_id='delivered-third-day'))['session_id']
        r = client.put(f'/v1/sessions/{sid3}/planning-context', json=dict(energy_level='medium', fixed_commitment_minutes=0,
            client_action_id='delivered-third-context'))
        assert r.status_code == 200, r.text
        # School declarations only are fixtures; lifecycle/planning run through HTTP.
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1,homework_declared_empty=1 WHERE session_id=?", (sid3,))
            c.commit()
        workspace = post(f'/v1/sessions/{sid3}/plan/generate', dict(client_action_id='delivered-third-plan'))
        retained = [d for d in workspace['constraint_decisions'] if d['obligation_type'] == 'oral_delivered_continuation']
        assert len(retained) == 1 and retained[0]['allocated_minutes'] == 0
        binding = json.loads(retained[0]['metadata_json'])['oral_delivered_continuation']
        assert binding['root_commitment_id'] == root_id and binding['prompt_id'] == prompt_before['prompt_id']
        obligations = [r for r in workspace['supply_obligations'] if r.get('root_obligation_id') == root_id]
        assert len(obligations) == 1 and obligations[0]['status'] == 'needs_plan', obligations
        assert '还没回答' in obligations[0]['message']
        repeated = post(f'/v1/sessions/{sid3}/plan/generate', dict(client_action_id='delivered-third-plan'))
        assert repeated['plan']['plan_id'] == workspace['plan']['plan_id']
        assert len([d for d in repeated['constraint_decisions'] if d['obligation_type']=='oral_delivered_continuation']) == 1
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?', (root_id,)).fetchone()) == root_before
            assert dict(c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt_before['prompt_id'],)).fetchone()) == prompt_before
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?', (child,)).fetchone()[0] == 1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?', (sid3,)).fetchone()[0] == 0
        detail = client.get(case['followup_path'], headers=parent_headers(case))
        assert detail.status_code == 200 and detail.json()['status'] == 'needs_plan', detail.text
        # Reading history never reauthorizes execution of the deferred old task.
        from home_study_agent.oral_supply_execution import resolve_execution
        from home_study_agent.oral_supply_carryover import unfinished_deliveries
        with db.connect() as c:
            c.execute('BEGIN')
            with pytest.raises(ValueError):
                resolve_execution(c,sid,case['new_item_id'])
            assert unfinished_deliveries(c,'child-primary-4') == []
        with fresh_oral_case(case, TestClient, client=('127.0.0.1', 41984)) as fresh_case:
            case = fresh_case
            client = reopened = case['client']
            restored = reopened.get(f'/v1/sessions/{sid3}')
            assert restored.status_code == 200, restored.text
            assert [r for r in restored.json()['supply_obligations'] if r.get('root_obligation_id')==root_id] == obligations
            if after_retention:
                after_retention({**case,'sid3':sid3,'workspace3':workspace})
            else:
                # One more closure retains the delivered question, not the older answered question.
                post(f'/v1/sessions/{sid3}/transition',dict(expected_state='plan_review',target_state='study',
                    reason='今天先核对安排。',client_action_id='third-study'))
                for item in client.get(f'/v1/sessions/{sid3}').json()['plan_items']:
                    if item['status'] in ('pending','paused'):
                        post(f"/v1/plan-items/{item['plan_item_id']}/reschedule",dict(reason='今天先结束。',
                            client_action_id='third-defer-'+item['plan_item_id']))
                post(f'/v1/sessions/{sid3}/transition',dict(expected_state='study',target_state='reflection',
                    reason='今天先结束。',client_action_id='third-reflect'))
                post(f'/v1/sessions/{sid3}/reflection/complete',dict(learned_text='',difficult_text='',client_action_id='third-close'))
                again = LearningStateService(db).close_session_learning(sid3)
                assert len([r for r in again['remediation'] if r['source_type']=='oral_delivered_continuation' and r['source_ref']==root_id]) == 1
                assert '还没回答' in again['child_response']
    journey(tmp_path, monkeypatch, subject, after_delivery=close_and_plan)


@pytest.mark.parametrize('fault',['source','receipt','carry_binding','cancel'])
def test_retained_work_rechecks_source_and_never_starts_an_old_task(tmp_path,monkeypatch,fault):
    def alter(case):
        db,client=case['db'],case['client']
        with db.connect() as c:
            if fault=='source':
                c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(case['published']['diagnostic_item_id'],))
            elif fault=='receipt':
                c.execute("DELETE FROM plan_item_event WHERE plan_item_id=? AND event_type='rescheduled'",(case['new_item_id'],))
            elif fault=='carry_binding':
                c.execute("UPDATE plan_constraint_decision SET metadata_json=json_set(metadata_json,'$.oral_delivered_continuation.prompt_id','wrong-prompt') WHERE plan_id=? AND obligation_type='oral_delivered_continuation'",
                    (case['workspace3']['plan']['plan_id'],))
            else:
                c.execute("UPDATE agent_objective SET status='cancelled' WHERE objective_id=(SELECT objective_id FROM agent_plan WHERE agent_turn_id=?)",
                    (case['resume_turn']['agent_turn_id'],))
            c.commit()
        r=client.get(f"/v1/sessions/{case['sid3']}")
        assert r.status_code==200,r.text
        values=[v for v in r.json()['supply_obligations'] if v.get('root_obligation_id')==case['commitment']['commitment_id']]
        if fault=='cancel':
            assert values==[]
        else:
            assert len(values)==1 and values[0]['status']=='source_changed'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(case['child'],)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(case['sid3'],)).fetchone()[0]==0
    test_unanswered_delivery_survives_closure_and_third_plan(tmp_path,monkeypatch,'数学',after_retention=alter)
