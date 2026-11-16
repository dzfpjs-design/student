"""Real pre71 source tables -> immutable publication/grant -> current host.

Reuse the repository's pre71 SCHEMA fixture, not frozen production modules.
Only source/standard input data is synthesized. Services create all material
bindings, package proofs and permission events before migration, once.
"""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService, _version
from home_study_agent.knowledge_base import source_lines
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import LocalSupplyError, digest, score_local_item, validate_item
from home_study_agent.open_response_materials import bind_material_excerpt, resolve_open_response_materials, resolve_source_materials
from home_study_agent.open_response_source import MATERIAL_SCHEMA, VALIDATOR, parse
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.stage_source_review import stage_review_context
from home_study_agent.writing_question_authorization import WritingQuestionAuthorizationService, _source_scope
from test_knowledge_source_revision_migration import old_database
from test_material_question_authorization import grant_arguments
from test_open_response_supply_http import CHILD, publish_open
from test_pet_reviewed_source_actual_http import NoCloud
from test_writing_question_authorization_http import parent


def _original_source(c, subject, name, text):
    """Pre71 import rows, with current typed syntax and full excerpt validation."""
    content, lines = source_lines(text)
    assert lines == [content]
    c.execute('''INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,
        grade_band,version_label,source_ref,license_status,review_status,content_hash,imported_at)
        VALUES (?,?,'school_material',?,?,'初三','material-v70',?,'public','approved',?,?)''',
        (name, CHILD, name, subject, 'isolated:' + name,
         hashlib.sha256(content.encode()).hexdigest(), '2026-09-01T12:00:00+00:00'))
    c.execute('INSERT INTO knowledge_excerpt VALUES (?,?,1,NULL,?,?)',
        (name + '-excerpt', name, content, hashlib.sha256(content.encode()).hexdigest()))


def legacy_material(tmp_path, subject='数学', *, publish=True):
    db = old_database(tmp_path)
    LearningStateService(db).seed_catalog()
    math = subject == '数学'
    text = '本题给定 a=1、b=2、c=5，讨论实数范围的解。' if math else '两导体材料、横截面积和温度相同，长度分别为一米和两米。'
    quote = '分析任务中的条件并用可追溯的推理过程解释结论及其适用边界。'
    with db.connect() as c:
        assert 'current_version' not in {r['name'] for r in c.execute('PRAGMA table_info(knowledge_source)')}
        _original_source(c, subject, 'material', text)
        c.commit()
        binding = bind_material_excerpt(c, child_id=CHILD, excerpt_id='material-excerpt', material_key='given')
        assert binding['source_version'] == digest(dict(c.execute("SELECT * FROM knowledge_source WHERE source_id='material'").fetchone()))
        source = dict(schema=MATERIAL_SCHEMA, subject=subject, grade='初三',
            knowledge_node_id='node-j3-math-discriminant' if math else 'node-j3-physics-experiment-reasoning',
            answer_type='open_response', assessment_mode='open_response_rubric',
            task_family='mathematical_process' if math else 'scientific_explanation', response_mode='written',
            prompt='说明 x²+2x+5=0 的实数解情况，给出判别依据与推理过程。' if math else
                   '设计检验导体长度与电阻关系的实验，说明控制变量和推论边界。',
            task_points=['说明给定条件和所用关系', '以推理过程支持结论并说明适用边界'],
            context_requirement='external_material', material_semantics='text_only', material_bindings=[binding])
        assert parse(json.dumps(source, ensure_ascii=False)) == source
        _original_source(c, subject, 'question', json.dumps(source, ensure_ascii=False))
        c.execute('''INSERT INTO curriculum_standard_source(source_id,source_key,jurisdiction,source_type,
            subject_title,title,version_label,official_url,local_root_path,status,imported_at,updated_at)
            VALUES ('material-standard','material-standard','CN','national_standard',?,'Synthetic standard',
            '2022','isolated:standard','','structured','2026-09-01','2026-09-01')''', (subject,))
        c.execute("INSERT INTO curriculum_standard_page VALUES('material-standard',12,?,'fixture-hash',20)", (quote,))
        c.execute('''INSERT INTO curriculum_standard_requirement(requirement_id,source_id,subject_title,stage_scope,
            requirement_type,title,requirement_text,observable_outcome,page_start,page_end,confidence,maturity,created_at,updated_at)
            VALUES('material-requirement','material-standard',?,'第四学段','content_requirement',?,?,'Traceable reasoning',
            12,12,.9,'verified','2026-09-01','2026-09-01')''', (subject, quote, quote))
        c.commit()
    supply = CurriculumSupplyService(db)
    supply.select_curriculum(CHILD, dict(grade='初三', subject=subject, edition='material-v70', semester='上学期',
        standard_scope='第四学段', textbook_source_id='question'), actor='parent', action_id='scope', expected_version=0)
    spec = dict(seed_item_id='diag-j3-discriminant-1' if math else 'diag-j3-physics-experiment-1', stage='parallel',
        excerpt_id='question-excerpt', requirement_id='material-requirement', standard_page=12, standard_quote=quote,
        validator=VALIDATOR, explanation='只审核原始任务和参考要求，不认证孩子掌握。', difficulty=2)
    publication = publish_open(supply, spec) if publish else supply.generate_draft(CHILD, spec, author='author', action_id='draft')
    return db, supply, publication, source


def history(c):
    tables = ['curriculum_supply_package', 'curriculum_supply_event']
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='writing_question_authorization_event'").fetchone():
        tables.append('writing_question_authorization_event')
    return {table: [dict(r) for r in c.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}


@pytest.mark.parametrize('subject', ['数学', '物理'])
def test_pre71_material_and_purpose_survive_actual_initialize_without_rewriting(tmp_path, subject):
    db, supply, item, source = legacy_material(tmp_path, subject)
    cid, item_id = item['candidate_id'], item['diagnostic_item_id']
    with db.connect() as c:
        before = history(c)
        assert not {'current_version', 'predecessor_id'} & json.loads(before['curriculum_supply_package'][0]['source_snapshot_json'])['source'].keys()
        scope = _source_scope(c, CHILD, item_id)
        version = _version(DiagnosticPromptService._item(c, CHILD, item_id))
        view = resolve_open_response_materials(c, child_id=CHILD, diagnostic_item_id=item_id, expected_item_version=version)
        assert view['status'] == 'ready'
        assert score_local_item(validate_item(c, CHILD, item_id), 'yes') is None
    db.initialize()
    Database(db.path).initialize()
    assert CurriculumSupplyService(Database(db.path)).get(CHILD, cid)['eligible']
    with db.connect() as c:
        current = dict(c.execute("SELECT * FROM knowledge_source WHERE source_id='material'").fetchone())
        assert (current['current_version'], current['predecessor_id']) == (1, None)
        assert digest(current) != source['material_bindings'][0]['source_version']
        assert history(c) == before
        assert _source_scope(c, CHILD, item_id) == scope
        assert resolve_open_response_materials(c, child_id=CHILD, diagnostic_item_id=item_id, expected_item_version=version) == view
        assert not view['cloud_transmission_allowed'] and not view['mastery_claim_allowed']
        with pytest.raises(LocalSupplyError, match='VERSION_CHANGED'):
            resolve_source_materials(c, child_id=CHILD, source=source)
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('fault', ['current_version', 'predecessor_id', 'title', 'license_status', 'child_id'])
@pytest.mark.parametrize('kind', ['legacy', 'fresh'])
def test_legacy_compatibility_and_fresh_hash_reject_changed_material(tmp_path, fault, kind):
    db, _, item, source = legacy_material(tmp_path)
    db.initialize()
    with db.connect() as c:
        candidate = item['candidate_id'] if kind == 'legacy' else None
        if kind == 'fresh':
            source['material_bindings'] = [bind_material_excerpt(c, child_id=CHILD, excerpt_id='material-excerpt', material_key='given')]
        before = resolve_source_materials(c, child_id=CHILD, source=source, candidate_id=candidate)
        assert before['materials']
        if kind == 'fresh':
            assert before['materials'][0]['source_basis']['current_version'] == 1
            assert before['materials'][0]['source_basis']['predecessor_id'] is None
        frozen = history(c)
        value = {'current_version': 0, 'predecessor_id': 'actual-prior-source', 'title': 'changed',
                 'license_status': 'unknown', 'child_id': 'child-primary-4'}[fault]
        c.execute(f'UPDATE knowledge_source SET {fault}=? WHERE source_id=?', (value, 'material'))
        c.commit()
        with pytest.raises(LocalSupplyError):
            resolve_source_materials(c, child_id=CHILD, source=source, candidate_id=candidate)
        assert history(c) == frozen
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('state', ['draft', 'validated'])
def test_pre71_material_pending_review_finishes_with_original_proof(tmp_path, state):
    db, supply, draft, _ = legacy_material(tmp_path, publish=False)
    cid = draft['candidate_id']
    if state == 'validated':
        supply.validate(CHILD, cid, actor='checker', action_id='check', expected_version=1)
    with db.connect() as c:
        original = c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[0]
        context = stage_review_context(c, CHILD, cid) if state == 'validated' else None
    db.initialize()
    if state == 'draft':
        assert supply.validate(CHILD, cid, actor='checker', action_id='check', expected_version=1)['state'] == 'validated'
    else:
        with db.connect() as c:
            assert stage_review_context(c, CHILD, cid) == context
    assert supply.review(CHILD, cid, decision='approve', actor='parent', note='Checked whole given material', action_id='review', expected_version=2)['state'] == 'reviewed'
    assert supply.publish(CHILD, cid, actor='parent', action_id='publish', expected_version=3)['eligible']
    with db.connect() as c:
        assert c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[0] == original


@pytest.mark.parametrize('subject', ['数学', '物理'])
def test_pre71_material_grant_actual_http_upgrade_restart_replay_and_revoke(tmp_path, subject):
    db, _, item, _ = legacy_material(tmp_path, subject)
    item_id = item['diagnostic_item_id']
    privacy = PrivacyLifecycleService(db, tmp_path, data_encryption_key=b'M' * 32)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    service = WritingQuestionAuthorizationService(db)
    service.initialize()
    view, args = grant_arguments(service, item_id)
    receipt = service.grant(CHILD, **args)['receipt']
    body = {key: value for key, value in args.items() if key not in {'actor_id', 'diagnostic_item_id'}}
    with db.connect() as c:
        assert 'current_version' not in {r['name'] for r in c.execute('PRAGMA table_info(knowledge_source)')}
        before = history(c)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False, data_encryption_key=b'M' * 32)
    url = f'/v1/parent/children/{CHILD}/writing-question-authorizations/{item_id}'
    for restart in range(2):
        with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
            assert client.get(url).status_code == 401
            headers = parent(client, restart == 0)
            current = client.get(url, headers=headers)
            assert current.status_code == 200 and current.json()['status'] == 'authorized', current.text
            for field in ('source_version', 'item_version', 'curriculum_version', 'preview'):
                assert current.json()[field] == view[field]
            replay = client.post(url + '/grant', headers=headers, json=body)
            assert replay.status_code == 200 and replay.json()['receipt'] == receipt, replay.text
            with db.connect() as c:
                assert history(c) == before
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
                assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
            if restart:
                revoked = client.post(url + '/revoke', headers=headers, json=dict(expected_revision=receipt['revision'], client_action_id='revoke-original-grant'))
                assert revoked.status_code == 200 and revoked.json()['current']['status'] == 'revoked'
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        headers = parent(client)
        assert client.get(url, headers=headers).json()['status'] == 'revoked'
        replay = client.post(url + '/grant', headers=headers, json=body)
        assert replay.status_code == 200 and replay.json()['receipt'] == receipt
        assert replay.json()['current']['status'] == 'revoked'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM writing_question_authorization_event').fetchone()[0] == 2
            assert history(c)['curriculum_supply_package'] == before['curriculum_supply_package']
            assert history(c)['curriculum_supply_event'] == before['curriculum_supply_event']
