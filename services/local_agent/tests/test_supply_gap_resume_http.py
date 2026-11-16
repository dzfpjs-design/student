"""External V8 probe: no edits to repository, no real network or family data."""
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.pet_writing_stage_plan import project_writing_stage_plan
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from test_pet_typed_source_import import CHILD, add_standard, packet, values, encode
from test_voice_runtime_binding import capture, interpret
from test_cloud_first_agent import Planner


@pytest.fixture
def supplied(tmp_path, monkeypatch, request):
    from home_study_agent import learning_input_replan, supply_gap_resume, scheduled_writing_goal, pet_training_plan, paper_writing_projection
    from test_writing_stage_review_consumers import advance_clock
    # This fixture exercises missing supply, not the runner's evening deadline.
    # Freeze before creating evidence so all actual writes share one learning day.
    clock = advance_clock(monkeypatch, datetime.now(timezone.utc).replace(
        hour=10, minute=0, second=0, microsecond=0))
    db = Database(tmp_path / 'supply-resume.db')
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    add_standard(db)
    # Only initial isolated catalog/policy setup. No plan, prompt, or receipt inserts.
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id!='diag-pet-writing-1' AND knowledge_node_id IN (SELECT knowledge_node_id FROM knowledge_node WHERE track='pet')")
        c.execute("UPDATE family_rule SET latest_end_time='23:59',max_learning_minutes=120,task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?", (CHILD,))
        c.commit()
    privacy = PrivacyLifecycleService(db, tmp_path, data_encryption_key=b'8' * 32)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        model_provider='qwen', model_api_key='synthetic-c628128d732429c54cfa', model_name='isolated-control',
        voice_intent_model_name='isolated-control', session_token='isolated-c03',
        data_encryption_key=privacy.data_encryption_key)
    requests = []
    speech = {'control':'task.resume', 'selector':{'subject':None,'task_kind':None,'relative_order':None}}
    def transport(request):
        data = json.loads(json.loads(request.content)['messages'][-1]['content'])
        requests.append(data)
        if set(data) == {'transcript', 'state'}:
            assert all(type(v) is bool for v in data['state'].values()), data
            decision = {'role': 'control', 'control': speech['control'], 'quote': data['transcript'], 'confidence': .99}
            if speech['control']=='task.resume':
                decision['selector']=speech['selector']
        elif set(data) == {'question','response','rubric'}:
            decision = {'understanding':'independent','confidence':.99,
                'evidence_summary':'Controlled synthetic formative output, not calibrated scoring',
                'spoken_feedback':'Check that the invitation says where and when to meet.',
                'next_check':'Try another original task independently.',
                'rubric_dimensions':[dict(dimension_id=d['dimension_id'],score=2,response_quotes=[data['response']],
                    rationale='Reference feedback on this original response') for d in data['rubric']['dimensions']],
                'criterion_evidence':[dict(criterion=p,response_quote=data['response']) for p in data['rubric']['task_points']]}
            from formative_response_fixture import criterion_reply
            decision=criterion_reply(data,decision)
        else:
            # With no bound task, current production chooses the ordinary planner.
            # Return its real schema rather than making a transport mismatch the red.
            decision = Planner('task.resume', {}, speech['quote']).payload
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(decision)}}]})
    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    # Catch any transport accidentally bypassing the controlled ModelGateway.
    def no_socket(*args, **kwargs):
        from synthetic_unclean_app_process import owned_synthetic_connection
        return owned_synthetic_connection(*args, **kwargs)
    monkeypatch.setattr('socket.create_connection', no_socket)
    def post(client, path, body):
        result = client.post(path, json=body)
        assert result.status_code in (200, 201), (path, result.status_code, result.text[:800])
        return result.json()
    report = {'database': str(db.path), 'synthetic_only': True,
              'initial_conditions': 'Other PET inventory retired; unchanged writing seed gets actual unqualified reference attempt; no target plan/prompt filled.'}
    with TestClient(create_app(settings, model_gateway=gateway), headers={'X-HSC-Session': settings.session_token},
                    client=('127.0.0.1', 41400)) as client:
        token = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        client.headers['X-HSC-Parent'] = token
        sid = post(client, '/v1/sessions', {'child_id': CHILD, 'client_action_id': 'original-gap-day'})['session_id']
        if getattr(request, 'param', None) == 'listening_wait':
            context=client.put(f'/v1/sessions/{sid}/planning-context',json={'energy_level':'medium',
                'fixed_commitment_minutes':0,'client_action_id':'explicit-listening-plan-context'})
            assert context.status_code==200,context.text
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        prior = LearningStateService(db).submit_diagnostic(sid, 'diag-pet-writing-1',
            'I do not know how to write this yet.', 'prior-reference-only')
        if getattr(request, 'param', None) == 'listening_wait':
            with db.connect() as c:
                c.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id='diag-pet-listening-1'")
                c.commit()
            LearningStateService(db).submit_diagnostic(sid, 'diag-pet-listening-1',
                'I do not know yet.', 'prior-listening-reference-only')
            with db.connect() as c:
                c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id='diag-pet-listening-1'")
                c.commit()
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (sid,))
            c.commit()
        if getattr(request, 'param', None) == 'listening_wait':
            post(client,f'/v1/sessions/{sid}/homework',{'subject':'数学','title':'口算',
                'expected_minutes':5,'client_action_id':'original-school-work-for-replan'})
        workspace = post(client, f'/v1/sessions/{sid}/plan/generate', {'client_action_id': 'original-gap-plan'})
        if getattr(request, 'param', None) == 'listening_wait':
            with db.connect() as c:
                c.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id='diag-pet-listening-1'")
                c.commit()
        gaps = [d for d in workspace['constraint_decisions'] if d['obligation_type']=='pet_daily' and
                ('writing' in str(d['source_ref']) or '写作' in str(d['reason']))]
        writing_slots = [p for p in workspace['plan_items'] if p['source_type']=='pet_daily' and p['task_type']=='writing']
        report.update(session_id=sid, initial_gaps=gaps, initial_writing_slots=writing_slots, prior_attempt=prior)
        (tmp_path / 'evidence.json').write_text(encode(report, indent=2))
        assert gaps and not writing_slots, 'Probe precondition must be real persisted missing fresh writing source'
        post(client, f'/v1/sessions/{sid}/transition', {'expected_state':'plan_review',
            'target_state':'study','reason':'isolated_user_accepts_real_plan','client_action_id':'accept-original-gap-plan'})
        base = f'/api/parent/children/{CHILD}/curriculum-supply'
        original = packet('writing')
        second = packet('writing', 2)
        second['prompt'] = 'Write an email to Ben about your new science club. Explain what you want to build and invite him to join. Write 35-55 words.'
        second['task_points'] = ['Describe the science club', 'Explain what you want to build', 'Invite Ben to join']
        reading = packet('objective')
        reading['knowledge_node_id']='node-pet-reading-main-idea'
        source_values = values('\n'.join(encode(p) for p in (original,second,reading)))
        if getattr(request, 'param', None) == 'public':
            source_values['license_status'] = 'public'
        imported = post(client, f'/v1/parent/children/{CHILD}/knowledge-sources', source_values)
        options_response = client.get(base + f"/sources/{imported['source_id']}/options")
        assert options_response.status_code == 200, options_response.text[:800]
        options = options_response.json()
        seed = next(s for s in options['seeds'] if s['validator']=='pet_writing_rubric_source_v1')
        standard = next(s for s in options['standards'] if s['requirement_id']=='c03-req')
        assert standard['available']
        selected = post(client, base+'/select', {'action_id':'select-new-source','expected_version':0,'scope':{
            'grade':'四年级','subject':'英语','edition':'isolated-c03-v1','semester':'上学期',
            'standard_scope':standard['stage_scope'],'textbook_source_id':imported['source_id']}})
        draft = post(client, base+'/generate', {'action_id':'supply-new-question','expected_version':0,
            'expected_scope_version':selected['version'],'source':{'seed_item_id':seed['seed_item_id'],
            'stage':'parallel','excerpt_id':options['excerpts'][0]['excerpt_id'],
            'requirement_id':standard['requirement_id'],'standard_page':standard['page_number'],
            'standard_quote':standard['quote'],'validator':seed['validator'],'difficulty':1,
            'explanation':'Synthetic original source reviewed against full written task; no exact score or child mastery claim.'}})
        cid = draft['candidate_id']
        for version, operation in enumerate(('validate','review','publish'),1):
            body = {'expected_version':version,'action_id':'parent-'+operation}
            if operation=='review':
                body.update(decision='approve',note='Checked the original source and task points; no child skill claim.')
            published = post(client, base+f'/{cid}/{operation}', body)
            assert published['state']=={'validate':'validated','review':'reviewed','publish':'published'}[operation], published
        assert published['eligible']
        with db.connect() as c:
            current = project_writing_stage_plan(c, CHILD, as_of=clock.now(timezone.utc).isoformat())
        report.update(candidate_id=cid, published_item_id=published['diagnostic_item_id'], current_inventory=current)
        (tmp_path / 'evidence.json').write_text(encode(report, indent=2))
        assert any(p['source_ref']==published['diagnostic_item_id'] for p in current['items']), 'Published real source must be independently ready for scheduling'
        def say(key, quote='刚才缺题的写作练习，题目补好了，请继续原来的任务。', *, prompt=None):
            speech['quote'] = quote
            voice = capture(client, settings, monkeypatch, sid, quote, prompt, key=key+'-voice')
            return interpret(client, sid, quote, key, voice)
        yield dict(db=db, sid=sid, client=client, settings=settings, gateway=gateway, gateway_transport=transport,
            post=post, say=say, speech=speech, report=report, requests=requests,
            base=base, published=published, draft=draft, selected=selected, options=options,
            standard=standard, seed=seed, source=imported, tmp_path=tmp_path)


def facts(case):
    with case['db'].connect() as c:
        return {table:[dict(r) for r in c.execute(f'SELECT * FROM {table}')]
                for table in ('daily_plan','plan_item','plan_constraint_decision','diagnostic_prompt','plan_item_event')}


def execute(case, turn):
    assert turn.get('runtime_capability') == 'task.resume', turn
    assert turn['runtime_execution_location'] == 'server', turn
    return case['client'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")


def start(case):
    before = facts(case)
    turn = case['say']('resume-original-gap-command')
    result = execute(case, turn)
    assert result.status_code == 200, result.text[:1500]
    after = facts(case)
    task = next(p for p in after['plan_item'] if p['source_ref']==case['published']['diagnostic_item_id'])
    prompt = next(p for p in after['diagnostic_prompt'] if p['diagnostic_item_id']==task['source_ref'])
    assert task['status']=='active' and task['started_at']
    assert after['daily_plan'][0]['version']==before['daily_plan'][0]['version']+1
    old = {r['decision_id']:r for r in before['plan_constraint_decision']}
    assert {r['decision_id']:r for r in after['plan_constraint_decision'] if r['decision_id'] in old} == old
    assert all(p in after['plan_item'] for p in before['plan_item'])
    metadata = next(json.loads(d['metadata_json']) for d in after['plan_constraint_decision']
                    if json.loads(d['metadata_json']).get('plan_item_id')==task['plan_item_id'])
    assert metadata['placement_plan_version']==after['daily_plan'][0]['version']
    assert metadata['supply_resume']['plan_version']==before['daily_plan'][0]['version']
    return turn, task, prompt


def test_empty_fresh_inventory_parent_publish_natural_resume(supplied):
    case = supplied
    turn, task, prompt = start(case)
    before = facts(case)
    replay = execute(case, turn)
    assert replay.status_code == 200, replay.text
    assert facts(case)==before
    case['report'].update(turn=turn, task=task, prompt=prompt, after=before)
    (case['tmp_path']/'evidence.json').write_text(encode(case['report'], indent=2))


@pytest.mark.parametrize('fault', ['withdraw', 'reapprove', 'consent_regrant', 'budget', 'late', 'cas', 'cancel', 'child'])
def test_original_arrival_cannot_gain_replacement_authority(supplied, monkeypatch, fault):
    from home_study_agent import supply_gap_resume
    case = supplied
    turn = case['say']('arrival-before-'+fault)
    before = facts(case)
    if fault in {'withdraw', 'reapprove'}:
        case['post'](case['client'], case['base']+f"/{case['draft']['candidate_id']}/withdraw",
            dict(expected_version=4, action_id='withdraw-original', note='Original authorization withdrawn'))
        if fault == 'reapprove':
            # A new explicitly reviewed publication is new authority. Old
            # publication receipt replay reports withdrawn rather than reviving it.
            old = case['post'](case['client'], case['base']+f"/{case['draft']['candidate_id']}/publish",
                dict(expected_version=3, action_id='parent-publish'))
            assert old['state']=='withdrawn'
            replacement = publish_next(case, key='reauthorized-distinct-source')
            assert replacement['diagnostic_item_id'] != case['published']['diagnostic_item_id']
    elif fault == 'consent_regrant':
        for allowed in (False,True):
            result=case['client'].put('/v1/parent/privacy',json={'cloud_model_text_allowed':allowed})
            assert result.status_code==200,result.text
    elif fault == 'budget':
        rule = case['client'].get(f'/v1/parent/children/{CHILD}/family-rule').json()
        changed = case['client'].put(f'/v1/parent/children/{CHILD}/family-rule', json={
            **{k:rule[k] for k in ('latest_end_time','max_learning_minutes','task_segment_minutes','break_minutes',
                'pet_min_minutes','competition_minutes','observation_mode','reminder_budget_per_10m')},
            'task_segment_minutes':10})
        assert changed.status_code==200, changed.text
    elif fault == 'late':
        from zoneinfo import ZoneInfo
        day = before['daily_plan'][0]['plan_date']
        instant = datetime.fromisoformat(day+'T23:58:00+08:00')
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz or ZoneInfo('Asia/Shanghai'))
        monkeypatch.setattr(supply_gap_resume, 'datetime', Clock)
    else:
        with case['db'].connect() as c:
            if fault == 'cas':
                c.execute('UPDATE daily_plan SET version=version+1 WHERE session_id=?', (case['sid'],))
            elif fault == 'cancel':
                c.execute("UPDATE agent_step SET status='cancelled',error_code='USER_CANCELLED' WHERE plan_id IN (SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)", (turn['agent_turn_id'],))
            else:
                c.execute("UPDATE agent_turn SET child_id='child-junior-3' WHERE agent_turn_id=?", (turn['agent_turn_id'],))
            c.commit()
    result = execute(case, turn)
    assert result.status_code==409, (fault, result.status_code, result.text[:1200])
    after = facts(case)
    assert after['plan_item']==before['plan_item'] and after['diagnostic_prompt']==before['diagnostic_prompt']
    assert after['plan_item_event']==before['plan_item_event']
    assert after['plan_constraint_decision']==before['plan_constraint_decision']


def publish_next(case, *, number=1, key='independent-source'):
    draft = case['post'](case['client'], case['base']+'/generate', {
        'action_id':key, 'expected_version':0, 'expected_scope_version':case['selected']['version'],
        'source':{'seed_item_id':case['seed']['seed_item_id'], 'stage':'parallel',
            'excerpt_id':case['options']['excerpts'][number]['excerpt_id'],
            'requirement_id':case['standard']['requirement_id'], 'standard_page':case['standard']['page_number'],
            'standard_quote':case['standard']['quote'], 'validator':case['seed']['validator'],
            'difficulty':1, 'explanation':'An independently authored local task; publication does not claim learning.'}})
    for version, operation in enumerate(('validate','review','publish'),1):
        body = {'expected_version':version, 'action_id':key+'-'+operation}
        if operation=='review':
            body.update(decision='approve', note='Checked the original prompt and its task points.')
        result = case['post'](case['client'], case['base']+f"/{draft['candidate_id']}/{operation}",body)
    assert result['eligible'], result
    return result
