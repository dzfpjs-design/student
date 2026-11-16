"""Real generated PET plans must bind the original question before a timer."""
import json

import pytest

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService, _version
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository, PlanInputError
from test_goal_task_prompt import command
from pet_listening_fixture import listening_resources


CHILD = 'child-primary-4'


def generated(tmp_path, skill='listening', *, with_homework=False):
    db = Database(tmp_path / 'pet-task.db')
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    sid = repo.start_session(CHILD, 'pet-generated-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''', (CHILD,))
        c.execute('''UPDATE diagnostic_item SET status='retired' WHERE knowledge_node_id IN
            (SELECT knowledge_node_id FROM knowledge_node WHERE track='pet' AND code NOT LIKE ?)''',
            ('PET.' + skill.upper() + '.%',))
        c.commit()
    if with_homework:
        repo.add_homework(sid, '数学', '隔离测试校内练习', 10, 'written', None, None, None, None, None, 'homework-before-pet')
    workspace = repo.generate_plan(sid, 'pet-generated-plan')
    slots = [item for item in workspace['plan_items'] if item['source_type'] == 'pet_daily'
             and not str(item['source_ref']).startswith('pet-vocabulary-due:')]
    assert len(slots) == 1, workspace['constraint_decisions']
    slot = slots[0]
    with db.connect() as c:
        original = DiagnosticPromptService._item(c, CHILD, slot['source_ref'])
    return db, sid, slot['plan_item_id'], original['knowledge_node_id'], original


def state(db, pid):
    with db.connect() as c:
        return dict(c.execute('SELECT * FROM plan_item WHERE plan_item_id=?', (pid,)).fetchone())


def published_listening_case(tmp_path):
    from test_pet_reviewed_source_publication import prepared, publish
    db, supply, spec, _ = prepared(tmp_path)
    published = publish(supply, spec)
    repo = StudyRepository(db)
    sid = repo.start_session(CHILD, 'published-listening-session')['session_id']
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.execute('''UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=120,
            task_segment_minutes=30,pet_min_minutes=15,competition_minutes=0 WHERE child_id=?''', (CHILD,))
        c.execute('''UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id NOT IN (?,?)
            AND knowledge_node_id IN (SELECT knowledge_node_id FROM knowledge_node WHERE track='pet')''',
            (published['diagnostic_item_id'], spec['seed_item_id']))
        c.commit()
    LearningStateService(db).submit_diagnostic(sid, spec['seed_item_id'], 'I am not sure', 'seed-reference-only')
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1 WHERE session_id=?", (sid,))
        c.commit()
    workspace = repo.generate_plan(sid, 'published-listening-plan')
    slot = next(item for item in workspace['plan_items'] if item['source_type'] == 'pet_daily'
                and item['source_ref'] == published['diagnostic_item_id'])
    with db.connect() as c:
        original = DiagnosticPromptService._item(c, CHILD, slot['source_ref'])
    return (db, sid, slot['plan_item_id'], original['knowledge_node_id'], original), supply, published


@pytest.mark.parametrize('withdraw_before_start', [False, True])
def test_real_app_published_listening_exact_prompt_restart_and_source_withdrawal(tmp_path, withdraw_before_start, listening_resources):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_learning_runtime_http import NoCloud
    case, supply, published = published_listening_case(tmp_path)
    db, sid, pid, _, original = case
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    def withdraw():
        supply.withdraw(CHILD, published['candidate_id'], actor='parent', note='Original source withdrawn',
                        action_id='withdraw-listening-original', expected_version=4)
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        tid = command(case, child_id=CHILD)
        if withdraw_before_start:
            withdraw()
        response = client.post(f'/v1/agent-turns/{tid}/execute')
        assert response.status_code == 200, response.text
        result = response.json()['execution']
        if withdraw_before_start:
            assert result['result']['status'] == 'blocked', result
            assert state(db, pid)['started_at'] is None
            with db.connect() as c:
                assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 0
            return
        prompt = result['result']['diagnostic_prompt']['prompt']
        assert prompt['diagnostic_item_id'] == original['diagnostic_item_id']
        assert prompt['item_version'] == _version(original)
        assert prompt['item']['pet_audio_modality'] == 'listening'
        assert original['stimulus_text'] not in result['response_text']
        assert state(db, pid)['status'] == 'active'
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        retry = client.post(f'/v1/agent-turns/{tid}/execute')
        assert retry.status_code == 200, retry.text
        assert retry.json()['execution'] == result
        assert DiagnosticPromptService(Database(db.path)).read(sid, prompt_id=prompt['prompt_id'])['status'] == 'waiting_answer'
        withdraw()
        invalid = DiagnosticPromptService(Database(db.path)).answer(sid, prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'], answer='red bridge', client_action_id='withdrawn-original-answer')
        assert invalid['status'] == 'invalidated'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt WHERE diagnostic_item_id=?',
                             (original['diagnostic_item_id'],)).fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1


@pytest.mark.parametrize('skill', ['listening', 'reading', 'speaking'])
def test_generated_plan_freezes_original_and_real_executor_prepares_before_timer(tmp_path, skill, listening_resources):
    case = generated(tmp_path, skill)
    db, sid, pid, node, original = case
    with db.connect() as c:
        records = c.execute('''SELECT metadata_json FROM plan_constraint_decision
            WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)
            AND decision='scheduled' AND source_ref=?''', (pid, original['diagnostic_item_id'])).fetchall()
        metadata = next(json.loads(row[0]) for row in records if json.loads(row[0]).get('plan_item_id') == pid)
        binding = metadata['planning_evidence'].get('pet_question_binding')
        assert binding and binding['item_version'] == _version(original)
        assert binding['diagnostic_item_id'] == original['diagnostic_item_id']
        assert binding['knowledge_node_id'] == node and binding['source_ref'] == original['source_ref']
        assert binding['child_id'] == CHILD and binding['skill'] == skill
    tid = command(case, child_id=CHILD)
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(tid)
    assert result['result']['status'] == 'active'
    prompt = result['result'].get('diagnostic_prompt')
    assert prompt and prompt['status'] == 'waiting_answer'
    assert prompt['prompt']['diagnostic_item_id'] == original['diagnostic_item_id']
    assert prompt['prompt']['item_version'] == _version(original)
    assert result['requires_child_reply']
    if skill == 'listening':
        assert original['stimulus_text'] not in result['response_text']
        assert prompt['prompt']['item']['pet_audio_modality'] == 'listening'
    reopened = Database(db.path)
    assert AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(tid) == result
    with reopened.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['withdraw', 'question', 'node', 'binding_missing', 'other_child'])
def test_changed_original_or_missing_binding_never_starts_timer_or_selects_another(tmp_path, fault):
    case = generated(tmp_path)
    db, sid, pid, node, original = case
    tid = command(case, child_id=CHILD)
    with db.connect() as c:
        if fault == 'withdraw':
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (original['diagnostic_item_id'],))
        elif fault == 'question':
            c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?", (original['diagnostic_item_id'],))
        elif fault == 'node':
            c.execute("UPDATE knowledge_node SET knowledge_version='changed-version' WHERE knowledge_node_id=?", (node,))
        else:
            for row in c.execute('SELECT decision_id,metadata_json FROM plan_constraint_decision').fetchall():
                metadata = json.loads(row['metadata_json'])
                if metadata.get('plan_item_id') != pid:
                    continue
                if fault == 'binding_missing':
                    metadata['planning_evidence'].pop('pet_question_binding', None)
                else:
                    metadata['planning_evidence'].setdefault('pet_question_binding', {})['child_id'] = 'child-junior-3'
                c.execute('UPDATE plan_constraint_decision SET metadata_json=? WHERE decision_id=?',
                          (json.dumps(metadata), row['decision_id']))
        c.commit()
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(tid)
    assert result['result']['status'] == 'blocked', result
    assert state(db, pid)['started_at'] is None
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 0


def test_legacy_direct_start_cannot_skip_prompt(tmp_path):
    db, sid, pid, _, _ = generated(tmp_path)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    with pytest.raises(PlanInputError):
        StudyRepository(db).start_plan_item(pid, 'unbound-legacy-start')
    assert state(db, pid)['started_at'] is None


@pytest.mark.parametrize('after_commit', [False, True])
def test_lost_start_receipt_recovers_same_prompt_and_does_not_duplicate_timer(tmp_path, monkeypatch, after_commit, listening_resources):
    case = generated(tmp_path)
    db, _, pid, _, original = case
    tid = command(case, child_id=CHILD)
    repo = StudyRepository(db)
    start = repo.start_plan_item
    def fail(*args, **kwargs):
        if after_commit:
            start(*args, **kwargs)
        raise RuntimeError('synthetic lost timer receipt')
    monkeypatch.setattr(repo, 'start_plan_item', fail)
    with pytest.raises(RuntimeError, match='synthetic lost'):
        AgentCapabilityExecutor(db, repo).execute(tid)
    with db.connect() as c:
        saved = c.execute('SELECT prompt_id FROM diagnostic_prompt').fetchone()
        assert saved is not None
    reopened = Database(db.path, media_root=tmp_path)
    result = AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(tid)
    assert result['result']['diagnostic_prompt']['prompt']['prompt_id'] == saved[0]
    assert result['result']['diagnostic_prompt']['prompt']['diagnostic_item_id'] == original['diagnostic_item_id']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['source', 'cancel', 'binding'])
def test_prepare_to_timer_race_never_leaves_active_task(tmp_path, monkeypatch, fault, listening_resources):
    from home_study_agent.task_start_binding import TaskStartBindingError
    case = generated(tmp_path)
    db, sid, pid, _, original = case
    tid = command(case, child_id=CHILD)
    repo = StudyRepository(db)
    start = repo.start_plan_item
    def raced(*args, **kwargs):
        with db.connect() as c:
            if fault == 'source':
                c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (original['diagnostic_item_id'],))
            elif fault == 'cancel':
                c.execute("UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (tid,))
            else:
                c.execute("UPDATE plan_constraint_decision SET metadata_json=json_remove(metadata_json,'$.planning_evidence.pet_question_binding') WHERE source_ref=?", (original['diagnostic_item_id'],))
            c.commit()
        return start(*args, **kwargs)
    monkeypatch.setattr(repo, 'start_plan_item', raced)
    with pytest.raises((PlanInputError, TaskStartBindingError)):
        AgentCapabilityExecutor(db, repo).execute(tid)
    assert state(db, pid)['status'] == 'pending'
    assert state(db, pid)['active_segment_started_at'] is None
    with db.connect() as c:
        saved = c.execute('SELECT prompt_id,item_version FROM diagnostic_prompt').fetchone()
    result = DiagnosticPromptService(Database(db.path)).answer(sid, prompt_id=saved['prompt_id'],
        item_version=saved['item_version'], answer='I am not sure', client_action_id='raced-answer')
    assert result['status'] == 'invalidated'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['source', 'plan_snapshot'])
def test_started_prompt_rechecks_original_without_new_exposure_blocker(tmp_path, fault):
    case = generated(tmp_path, 'reading')
    db, sid, pid, _, original = case
    tid = command(case, child_id=CHILD)
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(tid)
    prompt = result['result']['diagnostic_prompt']['prompt']
    assert DiagnosticPromptService(db).read(sid, prompt_id=prompt['prompt_id'])['status'] == 'waiting_answer'
    with db.connect() as c:
        if fault == 'source':
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (original['diagnostic_item_id'],))
        else:
            c.execute("UPDATE plan_constraint_decision SET metadata_json=json_set(metadata_json,'$.planning_evidence.pet_question_binding.version','forged') WHERE source_ref=?", (original['diagnostic_item_id'],))
        c.commit()
    result = DiagnosticPromptService(Database(db.path)).answer(sid, prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], answer='I am not sure', client_action_id='original-after-change')
    assert result['status'] == 'invalidated'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('cancelled', [False, True])
def test_new_start_command_resumes_only_original_prompt_or_blocks_cancelled_one(tmp_path, cancelled, listening_resources):
    case = generated(tmp_path)
    db, sid, pid, _, _ = case
    tid = command(case, child_id=CHILD)
    first = AgentCapabilityExecutor(db, StudyRepository(db)).execute(tid)
    original = first['result']['diagnostic_prompt']['prompt']
    StudyRepository(db).pause_plan_item(pid, 'pause-pet-original')
    if cancelled:
        DiagnosticPromptService(db).cancel(sid, prompt_id=original['prompt_id'], item_version=original['item_version'])
    second = command(case, child_id=CHILD)
    reopened = Database(db.path, media_root=tmp_path)
    current = AgentCapabilityExecutor(reopened, StudyRepository(reopened)).execute(second)
    if cancelled:
        assert current['result']['status'] == 'blocked'
        assert state(db, pid)['status'] == 'paused'
    else:
        assert current['result']['diagnostic_prompt']['prompt']['prompt_id'] == original['prompt_id']
        assert state(db, pid)['status'] == 'active'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        saved = json.loads(c.execute('SELECT result_json FROM agent_step WHERE plan_id=(SELECT plan_id FROM agent_plan WHERE agent_turn_id=?)', (tid,)).fetchone()[0])
        assert saved == first


def test_another_pending_question_cannot_be_replaced_to_start_the_plan(tmp_path):
    case = generated(tmp_path)
    db, sid, pid, _, original = case
    with db.connect() as c:
        other = c.execute('''SELECT diagnostic_item_id FROM diagnostic_item WHERE knowledge_node_id=?
            AND diagnostic_item_id!=? AND status='active' ''', (original['knowledge_node_id'], original['diagnostic_item_id'])).fetchone()[0]
    prompt = DiagnosticPromptService(db).prepare(sid, track='pet', preferred_item_id=other,
        client_action_id='other-pending-original')['prompt']
    tid = command(case, child_id=CHILD)
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(tid)
    assert result['result']['status'] == 'blocked'
    assert state(db, pid)['started_at'] is None
    assert DiagnosticPromptService(db).read(sid)['prompt']['prompt_id'] == prompt['prompt_id']


def test_resolver_is_read_only_and_rejects_another_session(tmp_path):
    from home_study_agent.pet_task_execution import resolve_pet_task_execution
    db, sid, pid, _, _ = generated(tmp_path)
    with db.connect() as c:
        c.execute('BEGIN')
        before = c.total_changes
        assert resolve_pet_task_execution(c, sid, pid)['status'] == 'ready'
        assert resolve_pet_task_execution(c, 'not-original-session', pid)['status'] == 'blocked'
        assert c.total_changes == before and c.in_transaction
        c.rollback()


def test_prepared_without_start_cannot_answer_and_new_command_recovers_original(tmp_path, monkeypatch):
    case = generated(tmp_path, 'reading', with_homework=True)
    db, sid, pid, _, _ = case
    tid = command(case, child_id=CHILD)
    repo = StudyRepository(db)
    def unavailable_timer(*args, **kwargs):
        raise RuntimeError('timer transaction not committed')
    monkeypatch.setattr(repo, 'start_plan_item', unavailable_timer)
    with pytest.raises(RuntimeError, match='timer transaction'):
        AgentCapabilityExecutor(db, repo).execute(tid)
    restored = DiagnosticPromptService(Database(db.path))
    pending = restored.read(sid)
    assert pending['status'] == 'awaiting_task_start'
    assert pending['reason'] == 'scheduled_pet_task_not_started'
    prompt = pending['prompt']
    from home_study_agent.diagnostic_prompt import DiagnosticPromptError
    with pytest.raises(DiagnosticPromptError, match='scheduled_pet_task_not_started'):
        LearningStateService(Database(db.path)).submit_diagnostic(sid, prompt['diagnostic_item_id'],
            'I am not sure', 'diagnostic-prompt-answer:' + prompt['prompt_id'], _trusted_prompt_id=prompt['prompt_id'])
    blocked = restored.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer='I am not sure', client_action_id='too-early-answer')
    assert blocked['status'] == 'awaiting_task_start'
    assert restored.resume(sid, prompt_id=prompt['prompt_id'])['status'] == 'awaiting_task_start'
    with db.connect() as c:
        row = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=?', (prompt['prompt_id'],)).fetchone()
        assert row['status'] == 'waiting_answer' and row['answer_json'] is None and row['answer_action_id'] is None
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
    second = command(case, child_id=CHILD)
    with db.connect() as c:
        other = c.execute("SELECT plan_item_id FROM plan_item WHERE plan_item_id!=? AND source_type!='planned_break' LIMIT 1", (pid,)).fetchone()
        assert other is not None
        c.execute("UPDATE plan_item SET status='pending',position=-100 WHERE plan_item_id=?", (other[0],))
        c.commit()
    new_db = Database(db.path)
    started = AgentCapabilityExecutor(new_db, StudyRepository(new_db)).execute(second)
    assert started['result']['diagnostic_prompt']['status'] == 'waiting_answer'
    assert started['result']['diagnostic_prompt']['prompt']['prompt_id'] == prompt['prompt_id']
    answered = restored.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer='I am not sure', client_action_id='answer-after-real-start')
    assert answered['status'] == 'answered'
    assert answered['attempt']['created_at'] >= state(db, pid)['started_at']
    assert state(db, other[0])['started_at'] is None
    assert StudyRepository(db).complete_plan_item(pid, 1, 'complete-original-after-answer')['status'] == 'completed'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='started'", (pid,)).fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['source_changed', 'plan_replaced', 'other_active'])
def test_unstarted_recovery_conflict_does_not_change_any_task(tmp_path, monkeypatch, fault):
    from home_study_agent.task_start_binding import TaskStartBindingError
    case = generated(tmp_path, 'reading', with_homework=True)
    db, sid, pid, _, original = case
    tid = command(case, child_id=CHILD)
    repo = StudyRepository(db)
    def failed_timer(*args, **kwargs):
        raise RuntimeError('timer unavailable')
    monkeypatch.setattr(repo, 'start_plan_item', failed_timer)
    with pytest.raises(RuntimeError):
        AgentCapabilityExecutor(db, repo).execute(tid)
    second = command(case, child_id=CHILD)
    with db.connect() as c:
        if fault == 'source_changed':
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (original['diagnostic_item_id'],))
        elif fault == 'plan_replaced':
            c.execute("UPDATE daily_plan SET status='superseded' WHERE plan_id=(SELECT plan_id FROM plan_item WHERE plan_item_id=?)", (pid,))
        else:
            other = c.execute("SELECT plan_item_id FROM plan_item WHERE plan_item_id!=? AND source_type!='planned_break' LIMIT 1", (pid,)).fetchone()[0]
            c.execute("UPDATE plan_item SET status='active' WHERE plan_item_id=?", (other,))
        before = [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY plan_item_id')]
        c.commit()
    with pytest.raises(TaskStartBindingError, match='TASK_START_PET_RECOVERY_'):
        AgentCapabilityExecutor(Database(db.path), StudyRepository(Database(db.path))).execute(second)
    with db.connect() as c:
        assert [tuple(row) for row in c.execute('SELECT * FROM plan_item ORDER BY plan_item_id')] == before
        assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='started'").fetchone()[0] == 0


def test_actual_http_unstarted_prompt_read_answer_resume_are_recoverable_not_score(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_learning_runtime_http import NoCloud
    case = generated(tmp_path, 'reading')
    db, sid, pid, _, _ = case
    tid = command(case, child_id=CHILD)
    repo = StudyRepository(db)
    def failed_timer(*args, **kwargs):
        raise RuntimeError('timer unavailable')
    monkeypatch.setattr(repo, 'start_plan_item', failed_timer)
    with pytest.raises(RuntimeError):
        AgentCapabilityExecutor(db, repo).execute(tid)
    app = create_app(Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False), model_gateway=NoCloud())
    with TestClient(app) as client:
        read = client.get(f'/v1/sessions/{sid}/diagnostic-prompt')
        assert read.status_code == 200, read.text
        assert read.json()['status'] == 'awaiting_task_start'
        assert read.json()['plan_item_id'] == pid
        prompt = read.json()['prompt']
        binding = {'prompt_id': prompt['prompt_id'], 'item_version': prompt['item_version']}
        answered = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={**binding,
            'answer': 'I am not sure', 'client_action_id': 'http-before-start'})
        assert answered.status_code == 200, answered.text
        assert answered.json()['status'] == 'awaiting_task_start'
        resumed = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/resume', json=binding)
        assert resumed.status_code == 200 and resumed.json()['status'] == 'awaiting_task_start'
        second = command(case, child_id=CHILD)
        response = client.post(f'/v1/agent-turns/{second}/execute')
        assert response.status_code == 200, response.text
        current = response.json()['execution']['result']['diagnostic_prompt']
        assert current['status'] == 'waiting_answer' and current['prompt']['prompt_id'] == prompt['prompt_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert c.execute('SELECT COUNT(*) FROM diagnostic_prompt WHERE answer_action_id IS NOT NULL').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['standalone', 'other_prompt', 'node_version', 'plan_binding'])
def test_completion_requires_this_prompt_and_current_exact_plan_source(tmp_path, fault):
    case = generated(tmp_path, 'reading')
    db, sid, pid, node, original = case
    tid = command(case, child_id=CHILD)
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(tid)
    prompt = result['result']['diagnostic_prompt']['prompt']
    prompts = DiagnosticPromptService(db)
    if fault == 'standalone':
        LearningStateService(db).submit_diagnostic(sid, original['diagnostic_item_id'],
            'I am not sure', 'standalone-after-start')
    else:
        if fault == 'other_prompt':
            prompts.cancel(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'])
            prompt = prompts.prepare(sid, track='pet', preferred_item_id=original['diagnostic_item_id'],
                                     client_action_id='different-prompt-after-start')['prompt']
        result = prompts.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
            answer='I am not sure', client_action_id='exact-prompt-answer')
        assert result['status'] == 'answered'
    with db.connect() as c:
        if fault == 'node_version':
            c.execute("UPDATE knowledge_node SET knowledge_version='revised-source' WHERE knowledge_node_id=?", (node,))
        elif fault == 'plan_binding':
            c.execute("UPDATE plan_constraint_decision SET metadata_json=json_remove(metadata_json,'$.planning_evidence.pet_question_binding') WHERE source_ref=?", (original['diagnostic_item_id'],))
        c.commit()
    with pytest.raises(PlanInputError):
        StudyRepository(Database(db.path)).complete_plan_item(pid, 1, 'not-the-bound-execution')
    assert state(db, pid)['status'] == 'active'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE plan_item_id=? AND event_type='completed'", (pid,)).fetchone()[0] == 0


@pytest.mark.parametrize('skill', ['reading', 'speaking', 'listening'])
def test_exact_prompt_practice_can_complete_without_claiming_skill(tmp_path, skill, listening_resources):
    case = generated(tmp_path, skill)
    db, sid, pid, _, _ = case
    tid = command(case, child_id=CHILD)
    result = AgentCapabilityExecutor(db, StudyRepository(db)).execute(tid)
    prompt = result['result']['diagnostic_prompt']['prompt']
    answered = DiagnosticPromptService(db).answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer='I am not sure', client_action_id='practice-not-qualification')
    assert answered['status'] == 'answered'
    before = answered['attempt']
    reopened = Database(db.path)
    completed = StudyRepository(reopened).complete_plan_item(pid, 1, 'complete-bound-practice')
    assert completed['status'] == 'completed'
    assert StudyRepository(reopened).complete_plan_item(pid, 1, 'complete-bound-practice') == completed
    with reopened.connect() as c:
        assert LearningStateService._attempt(c, before['attempt_id']) == before
        evidence = c.execute('SELECT independent,confidence FROM learning_evidence WHERE evidence_id=?', (before['evidence_id'],)).fetchone()
        if skill in {'listening', 'speaking'}:
            assert evidence['independent'] == 0 and evidence['confidence'] <= .35
