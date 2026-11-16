"""A still-undelivered oral promise survives a second closure and a third plan."""
from datetime import datetime,timedelta
import json

import pytest

from home_study_agent.learning_state import LearningStateService
from test_oral_supply_cross_day import test_missing_oral_request_survives_day_close_and_next_plan as journey
from test_writing_stage_review_consumers import advance_clock


@pytest.mark.parametrize('subject',['数学','authored-p4:数学'])
def test_original_request_remains_in_second_summary_and_third_day(tmp_path,monkeypatch,subject):
    def third_day(case):
        client,db,sid,child = case['client'],case['db'],case['next_sid'],case['child']
        root = case['commitment']
        def post(path,body):
            response=client.post(path,json=body)
            assert response.status_code in (200,201),response.text
            return response.json()
        for expected,target in [('plan_review','study'),('study','reflection')]:
            post(f'/v1/sessions/{sid}/transition',dict(expected_state=expected,target_state=target,
                reason='今天先结束，未交付的请求继续保留。',client_action_id='second-'+target))
        post(f'/v1/sessions/{sid}/reflection/complete',dict(learned_text='',difficult_text='',client_action_id='second-close'))
        summary = LearningStateService(db).close_session_learning(sid)
        requests=[r for r in summary['remediation'] if r['source_type']=='oral_question_supply']
        assert len(requests)==1 and requests[0]['source_ref']==root['commitment_id']
        assert requests[0]['objective_id']==root['objective_id'] and requests[0]['status']=='needs_supply'
        assert '新题还没有交付' in summary['child_response']
        with db.connect() as c:
            ended=c.execute('SELECT ended_at FROM daily_session WHERE session_id=?',(sid,)).fetchone()[0]
        advance_clock(monkeypatch,datetime.fromisoformat(ended)+timedelta(days=1))
        client.headers['X-HSC-Parent']=post('/v1/parent/unlock',dict(pin='8264'))['parent_token']
        sid3=post('/v1/sessions',dict(child_id=child,client_action_id='third-day'))['session_id']
        assert client.put(f'/v1/sessions/{sid3}/planning-context',json=dict(energy_level='medium',
            fixed_commitment_minutes=0,client_action_id='third-context')).status_code==200
        # Empty school declarations are fixtures; closure and planning use actual APIs.
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1,homework_declared_empty=1 WHERE session_id=?",(sid3,))
            c.commit()
        workspace=post(f'/v1/sessions/{sid3}/plan/generate',dict(client_action_id='third-plan'))
        retained=[d for d in workspace['constraint_decisions'] if d['obligation_type']=='oral_question_supply']
        assert len(retained)==1 and retained[0]['allocated_minutes']==0
        binding=json.loads(retained[0]['metadata_json'])['oral_supply_carryover']
        assert binding['root_session_id']==case['sid'] and binding['objective_id']==root['objective_id']
        with db.connect() as c:
            assert dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',(root['commitment_id'],)).fetchone())==root
            assert c.execute("SELECT COUNT(*) FROM agent_commitment WHERE child_id=? AND json_extract(payload_json,'$.origin')='oral_question_supply_v1'",(child,)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(child,)).fetchone()[0]==1
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id IN (?,?)',(sid,sid3)).fetchone()[0]==0
        detail=client.get(case['followup_path'])
        assert detail.status_code==200,detail.text
        assert detail.json()['current_session_id']==sid3 and detail.json()['status']=='missing_source'
    journey(tmp_path,monkeypatch,subject,after_plan=third_day)
