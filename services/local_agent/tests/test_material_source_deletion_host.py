"""Exact single-source deletion through the local parent API, synthetic data only."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from privacy_restore_operation import restore_operation
from synthetic_native_restore_request import KEY, signed_restore_post
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.local_supply import LocalSupplyError, validate_item
from home_study_agent.repository import StudyRepository
from home_study_agent.repository import utc_now
from home_study_agent.writing_response_origin import WritingOriginalService
from home_study_agent.writing_question_authorization import authorized_question_projection
from test_material_question_authorization import published_material, grant_arguments
from test_open_response_materials import CHILD
from test_companion_activity_host import NoCloud

OTHER = 'child-primary-4'
ANSWER = '我先控制材料和横截面积，保持温度相同，再比较长度与电阻，不能把题给记录当作我做过实验。'


@pytest.fixture
def material_family(tmp_path):
    db, authority, item, binding, text, privacy = published_material(tmp_path)
    _, args = grant_arguments(authority, item)
    authority.grant(CHILD, **args)
    sid = StudyRepository(db).start_session(CHILD, 'material-delete-start')['session_id']
    prompts = DiagnosticPromptService(db)
    prompt = prompts.prepare(sid, track='school', subject='物理', preferred_item_id=item, client_action_id='material-delete-prompt')['prompt']
    objective, start_turn, plan, step = [uuid4().hex for _ in range(4)]
    criteria = ['Explain the given conditions independently']
    now = utc_now()
    with db.connect() as c:
        binding_goal = dict(objective_id=objective, child_id=CHILD, session_id=sid,
            prompt_id=prompt['prompt_id'], item_version=prompt['item_version'], diagnostic_item_id=item,
            phase='diagnose', success_criteria=criteria, created_at=now, version='diagnostic_objective_binding_v1',
            local_only=True, cloud_transmission_allowed=False)
        c.execute("INSERT INTO agent_objective VALUES(?,?,?,'learning','Isolated material source goal',?,'diagnose','active',?,?,NULL,NULL)",
            (objective, CHILD, sid, json.dumps(criteria), now, now))
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,action_json,
            orchestrator_version,created_at,client_action_id) VALUES(?,?,?,'Begin isolated material question','diagnostic.start','',
            '{"type":"start_diagnostic","local_only":true,"cloud_transmission_allowed":false}','fixture',?,?)""",
            (start_turn, CHILD, sid, now, start_turn))
        c.execute("INSERT INTO agent_plan VALUES(?,?,?,?,?,'fixture','{}',?,'completed',?,?)",
            (plan, objective, CHILD, sid, start_turn, json.dumps({'local_learning_binding':binding_goal}), now, now))
        c.execute("INSERT INTO agent_step VALUES(?,?,0,'diagnostic.start','1','server','low','{}','[]','completed',?,NULL,NULL,?,?)", (step, plan, step, now, now))
        c.commit()
    original = WritingOriginalService(db).capture_native_response(sid, prompt_id=prompt['prompt_id'],
        item_version=prompt['item_version'], text=ANSWER, client_action_id='material-delete-original')
    answered = prompts.answer(sid, prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        answer=ANSWER, writing_original_artifact_id=original['artifact_id'], client_action_id='material-delete-answer')
    other_source = KnowledgeBaseService(db).import_source(OTHER, dict(source_type='school_material',
        title='Other child separate copy', subject='物理', grade_band='四年级', source_ref='isolated:material',
        license_status='family_owned_index', parent_reviewed=True, content_text=text))
    with db.connect() as c:
        candidate = c.execute('SELECT candidate_id FROM specialized_supply_candidate WHERE diagnostic_item_id=?', (item,)).fetchone()[0]
        package = c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (candidate,)).fetchone()[0]
        question_source = json.loads(package)['source']['source_id']
    return dict(db=db, item=item, binding=binding, text=text, sid=sid, prompt=prompt,
        original=original, attempt=answered['attempt'], question_source=question_source,
        candidate=candidate, other_source=other_source['source_id'], objective=objective)


def settings_for(f, *, native_launch_attestation_key=None):
    return Settings(data_dir=f['db'].path.parent, database_path=f['db'].path, maintenance_enabled=False,
        session_token='material-source-delete-local', data_encryption_key=b'M'*32,
        native_launch_attestation_key=native_launch_attestation_key)


def headers(client, settings, restart=False):
    local = {'X-HSC-Session':settings.session_token}
    result = client.post('/v1/parent/'+('unlock' if restart else 'setup'), headers=local, json={'pin':'7391'})
    assert result.status_code == 200, result.text
    return {**local, 'X-HSC-Parent':result.json()['parent_token']}


def delete(client, h, f, *, child=CHILD, source=None, key='delete-only-material'):
    source = source or f['binding']['source_id']
    return client.post(f'/v1/parent/children/{child}/knowledge-sources/{source}/delete', headers=h,
        json={'confirmation':'确认删除', 'client_action_id':key})


def retained(c, f):
    return {table: [tuple(r) for r in c.execute(sql, args)] for table, sql, args in (
        ('question', 'SELECT * FROM knowledge_source WHERE source_id=?', (f['question_source'],)),
        ('question_excerpt', 'SELECT * FROM knowledge_excerpt WHERE source_id=?', (f['question_source'],)),
        ('other', 'SELECT * FROM knowledge_source WHERE child_id=?', (OTHER,)),
        ('other_excerpt', 'SELECT ke.* FROM knowledge_excerpt ke JOIN knowledge_source ks USING(source_id) WHERE ks.child_id=?', (OTHER,)),
        ('original', 'SELECT * FROM capture_artifact WHERE artifact_id=?', (f['original']['artifact_id'],)),
    )}


def assert_erased(c, f, before):
    assert retained(c, f) == before
    assert not c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?', (f['binding']['source_id'],)).fetchone()
    assert not c.execute('SELECT 1 FROM knowledge_excerpt WHERE source_id=?', (f['binding']['source_id'],)).fetchone()
    assert c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE attempt_id=?', (f['attempt']['attempt_id'],)).fetchone()[0] == ANSWER
    assert c.execute('SELECT status,item_json FROM diagnostic_prompt WHERE prompt_id=?', (f['prompt']['prompt_id'],)).fetchone()[:] == ('invalidated', '{}')
    assert c.execute('SELECT state,payload_json,source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (f['candidate'],)).fetchone()[:] == ('withdrawn', '{}', '{}')
    assert not c.execute('SELECT 1 FROM curriculum_supply_event WHERE candidate_id=?', (f['candidate'],)).fetchone()
    assert not c.execute('SELECT 1 FROM writing_question_authorization_event WHERE diagnostic_item_id=?', (f['item'],)).fetchone()
    assert c.execute('SELECT status,stimulus_text FROM diagnostic_item WHERE diagnostic_item_id=?', (f['item'],)).fetchone()[:] == ('retired', '')
    with pytest.raises(LocalSupplyError): validate_item(c, CHILD, f['item'])
    with pytest.raises(ValueError): authorized_question_projection(c, child_id=CHILD, diagnostic_item_id=f['item'])
    assert not c.execute('PRAGMA foreign_key_check').fetchall()
    assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


def reviewed_original(f, *, prepare_only=False):
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
    from home_study_agent.writing_rubric import prepare_writing_review
    db = f['db']
    service = RubricEvidenceService(db)
    service.initialize()
    candidate = service.diagnostic_candidate_input(CHILD, f['attempt']['evidence_id'])
    evaluator = LearningOutcomeEvaluator(db, StudyRepository(db), None, SimpleNamespace(model_provider='unused', model_name='unused'))
    turn, plan, step = [uuid4().hex for _ in range(3)]
    objective = f['objective']
    now = utc_now()
    with db.connect() as c:
        c.execute("""INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
            action_json,orchestrator_version,created_at,client_action_id)
            VALUES(?,?,?,?,'learning.evaluate_response','','{"local_only":true,"cloud_transmission_allowed":false}','test',?,?)""",
            (turn, CHILD, f['sid'], ANSWER, now, turn))
        c.execute("INSERT INTO agent_plan VALUES(?,?,?,?,?,'test','{}','{}','planned',?,?)", (plan, objective, CHILD, f['sid'], turn, now, now))
        c.execute("INSERT INTO agent_step VALUES(?,?,0,'learning.evaluate_response','1','local_model','medium',?,'[]','planned',?,NULL,NULL,?,?)",
            (step, plan, json.dumps(candidate['arguments']), step, now, now))
        c.commit()
    if prepare_only:
        return evaluator, turn
    assessment = evaluator.prepare_local_evaluation(turn)['evaluation']['rubric_assessment']
    with db.connect() as c: binding = prepare_writing_review(c, CHILD, assessment['assessment_id'])
    body = dict(expected_revision=0, client_action_id='local-material-human-review', reviewer_id='parent', reviewer_role='parent',
        operation='approve', reason='Isolated manually reviewed reference; not automatic calibration', independent=True, hint_level=0,
        review_profile='school_open_written_v1',
        dimensions=[dict(dimension_id=d['dimension_id'], score=3, response_quotes=[ANSWER], rationale='Checked exact original',
            **({'source_quotes':[f['text']]} if d['dimension_id']=='evidence' else {})) for d in assessment['dimensions']],
        criterion_evidence=[dict(criterion=criterion, response_quote=ANSWER) for criterion in assessment['criteria']],
        writing_evidence=dict(binding_version=binding['binding_version'],
            task_point_evidence=[dict(task_point=point, response_quote=ANSWER) for point in binding['task_points']]))
    service.review(CHILD, assessment['assessment_id'], **body)
    assert service.qualification(CHILD, assessment['assessment_id'])['eligible_for_mastery_gate']
    return service, assessment


def test_single_material_delete_preserves_question_answer_other_child_and_restart(material_family):
    f = material_family
    settings = settings_for(f)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42000), raise_server_exceptions=False) as client:
        h = headers(client, settings)
        assert delete(client, {'X-HSC-Session':settings.session_token}, f).status_code == 401
        assert delete(client, h, f, child=OTHER, key='wrong-child').status_code == 404
        with f['db'].connect() as c: before = retained(c, f)
        export = client.post(f'/v1/parent/children/{CHILD}/export', headers=h)
        assert export.status_code == 200, export.text
        export_path = Path(export.json()['path'])
        assert f['text'] in export_path.read_text()
        backup = client.post('/v1/parent/backups', headers=h)
        assert backup.status_code == 200, backup.text
        result = delete(client, h, f)
        assert result.status_code == 200 and result.json()['status'] == 'completed', result.text
        receipt = result.json()
        assert receipt['target_type'] == 'knowledge_source' and receipt['source_id'] == f['binding']['source_id']
        assert not export_path.exists()
        assert receipt['recovery_rotation']['status'] == 'completed'
        with f['db'].connect() as c: assert_erased(c, f, before)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42001), raise_server_exceptions=False) as client:
        h = headers(client, settings, True)
        again = delete(client, h, f)
        assert again.status_code == 200 and again.json()['deletion_id'] == receipt['deletion_id'], again.text
        wrong = delete(client, h, f, source=f['question_source'])
        assert wrong.status_code == 409, wrong.text
        with f['db'].connect() as c: assert_erased(c, f, before)


def test_source_closure_is_exact_before_http_replay_hooks(material_family):
    from home_study_agent.privacy_native_sources import purge_native_sources
    f = material_family
    with f['db'].connect() as c:
        before = retained(c, f)
        c.execute('BEGIN IMMEDIATE')
        affected = {}
        purge_native_sources(c, CHILD, owner_source_ids={f['binding']['source_id']}, affected_scopes=affected)
        assert CHILD in affected
        assert_erased(c, f, before)
        c.rollback()


def test_single_material_delete_rolls_back_then_retries_same_job(material_family, monkeypatch):
    f = material_family
    settings = settings_for(f)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42002), raise_server_exceptions=False) as client:
        h = headers(client, settings)
        with f['db'].connect() as c:
            before = {t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in (
                'knowledge_source', 'knowledge_excerpt', 'curriculum_supply_package', 'curriculum_supply_event',
                'diagnostic_item', 'diagnostic_attempt', 'diagnostic_prompt', 'writing_question_authorization_event')}
        def fail(*args, **kwargs):
            assert kwargs['connection'].in_transaction
            raise sqlite3.OperationalError('isolated late replay failure')
        with monkeypatch.context() as patch:
            patch.setattr(client.app.state.privacy_lifecycle, 'replay_source_mastery', fail)
            failed = delete(client, h, f)
        assert failed.status_code == 500, failed.text
        assert failed.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE', failed.text
        with f['db'].connect() as c:
            for table, rows in before.items(): assert [tuple(r) for r in c.execute('SELECT * FROM '+table)] == rows
            job = dict(c.execute("SELECT * FROM deletion_job WHERE client_action_id='delete-only-material'").fetchone())
            assert job['status'] == 'failed'
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42003), raise_server_exceptions=False) as client:
        result = delete(client, headers(client, settings, True), f)
        assert result.status_code == 200 and result.json()['status'] == 'completed', result.text
        assert result.json()['deletion_id'] == job['deletion_id']


def test_material_delete_revokes_real_local_review_and_preserves_answer(material_family):
    f = material_family
    service, assessment = reviewed_original(f)
    settings = settings_for(f)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42004), raise_server_exceptions=False) as client:
        h = headers(client, settings)
        with f['db'].connect() as c: before = retained(c, f)
        result = delete(client, h, f)
        assert result.status_code == 200 and result.json()['status'] == 'completed', result.text
        with f['db'].connect() as c:
            assert_erased(c, f, before)
            assert not c.execute('SELECT 1 FROM rubric_assessment WHERE assessment_id=?', (assessment['assessment_id'],)).fetchone()
            assert not c.execute('SELECT 1 FROM rubric_review_event WHERE assessment_id=?', (assessment['assessment_id'],)).fetchone()
            from home_study_agent.learning_obligations import withdrawn_evidence_ids
            assert f['attempt']['evidence_id'] in withdrawn_evidence_ids(c, CHILD)


def test_processing_evaluation_defers_delete_rejects_late_result_and_retries(material_family, monkeypatch):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from home_study_agent.learning_outcome_evaluator import LearningEvaluationUnknownError
    f = material_family
    evaluator, turn = reviewed_original(f, prepare_only=True)
    evaluator.settings.model_timeout_seconds = 60
    entered, release = threading.Event(), threading.Event()
    async def late_result(record):
        entered.set()
        assert await asyncio.to_thread(release.wait, 30)
        # Inject only the timing of a reply, never a grant or score. Real claim
        # and real persist/unknown handling stay in the production evaluator.
        return evaluator._persist(record, {}, None)
    monkeypatch.setattr(evaluator, '_evaluate_claimed', late_result)
    settings = settings_for(f)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42005), raise_server_exceptions=False) as client:
        h = headers(client, settings)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, evaluator.evaluate(turn))
            try:
                if not entered.wait(5): future.result(timeout=1)
                assert entered.is_set()
                result = delete(client, h, f)
                assert result.status_code == 409 and result.json()['code'] == 'DELETION_AWAITING_EVALUATION', result.text
                with f['db'].connect() as c:
                    assert c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?', (f['binding']['source_id'],)).fetchone()
                    assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 0
            finally:
                release.set()
            with pytest.raises(LearningEvaluationUnknownError): future.result(timeout=10)
        with f['db'].connect() as c:
            assert c.execute('SELECT state,evaluation_json,result_json FROM learning_evaluation_execution').fetchone()[:] == ('unknown', None, None)
            assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 0
            before = retained(c, f)
        completed = delete(client, h, f)
        assert completed.status_code == 200 and completed.json()['status'] == 'completed', completed.text
        with f['db'].connect() as c: assert_erased(c, f, before)


def test_post_deletion_backup_restore_does_not_restore_material_or_grant(material_family):
    f = material_family
    settings = settings_for(f, native_launch_attestation_key=KEY)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42006), raise_server_exceptions=False) as client:
        h = headers(client, settings)
        with f['db'].connect() as c: before = retained(c, f)
        result = delete(client, h, f)
        assert result.status_code == 200, result.text
        states = ('check_in', 'classroom_recall', 'homework_capture', 'plan_review', 'study', 'reflection')
        for current, target in zip(states, states[1:]):
            if current == 'classroom_recall':
                assert client.post(f"/v1/sessions/{f['sid']}/recalls/none", headers=h, json={'client_action_id':'no-recalls'}).status_code == 200
            if current == 'homework_capture':
                assert client.post(f"/v1/sessions/{f['sid']}/homework/none", headers=h, json={'client_action_id':'no-homework'}).status_code == 200
            transitioned = client.post(f"/v1/sessions/{f['sid']}/transition", headers=h,
                json=dict(expected_state=current, target_state=target, reason='Finish isolated restore fixture', client_action_id='finish-'+target))
            assert transitioned.status_code == 200, transitioned.text
        finish = client.post(f"/v1/sessions/{f['sid']}/reflection/complete", headers=h,
            json={'skipped':True, 'client_action_id':'finish-isolated-material'})
        assert finish.status_code == 200, finish.text
        backup = client.post('/v1/parent/backups', headers=h)
        assert backup.status_code == 200, backup.text
        restored = signed_restore_post(client, headers=h,
            json={'backup_id':backup.json()['backup_id'], 'confirmation':'确认恢复',
                  'operation':restore_operation(client,headers=h)})
        assert restored.status_code == 200 and restored.json()['status'] == 'restored', restored.text
        with f['db'].connect() as c: assert_erased(c, f, before)
        h = headers(client, settings, True)
        replay = delete(client, h, f)
        assert replay.status_code == 200 and replay.json()['deletion_id'] == result.json()['deletion_id'], replay.text


def test_one_shared_material_erases_both_dependent_items_not_question_source(tmp_path):
    from test_open_response_materials import material_case
    db, supply, spec, source, binding, _ = material_case(tmp_path)
    second = {**source, 'prompt':'根据同一组导体材料，说明还需要哪些对照条件才能解释电阻差异，写出理由。'}
    imported = KnowledgeBaseService(db).import_source(CHILD, dict(source_type='school_material', title='Two original tasks sharing one given',
        subject='物理', grade_band='初三', version_label='isolated-open-2026', source_ref='isolated:shared-tasks',
        license_status='family_owned_index', parent_reviewed=True,
        content_text='\n'.join(json.dumps(value, ensure_ascii=False) for value in (source, second))))
    supply.select_curriculum(CHILD, dict(grade='初三', subject='物理', edition='isolated-open-2026', semester='上学期',
        standard_scope='第四学段', textbook_source_id=imported['source_id']), actor='parent', action_id='shared-selection', expected_version=2)
    with db.connect() as c:
        excerpts = [r[0] for r in c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (imported['source_id'],))]
    items = []
    for index, excerpt in enumerate(excerpts):
        draft = supply.generate_draft(CHILD, {**spec, 'excerpt_id':excerpt}, author='author', action_id='draft-'+str(index))
        cid = draft['candidate_id']
        supply.validate(CHILD, cid, actor='validator', action_id='validate-'+str(index), expected_version=1)
        supply.review(CHILD, cid, actor='parent', decision='approve', note='Original text material reviewed', action_id='review-'+str(index), expected_version=2)
        result = supply.publish(CHILD, cid, actor='parent', action_id='publish-'+str(index), expected_version=3)
        assert result['eligible']
        items.append(result['diagnostic_item_id'])
    assert len(items) == 2
    f = {'db':db, 'binding':binding}
    settings = settings_for(f)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42007), raise_server_exceptions=False) as client:
        h = headers(client, settings)
        with db.connect() as c:
            for item in items: validate_item(c, CHILD, item)
            original = [tuple(r) for r in c.execute('SELECT * FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (imported['source_id'],))]
        result = delete(client, h, f)
        assert result.status_code == 200 and result.json()['status'] == 'completed', result.text
        with db.connect() as c:
            assert c.execute('SELECT 1 FROM knowledge_source WHERE source_id=?', (imported['source_id'],)).fetchone()
            assert [tuple(r) for r in c.execute('SELECT * FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (imported['source_id'],))] == original
            for item in items:
                assert c.execute('SELECT status,stimulus_text FROM diagnostic_item WHERE diagnostic_item_id=?', (item,)).fetchone()[:] == ('retired', '')


@pytest.mark.parametrize('fault', ['export_disappears', 'sqlite_trigger', 'export_symlink'])
def test_single_source_storage_failure_is_durable_and_original_key_retries(material_family, monkeypatch, fault):
    f = material_family
    settings = settings_for(f)
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 42008), raise_server_exceptions=False) as client:
        h = headers(client, settings)
        with f['db'].connect() as c:
            before = {t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in (
                'knowledge_source', 'knowledge_excerpt', 'curriculum_supply_package', 'curriculum_supply_event',
                'diagnostic_item', 'diagnostic_prompt', 'writing_question_authorization_event')}
        if fault.startswith('export'):
            exported = client.post(f'/v1/parent/children/{CHILD}/export', headers=h)
            assert exported.status_code == 200, exported.text
            path = Path(exported.json()['path'])
        if fault == 'sqlite_trigger':
            with f['db'].connect() as c:
                c.executescript("""CREATE TRIGGER deny_material_delete BEFORE DELETE ON knowledge_excerpt
                    WHEN OLD.source_id IN (SELECT target_id FROM deletion_job WHERE target_type='knowledge_source')
                    BEGIN SELECT RAISE(ABORT,'isolated source delete failure'); END;""")
        if fault == 'export_symlink':
            other_export = client.post(f'/v1/parent/children/{OTHER}/export', headers=h)
            assert other_export.status_code == 200, other_export.text
            target = Path(other_export.json()['path'])
            preserved = target.read_bytes()
            path.unlink()
            path.symlink_to(target)
        with monkeypatch.context() as patch:
            if fault == 'export_disappears':
                stat = Path.stat
                def vanish(value, *args, **kwargs):
                    if value == path:
                        path.unlink(missing_ok=True)
                        raise FileNotFoundError('isolated concurrent export cleanup')
                    return stat(value, *args, **kwargs)
                patch.setattr(Path, 'stat', vanish)
            failed = delete(client, h, f)
        assert failed.status_code == 500 and failed.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE', failed.text
        with f['db'].connect() as c:
            for table, rows in before.items(): assert [tuple(r) for r in c.execute('SELECT * FROM '+table)] == rows
            job = dict(c.execute("SELECT * FROM deletion_job WHERE client_action_id='delete-only-material'").fetchone())
            assert job['status'] == 'failed'
            if fault == 'sqlite_trigger': c.execute('DROP TRIGGER deny_material_delete')
            c.commit()
        if fault == 'export_symlink':
            assert target.read_bytes() == preserved
            path.unlink()
        retried = delete(client, h, f)
        assert retried.status_code == 200 and retried.json()['status'] == 'completed', retried.text
        assert retried.json()['deletion_id'] == job['deletion_id']
