"""Real publication and day-boundary reads; content, ASR and model are synthetic."""
import json
from synthetic_oral_app_lifecycle import parent_headers
from datetime import datetime,timezone

import pytest

from home_study_agent.open_response_source import VALIDATOR
from test_oral_supply_cross_day import test_missing_oral_request_survives_day_close_and_next_plan as journey


def publish(case, subject, *, excerpt_index=1, key='cross-publish'):
    client = case['client']
    base = f"/api/parent/children/{case['child']}/curriculum-supply"
    options = client.get(base+'/sources/'+case['supply_source_id']+'/options', headers=parent_headers(case)).json()
    requirement = next(r for r in options['standards'] if r['requirement_id']=='open-requirement')
    with case['db'].connect() as c:
        version = c.execute('SELECT version FROM curriculum_supply_selection WHERE child_id=? AND subject=? ORDER BY version DESC LIMIT 1',
            (case['child'],subject)).fetchone()[0]
    spec = dict(seed_item_id='diag-j3-discriminant-1' if subject=='数学' else 'diag-j3-physics-experiment-1',
        stage='parallel',excerpt_id=options['excerpts'][excerpt_index]['excerpt_id'],requirement_id='open-requirement',
        standard_page=requirement['page_number'],standard_quote=requirement['quote'],validator=VALIDATOR,
        difficulty=2,explanation='Synthetic task with exact source and reviewed family.')
    response = client.post(base+'/generate',json=dict(action_id=key+'-draft',expected_version=0,
        expected_scope_version=version,source=spec), headers=parent_headers(case))
    assert response.status_code==200,response.text
    path = base+'/'+response.json()['candidate_id']
    for expected,op in enumerate(('validate','review','publish'),1):
        body = dict(action_id=key+'-'+op,expected_version=expected)
        if op=='review':
            body.update(decision='approve',note='Checked synthetic task and source.')
        response = client.post(path+'/'+op,json=body, headers=parent_headers(case))
        assert response.status_code==200,response.text
    assert response.json()['eligible']
    return response.json()


@pytest.mark.parametrize('subject',['数学','物理'])
def test_cross_day_source_uses_original_goal_and_published_unseen_question(tmp_path,monkeypatch,subject,fault=None,after_ready=None):
    from home_study_agent.oral_supply_carryover import resolve_request,revalidate_request
    from test_writing_stage_review_consumers import advance_clock
    advance_clock(monkeypatch,datetime.now(timezone.utc).replace(hour=8,minute=0,second=0,microsecond=0))
    def check(case):
        def resolve():
            with case['db'].connect() as c:
                c.execute('BEGIN')
                before = c.total_changes
                value = resolve_request(c,case['child'],case['next_sid'],case['commitment']['commitment_id'])
                assert c.total_changes==before,'Reading availability must not write a task or prompt.'
                return value
        unavailable = resolve()
        assert unavailable['status']=='blocked' and unavailable['reason']=='oral_no_unexposed_source',unavailable
        detail = case['client'].get(case['followup_path'], headers=parent_headers(case)).json()
        assert detail['status']=='missing_source'
        published = publish(case,subject)
        unknown = resolve()
        assert unknown['reason']=='SUPPLY_RESUME_BUDGET_UNKNOWN',unknown
        path = f"/v1/parent/children/{case['child']}/family-rule"
        rule = case['client'].get(path, headers=parent_headers(case)).json()
        values = {k:rule[k] for k in ('latest_end_time','max_learning_minutes','task_segment_minutes',
            'break_minutes','pet_min_minutes','competition_minutes','observation_mode','reminder_budget_per_10m')}
        values['latest_end_time']='23:59'
        response = case['client'].put(path,json=values, headers=parent_headers(case))
        assert response.status_code==200,response.text
        ready = resolve()
        assert ready['status']=='ready',ready
        binding = ready['binding']
        assert binding['root_objective_id']==case['commitment']['objective_id']
        assert binding['root_session_id']==case['sid'] and binding['session_id']==case['next_sid']
        assert binding['source']['parent_prompt_id']==case['prompt']['prompt_id']
        assert binding['source']['diagnostic_item_id']==published['diagnostic_item_id']
        assert binding['source']['diagnostic_item_id']!=case['prompt']['diagnostic_item_id']
        assert ready['requires_runtime_admission'] and not ready['mastery_claim_allowed']
        assert not ready['cloud_transmission_allowed']
        detail = case['client'].get(case['followup_path'], headers=parent_headers(case)).json()
        assert detail['status']=='needs_plan' and '合格的同能力新题已补齐' in detail['next_action']
        workspace = case['client'].get(f"/v1/sessions/{case['next_sid']}").json()
        local = next(r for r in workspace['supply_obligations'] if r.get('root_obligation_id')==case['commitment']['commitment_id'])
        assert local['status']==detail['status'] and local['message']==detail['next_action']
        with case['db'].connect() as c:
            assert dict(c.execute('SELECT * FROM agent_commitment WHERE commitment_id=?',
                (case['commitment']['commitment_id'],)).fetchone())==case['commitment']
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',
                (case['next_sid'],)).fetchone()[0]==0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(case['child'],)).fetchone()[0]==1
        assert binding['root_objective_id'] not in json.dumps(case['posts'])
        if fault:
            if fault=='consent':
                assert case['client'].put('/v1/parent/privacy',json={'cloud_model_text_allowed':False}, headers=parent_headers(case)).status_code==200
            elif fault=='budget':
                response = case['client'].put(path,json={**values,'latest_end_time':'00:01'}, headers=parent_headers(case))
                assert response.status_code==200,response.text
            else:
                # Adversarial isolated DB fixtures, not real user actions.
                with case['db'].connect() as c:
                    if fault=='cancel_goal':
                        c.execute("UPDATE agent_objective SET status='cancelled' WHERE objective_id=?",(binding['root_objective_id'],))
                    elif fault=='withdraw_source':
                        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(published['diagnostic_item_id'],))
                    elif fault=='withdraw_original':
                        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?",(case['prompt']['diagnostic_item_id'],))
                    elif fault=='changed_answer':
                        c.execute("UPDATE diagnostic_attempt SET submitted_answer='different synthetic answer' WHERE attempt_id=?",(case['attempt']['attempt_id'],))
                    elif fault=='bad_request':
                        c.execute("UPDATE voice_control_intent_request SET fingerprint='wrong' WHERE request_key=?",
                            (json.loads(case['commitment']['payload_json'])['request_key'],))
                    elif fault=='plan':
                        c.execute("UPDATE daily_plan SET version=version+1 WHERE plan_id=?",(binding['plan_id'],))
                    elif fault=='other_child':
                        binding['child_id']='child-primary-4'
                    c.commit()
            with case['db'].connect() as c:
                c.execute('BEGIN')
                before = c.total_changes
                # Rehash a forged cross-child request: rejection must come from
                # ownership validation, not merely from the old digest mismatch.
                from home_study_agent.local_supply import digest
                version = digest(binding) if fault=='other_child' else ready['binding_version']
                checked = revalidate_request(c,binding,version)
                assert checked['status']=='blocked',(fault,checked)
                assert c.total_changes==before
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE origin_session_id=?',(case['next_sid'],)).fetchone()[0]==0
        if after_ready:
            after_ready({**case,'published':published,'resume_source':ready})
    journey(tmp_path,monkeypatch,subject,after_plan=check)


@pytest.mark.parametrize('fault',['cancel_goal','withdraw_source','withdraw_original','changed_answer',
    'bad_request','plan','other_child','consent','budget'])
def test_frozen_cross_day_choice_cannot_outlive_its_evidence_or_plan(tmp_path,monkeypatch,fault):
    test_cross_day_source_uses_original_goal_and_published_unseen_question(tmp_path,monkeypatch,'数学',fault=fault)
