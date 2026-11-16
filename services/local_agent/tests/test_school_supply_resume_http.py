"""Actual school supply admission; synthetic reviewed sources, no mastery inserts."""
import json
import base64
import sqlite3
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from home_study_agent.family_clock import family_today
from test_curriculum_first_source_http import CHILD, app_client, auth, first_source_fixture


def post(client, path, body):
    result = client.post(path, json=body)
    assert result.status_code in (200, 201), (path, result.text)
    return result.json()


def configure(client):
    root = f'/v1/parent/children/{CHILD}/goal-programs/junior_exam_readiness'
    result = client.put(root, json=dict(client_action_id='school-gap-configure',
        target_date=(family_today()+timedelta(days=120)).isoformat(), weekly_minutes=120, weekly_sessions=4))
    assert result.status_code == 200, result.text
    result = client.get(root+'/roadmap')
    assert result.status_code == 200, result.text
    return result.json()


def plan(client):
    result = client.post('/v1/sessions', json=dict(child_id=CHILD, client_action_id='school-gap-session'))
    assert result.status_code == 201, result.text
    sid = result.json()['session_id']
    for before, after in [('check_in','classroom_recall'), ('classroom_recall','homework_capture')]:
        result = client.post(f'/v1/sessions/{sid}/transition', json=dict(
            expected_state=before, target_state=after, reason='ready', client_action_id='school-gap-'+after))
        assert result.status_code == 200, result.text
        if after == 'classroom_recall':
            result = client.post(f'/v1/sessions/{sid}/recalls', json=dict(subject='数学',
                summary='今天学习了判别式，想核对自己的基础。', client_action_id='school-gap-recall'))
            assert result.status_code == 201, result.text
        else:
            result = client.post(f'/v1/sessions/{sid}/homework/none', json=dict(client_action_id='school-gap-no-homework'))
            assert result.status_code == 200, result.text
    result = client.post(f'/v1/sessions/{sid}/plan/generate', json=dict(client_action_id='school-gap-plan'))
    assert result.status_code == 200, result.text
    return sid, result.json()


def test_assessment_is_not_a_fake_skill_and_mapping_uses_existing_edge(tmp_path):
    db, _, _, _ = first_source_fixture(tmp_path)
    with app_client(tmp_path, db) as client:
        auth(client)
        roadmap = configure(client)
        action = roadmap['next_daily_action']
        assert action['knowledge_node_id'] == 'node-j3-math-discriminant', action
        assert action['status'] == 'qualified_source_needed'
        binding = action['assessment_binding']
        assert [edge['edge_id'] for edge in binding['edges']] == ['edge-j3-discriminant-assess']
        assert binding['edges'][0]['target_node_id'] == 'node-j3-assessment-quadratic'
        formula = next(row for row in roadmap['prerequisite_path'] if row['knowledge_node_id'] == 'node-j3-math-quadratic-formula')
        assert formula['blocked_by'] == ['node-j3-math-discriminant']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute("SELECT node_type FROM knowledge_node WHERE knowledge_node_id='node-j3-assessment-quadratic'").fetchone()[0] == 'assessment_task'


def test_unmapped_assessment_never_borrows_a_random_skill(tmp_path):
    db, _, _, _ = first_source_fixture(tmp_path)
    with app_client(tmp_path, db) as client:
        auth(client)
        before = configure(client)
        with db.connect() as c:
            c.execute("UPDATE knowledge_edge SET status='retired' WHERE edge_id='edge-j3-discriminant-assess'")
            c.commit()
        result = client.get(f'/v1/parent/children/{CHILD}/goal-programs/junior_exam_readiness/roadmap')
        assert result.status_code == 200, result.text
        after = result.json()
        assert after['next_daily_action'] is None
        assert any(row['code'] == 'assessment_skill_mapping_unavailable' for row in after['risks'])
        assert after['source_version'] != before['source_version']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_missing_school_skill_is_persisted_without_a_fake_task(tmp_path):
    db, _, _, _ = first_source_fixture(tmp_path)
    with app_client(tmp_path, db) as client:
        auth(client)
        action = configure(client)['next_daily_action']
        sid, workspace = plan(client)
        gaps = [row for row in workspace['constraint_decisions'] if row['obligation_type'] == 'junior_exam_goal'
            and row['decision'] == 'deferred']
        assert len(gaps) == 1, workspace['constraint_decisions']
        evidence = gaps[0]['metadata']['planning_evidence']
        assert evidence['knowledge_node_id'] == action['knowledge_node_id']
        assert evidence['school_supply_gap']['version'] == 'school_supply_gap_v1'
        assert not [row for row in workspace['plan_items'] if row['source_type'] == 'junior_exam_goal']
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 0


@pytest.fixture
def school(tmp_path, monkeypatch, request):
    from home_study_agent import supply_gap_resume, learning_input_replan, goal_roadmap
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.model_gateway import ModelGateway
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.local_media_ai import LocalMediaAI
    from test_pet_media_evidence import wav_file
    from test_cloud_first_agent import Planner
    from test_writing_stage_review_consumers import advance_clock
    clock = advance_clock(monkeypatch, datetime.now(timezone.utc).replace(hour=8,minute=0,second=0,microsecond=0))
    db, _, _, _ = first_source_fixture(tmp_path)
    first_root = getattr(request, 'param', 'existing_seed') == 'first_root'
    with db.connect() as c:
        if first_root:
            # Synthetic installation with retired legacy reference items, not
            # invented mastery or a placeholder active diagnostic seed.
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id='node-j3-math-discriminant'")
        quote = '理解一元二次方程的判别式，使用判别式判断实数根的数量。'
        c.execute('UPDATE curriculum_standard_requirement SET requirement_text=? WHERE requirement_id=\'req\'', (quote,))
        c.execute('UPDATE curriculum_standard_page SET content_text=? WHERE source_id=\'std\'', (quote,))
        c.commit()
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed':True})
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
        session_token='school-supply-isolated', model_provider='qwen', model_api_key='synthetic-7ccdfface0190d33a183',
        model_name='isolated-control', voice_intent_model_name='isolated-control')
    calls = []
    def transport(request):
        data = json.loads(json.loads(request.content)['messages'][-1]['content'])
        calls.append(data)
        if set(data) == {'transcript','state'}:
            assert all(type(value) is bool for value in data['state'].values())
            result = dict(role='control', control='task.resume', quote=data['transcript'], confidence=.99,
                selector=dict(subject=None,task_kind=None,relative_order=None))
        else:
            result = Planner('task.resume', {}, '刚才缺题的数学练习补好了，请继续原来的任务。').payload
        return httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(result)}}]})
    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    monkeypatch.setattr('socket.create_connection', lambda *args, **kwargs: pytest.fail('No external network'))
    def client():
        return TestClient(create_app(settings, model_gateway=gateway), client=('127.0.0.1',41400),
            headers={'X-HSC-Session':settings.session_token})
    with ExitStack() as stack:
        api = stack.enter_context(client())
        auth(api)
        rule = api.put(f'/v1/parent/children/{CHILD}/family-rule', json=dict(latest_end_time='21:00',
            max_learning_minutes=120,task_segment_minutes=35,break_minutes=5,pet_min_minutes=0,competition_minutes=0,
            observation_mode='shadow',reminder_budget_per_10m=0))
        assert rule.status_code == 200, rule.text
        initial = configure(api)
        sid, workspace = plan(api)
        post(api, f'/v1/sessions/{sid}/transition', dict(expected_state='plan_review',target_state='study',
            reason='accept original plan',client_action_id='school-accept-plan'))
        original = dict(schema='curriculum_objective_source_v1',subject='数学',grade='初三',
            knowledge_node_id='node-j3-math-discriminant', assessment_mode='objective',answer_type='text',
            prompt='方程 x²+x+1=0 的判别式是多少？只写出判别式数值。',
            expected_answer=dict(local_rule='local_exact_v1',exact_answers=['-3']))
        source = post(api, f'/v1/parent/children/{CHILD}/knowledge-sources', dict(
            source_type='school_material',title='合成隔离判别式原题',subject='数学',grade_band='初三',
            version_label='合成2026',source_ref='isolated:school-gap-discriminant',license_status='family_owned_index',
            parent_reviewed=True,content_text=json.dumps(original,ensure_ascii=False)))
        base = f'/api/parent/children/{CHILD}/curriculum-supply'
        options = api.get(base+f"/sources/{source['source_id']}/options")
        assert options.status_code == 200, options.text
        options = options.json()
        selected = post(api, base+'/select', dict(action_id='school-select-discriminant',expected_version=1,
            scope=dict(grade='初三',subject='数学',edition='合成2026',semester='上学期',standard_scope='第四学段',
                textbook_source_id=source['source_id'])))
        spec = dict(excerpt_id=options['excerpts'][0]['excerpt_id'],requirement_id='req',standard_page=12,standard_quote=quote,
            explanation='合成原题：判别式1-4=-3，完整参考答案仅该数值。',difficulty=1)
        if first_root:
            assert any(row['knowledge_node_id'] == original['knowledge_node_id'] for row in options['first_sources'])
            with db.connect() as c:
                assert c.execute("SELECT COUNT(*) FROM diagnostic_item WHERE knowledge_node_id=? AND status='active'",
                    (original['knowledge_node_id'],)).fetchone()[0] == 0
            spec['knowledge_node_id'] = original['knowledge_node_id']
        else:
            spec.update(seed_item_id='diag-j3-discriminant-1',stage='parallel',validator='curriculum_objective_exact_source_v1')
        draft = post(api, base+('/first-sources' if first_root else '/generate'), dict(action_id='school-source-draft',
            expected_version=0, expected_scope_version=selected['version'], source=spec))
        if first_root:
            assert draft['payload']['seed_item_id'] is None and draft['diagnostic_item_id'] is None
        cid = draft['candidate_id']
        for version, operation in enumerate(('validate','review','publish'),1):
            body = dict(action_id='school-source-'+operation,expected_version=version)
            if operation == 'review':
                body.update(decision='approve',note='核对合成原题与完整参考数值，不作为孩子掌握或迁移证据。')
            published = post(api, base+f'/{cid}/{operation}', body)
        assert published['eligible']
        def say(key):
            text = '刚才缺题的数学练习补好了，请继续原来的任务。'
            monkeypatch.setattr(LocalMediaAI, '_recognize_wav', lambda *a, **k:(text,1000,'fixture',False))
            audio = tmp_path/'school-synthetic.wav'
            wav_file(audio)
            voice = post(api, f'/v1/sessions/{sid}/asr', dict(wav_base64=base64.b64encode(audio.read_bytes()).decode(),
                purpose='agent_command',client_action_id=key+'-capture',capture_reference={'child_id':CHILD}))
            return post(api, f'/v1/sessions/{sid}/agent-turn', dict(transcript=text,client_action_id=key,
                cloud_text_allowed=True,voice_input_id=voice['voice_input_id']))
        case = dict(api=api,db=db,sid=sid,workspace=workspace,initial=initial,base=base,published=published,
            source=source,say=say,client=client,calls=calls,clock=clock,original=original)
        def restart():
            nonlocal api
            stack.pop_all().close()
            api = stack.enter_context(client())
            auth(api,restart=True)
            case['api'] = api
        case['restart'] = restart
        yield case


@pytest.mark.parametrize('school', ['existing_seed', 'first_root'], indirect=True)
def test_real_voice_resumes_exact_supplied_school_obligation(school):
    case = school
    from home_study_agent.task_start_binding import TaskStartBinding
    with case['db'].connect() as c:
        targets = TaskStartBinding.resume_candidates(c,case['sid'],CHILD)
        assert len(targets) == 1, targets
        assert targets[0]['supply_item']['planning_evidence']['knowledge_node_id'] == 'node-j3-math-discriminant'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
    turn = case['say']('school-resume-original')
    assert turn['runtime_capability'] == 'task.resume', turn
    executed = case['api'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert executed.status_code == 200, executed.text
    body = executed.json()['execution']
    assert body['result']['status'] == 'active', body
    assert body['result']['diagnostic_prompt']['prompt']['item']['diagnostic_item_id'] == case['published']['diagnostic_item_id']
    assert case['api'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute").status_code == 200
    with case['db'].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        task = dict(c.execute("SELECT * FROM plan_item WHERE source_type='junior_exam_goal'").fetchone())
        gap = next(row for row in case['workspace']['constraint_decisions'] if row['obligation_type'] == 'junior_exam_goal')
        assert task['source_ref'] == gap['source_ref']
        receipt = json.loads(c.execute("SELECT metadata_json FROM plan_constraint_decision WHERE decision='scheduled' AND source_ref=?",
            (task['source_ref'],)).fetchone()[0])
        assert receipt['supply_resume']['obligation_id'] == gap['decision_id']
    case['restart']()
    recovered = case['api'].get(f"/v1/sessions/{case['sid']}/diagnostic-prompt")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['prompt']['prompt_id'] == body['result']['diagnostic_prompt']['prompt']['prompt_id']
    with case['db'].connect() as c:
        current = c.execute('SELECT * FROM plan_item WHERE plan_item_id=?',(task['plan_item_id'],)).fetchone()
        assert current['status'] == 'paused'
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['withdraw','relation','budget','consent','cancel_capture','next_day'])
def test_arrival_cannot_gain_new_source_or_constraints(school, fault):
    case = school
    turn = case['say']('school-before-'+fault)
    assert turn['runtime_capability'] == 'task.resume', turn
    with case['db'].connect() as c:
        before = [dict(row) for row in c.execute('SELECT * FROM plan_constraint_decision ORDER BY rowid')]
    if fault == 'withdraw':
        post(case['api'],case['base']+f"/{case['published']['candidate_id']}/withdraw", dict(
            action_id='school-withdraw',expected_version=4,note='撤回这道合成原题。'))
    elif fault == 'relation':
        with case['db'].connect() as c:
            c.execute("UPDATE knowledge_edge SET status='retired' WHERE edge_id='edge-j3-discriminant-assess'")
            c.commit()
    elif fault == 'budget':
        rule = case['api'].get(f'/v1/parent/children/{CHILD}/family-rule').json()
        values = {key:rule[key] for key in ('latest_end_time','max_learning_minutes','task_segment_minutes',
            'break_minutes','pet_min_minutes','competition_minutes','observation_mode','reminder_budget_per_10m')}
        result = case['api'].put(f'/v1/parent/children/{CHILD}/family-rule',json={**values,'task_segment_minutes':10})
        assert result.status_code == 200, result.text
    elif fault == 'consent':
        for value in (False,True):
            result = case['api'].put('/v1/parent/privacy',json={'cloud_model_text_allowed':value})
            assert result.status_code == 200, result.text
    elif fault == 'cancel_capture':
        result = case['api'].post(f"/v1/sessions/{case['sid']}/asr/actions/school-before-{fault}-capture/cancel")
        assert result.status_code == 200, result.text
    else:
        case['clock'].current += timedelta(days=1)
    result = case['api'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert result.status_code == 409, result.text
    with case['db'].connect() as c:
        assert [dict(row) for row in c.execute('SELECT * FROM plan_constraint_decision ORDER BY rowid')] == before
        assert c.execute("SELECT COUNT(*) FROM plan_item WHERE source_type='junior_exam_goal'").fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_borrowed_placement_rollback_and_same_voice_key_retry(school):
    case = school
    turn = case['say']('school-rollback-original')
    with case['db'].connect() as c:
        before = [dict(row) for row in c.execute('SELECT * FROM plan_constraint_decision ORDER BY rowid')]
        c.execute("""CREATE TRIGGER isolated_school_start_failure BEFORE INSERT ON plan_item_event
            WHEN NEW.event_type='started' BEGIN SELECT RAISE(ABORT,'isolated-school-start-failure'); END""")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='isolated-school-start-failure'):
        case['api'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    with case['db'].connect() as c:
        assert [dict(row) for row in c.execute('SELECT * FROM plan_constraint_decision ORDER BY rowid')] == before
        assert c.execute("SELECT COUNT(*) FROM plan_item WHERE source_type='junior_exam_goal'").fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 0
        c.execute('DROP TRIGGER isolated_school_start_failure')
        c.commit()
    result = case['api'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert result.status_code == 200, result.text
    with case['db'].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


def test_existing_workspace_receives_original_school_gap_current_projection(school):
    from home_study_agent.repository import StudyRepository
    case = school
    original = [row for row in case['workspace']['supply_obligations'] if row['title'] == '根的判别式']
    assert len(original) == 1 and original[0]['status'] == 'missing_source', case['workspace']['supply_obligations']
    workspace = StudyRepository(case['db']).get_workspace(case['sid'])
    ready = next(row for row in workspace['supply_obligations'] if row['obligation_id'] == original[0]['obligation_id'])
    assert ready['status'] == 'ready' and ready['plan_item_id'] is None
    post(case['api'],case['base']+f"/{case['published']['candidate_id']}/withdraw", dict(
        action_id='school-display-withdraw',expected_version=4,note='撤回合成原题。'))
    blocked = next(row for row in StudyRepository(case['db']).get_workspace(case['sid'])['supply_obligations']
        if row['obligation_id'] == ready['obligation_id'])
    assert blocked['status'] == 'missing_source' and blocked['plan_item_id'] is None
    with case['db'].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0


def test_school_supply_and_original_capture_do_not_cross_children(school):
    from home_study_agent.task_start_binding import TaskStartBinding
    case = school
    other = 'child-primary-4'
    rejected = case['api'].post('/v1/sessions',json=dict(child_id=other,client_action_id='school-other-child'))
    assert rejected.status_code == 409 and rejected.json()['code'] == 'ACTIVE_SESSION_EXISTS'
    with case['db'].connect() as c:
        assert TaskStartBinding.resume_candidates(c,case['sid'],other) == []
        foreign_before = [dict(row) for row in c.execute('SELECT * FROM daily_session WHERE child_id=?',(other,))]
    assert case['api'].get(case['base'].replace(CHILD,other)+f"/{case['published']['candidate_id']}").status_code == 404
    turn = case['say']('school-own-child-only')
    result = case['api'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert result.status_code == 200, result.text
    with case['db'].connect() as c:
        assert [dict(row) for row in c.execute('SELECT * FROM daily_session WHERE child_id=?',(other,))] == foreign_before
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE child_id=?',(other,)).fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?',(other,)).fetchone()[0] == 0


@pytest.mark.parametrize('change', ['withdraw','relation'])
def test_started_original_prompt_loses_current_authority_without_erasing_history(school, change):
    from home_study_agent.repository import StudyRepository
    case = school
    turn = case['say']('school-current-original')
    result = case['api'].post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute")
    assert result.status_code == 200, result.text
    if change == 'withdraw':
        post(case['api'],case['base']+f"/{case['published']['candidate_id']}/withdraw", dict(
            action_id='school-started-withdraw',expected_version=4,note='撤回合成原题。'))
    else:
        with case['db'].connect() as c:
            c.execute("UPDATE knowledge_edge SET status='retired' WHERE edge_id='edge-j3-discriminant-assess'")
            c.commit()
    current = case['api'].get(f"/v1/sessions/{case['sid']}/diagnostic-prompt")
    assert current.status_code == 200, current.text
    assert current.json()['status'] == 'invalidated', current.json()
    workspace = StudyRepository(case['db']).get_workspace(case['sid'])
    assert workspace['supply_obligations'][0]['status'] in {'source_changed','needs_replan'}
    case['restart']()
    current = case['api'].get(f"/v1/sessions/{case['sid']}/diagnostic-prompt")
    assert current.status_code == 200 and current.json()['status'] != 'waiting_answer'
    with case['db'].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 1
