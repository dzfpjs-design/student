"""Preserve a real unavailable oral request across actual day-end and planning.

Audio, model answers, standard excerpts and authored questions are synthetic.
"""
from datetime import datetime, timedelta
import json
from synthetic_oral_app_lifecycle import fresh_oral_case, parent_headers

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.learning_state import LearningStateService
from test_oral_supply_published_reminder_http import (
    test_published_source_releases_saved_supply_request_without_losing_original_goal as journey,
)
from test_writing_stage_review_consumers import advance_clock


@pytest.mark.parametrize('subject',['数学','物理','authored-p4:数学'])
def test_missing_oral_request_survives_day_close_and_next_plan(tmp_path,monkeypatch,subject,fault=None,after_plan=None):
    def next_day(case):
        client,db,sid,child = (case[k] for k in ('client','db','sid','child'))
        client.headers.update(case['headers'])
        root = case['commitment']
        def post(path,body):
            response = client.post(path,json=body)
            assert response.status_code in (200,201),response.text
            return response.json()
        post(f'/v1/sessions/{sid}/transition',dict(expected_state='study',target_state='reflection',
            reason='今天结束，原来的换题请求保留。',client_action_id='oral-close-reflect'))
        post(f'/v1/sessions/{sid}/reflection/complete',dict(learned_text='',difficult_text='',
            client_action_id='oral-close'))
        with db.connect() as c:
            closed = dict(c.execute('SELECT * FROM daily_session WHERE session_id=?',(sid,)).fetchone())
        assert closed['status']=='completed'
        closure = LearningStateService(db).close_session_learning(sid)
        oral = [r for r in closure['remediation'] if r['source_type']=='oral_question_supply']
        assert len(oral)==1 and oral[0]['source_ref']==root['commitment_id']
        assert '新题还没有交付' in closure['child_response']
        with db.connect() as c:
            duplicate = c.execute("""SELECT 1 FROM agent_commitment WHERE objective_id=?
                AND status IN ('pending','due') AND json_extract(payload_json,'$.origin')='daily_learning_closure'""",
                (root['objective_id'],)).fetchone()
            assert duplicate is None,'Do not create a generic original-answer retry instead of the promised new question.'
        advance_clock(monkeypatch,datetime.fromisoformat(closed['ended_at'])+timedelta(days=1))
        token = post('/v1/parent/unlock',dict(pin='8264'))['parent_token']
        client.headers['X-HSC-Parent']=token
        if fault=='cancelled':
            with db.connect() as c:
                c.execute("UPDATE agent_objective SET status='cancelled' WHERE objective_id=?",(root['objective_id'],))
                c.commit()
        target_child = 'child-primary-4' if fault=='other_child' else child
        new_sid = post('/v1/sessions',dict(child_id=target_child,client_action_id='oral-next-day'))['session_id']
        assert client.put(f'/v1/sessions/{new_sid}/planning-context',json=dict(energy_level='medium',
            fixed_commitment_minutes=0,client_action_id='oral-new-context')).status_code==200
        # Only initial declarations are a fixture; the plan and carryover are real.
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?",(new_sid,))
            c.commit()
        body = dict(client_action_id='oral-next-plan')
        if fault=='write_failure':
            with db.connect() as c:
                c.executescript("""CREATE TRIGGER fail_oral_carry BEFORE INSERT ON plan_constraint_decision
                    WHEN NEW.obligation_type='oral_question_supply'
                    BEGIN SELECT RAISE(ABORT,'synthetic oral carry failure'); END;""")
            failed = client.post(f'/v1/sessions/{new_sid}/plan/generate',json=body)
            assert failed.status_code>=500,failed.text
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM daily_plan WHERE session_id=?',(new_sid,)).fetchone()[0]==0
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(new_sid,)).fetchone()[0]==0
                c.execute('DROP TRIGGER fail_oral_carry')
                c.commit()
        workspace = post(f'/v1/sessions/{new_sid}/plan/generate',body)
        matching = [r for r in workspace['constraint_decisions'] if r['obligation_type']=='oral_question_supply']
        if fault in ('cancelled','other_child'):
            assert matching==[]
            assert not any(r.get('root_obligation_id')==root['commitment_id'] for r in workspace['supply_obligations'])
            return
        assert len(matching)==1 and matching[0]['source_ref']==root['commitment_id']
        assert matching[0]['decision']=='deferred' and matching[0]['allocated_minutes']==0
        manifest = json.loads(matching[0]['metadata_json'])['oral_supply_carryover']
        assert manifest['root_session_id']==sid and manifest['objective_id']==root['objective_id']
        assert manifest['source_hash']==json.loads(root['payload_json'])['source_hash']
        assert manifest['cloud_transmission_allowed'] is False
        assert any(r.get('root_obligation_id')==root['commitment_id'] for r in workspace['supply_obligations'])
        repeated = post(f'/v1/sessions/{new_sid}/plan/generate',body)
        assert [r['decision_id'] for r in repeated['constraint_decisions']]==[r['decision_id'] for r in workspace['constraint_decisions']]
        detail = client.get(case['followup_path'])
        if detail.status_code!=200:
            from home_study_agent.parent_work_queue import ParentWorkQueueService
            ParentWorkQueueService(db).supply_followup(child,root['commitment_id'])
        assert detail.status_code==200,detail.text
        assert detail.json()['current_session_id']==new_sid
        assert detail.json()['status']=='missing_source'
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(root['commitment_id'],)).fetchone())==root
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(new_sid,)).fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(child,)).fetchone()[0]==1
        with fresh_oral_case(case, TestClient, client=('127.0.0.1', 41976)) as fresh_case:
            case = fresh_case
            client = restarted = case['client']
            restored = restarted.get(f'/v1/sessions/{new_sid}')
            assert restored.status_code==200,restored.text
            retained = [r for r in restored.json()['constraint_decisions'] if r['obligation_type']=='oral_question_supply']
            assert retained==matching
            external_requests = json.dumps(case['posts'],ensure_ascii=False)
            assert root['commitment_id'] not in external_requests
            assert manifest['source_hash'] not in external_requests
            assert 'oral_supply_carryover' not in external_requests
            if after_plan:
                after_plan({**case,'next_sid':new_sid,'next_plan':workspace,'carried':matching[0]})
    if subject.startswith('authored-p4:'):
        from test_oral_supply_obligation_http import test_missing_question_request_replay_and_restart_keep_original_goal as primary_journey
        def after_pending(j,root):
            next_day({**j,'commitment':root,
                'followup_path':f"/v1/parent/children/{j['child']}/supply-followups/{root['commitment_id']}"})
            return True
        primary_journey(tmp_path,monkeypatch,subject,None,after_pending=after_pending,
            http_client_address=('127.0.0.1',41977))
    else:
        journey(tmp_path,monkeypatch,subject,after_missing=next_day)


@pytest.mark.parametrize('fault',['write_failure','cancelled','other_child'])
def test_cross_day_plan_recovery_and_original_scope(tmp_path,monkeypatch,fault):
    test_missing_oral_request_survives_day_close_and_next_plan(tmp_path,monkeypatch,'数学',fault=fault)
