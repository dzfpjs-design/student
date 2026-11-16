"""Local reviewed material publication; no model or learner qualification."""
import hashlib
import json

import pytest

from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService, _version
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.local_supply import LocalSupplyError, digest, score_local_item, validate_item
from test_open_response_supply_http import CHILD, prepared, publish_open, item_rows


def material_case(tmp_path, subject='物理', *, semantics='text_only', owner=CHILD,
                  question_license='family_owned_index', material_license='family_owned_index', material_count=1):
    db, service, spec, source, _ = prepared(tmp_path, subject)
    kb = KnowledgeBaseService(db)
    text = '导体甲与乙的材料和横截面积相同，长度分别为一米和两米，实验在相同温度下进行。'
    if subject == '数学':
        text = '二次函数的系数满足 a=1、b=2、c=5；本题仅讨论实数范围内函数与横轴的交点。'
    material = kb.import_source(owner, dict(source_type='school_material', title='Isolated task data',
        subject=subject, grade_band='初三', version_label='material-1', source_ref='isolated:material',
        license_status=material_license, parent_reviewed=True, content_text=text))
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM knowledge_source WHERE source_id=?', (material['source_id'],)).fetchone())
        excerpt = dict(c.execute('SELECT * FROM knowledge_excerpt WHERE source_id=?', (material['source_id'],)).fetchone())
    binding = dict(material_key='given', source_id=material['source_id'], source_version=digest(row),
        excerpt_id=excerpt['excerpt_id'], excerpt_hash=excerpt['content_hash'],
        span_start=0, span_end=len(text), text_hash=hashlib.sha256(text.encode()).hexdigest())
    source.update(schema='reviewed_open_response_source_v2', context_requirement='external_material',
        material_semantics=semantics, material_bindings=[binding])
    if material_count == 2:
        from home_study_agent.open_response_materials import bind_material_excerpt
        second = kb.import_source(owner, dict(source_type='school_material', title='Isolated second given condition',
            subject=subject, grade_band='初三', version_label='material-2', source_ref='isolated:second-material',
            license_status=material_license, parent_reviewed=True,
            content_text='甲的电阻为十欧姆，乙的电阻为二十欧姆。记录由题目给定，不是孩子实际实验测量。'))
        with db.connect() as c:
            second_excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?', (second['source_id'],)).fetchone()[0]
            source['material_bindings'].append(bind_material_excerpt(c, child_id=owner, excerpt_id=second_excerpt, material_key='measurement'))
    question = kb.import_source(CHILD, dict(source_type='school_material', title='Isolated material question',
        subject=subject, grade_band='初三', version_label='isolated-open-2026', source_ref='isolated:material-question',
        license_status=question_license, parent_reviewed=True,
        content_text=json.dumps(source, ensure_ascii=False, indent=2 if material_count == 2 else None)))
    with db.connect() as c:
        spec['excerpt_id'] = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?', (question['source_id'],)).fetchone()[0]
    service.select_curriculum(CHILD, dict(grade='初三', subject=subject, edition='isolated-open-2026', semester='上学期',
        standard_scope='第四学段', textbook_source_id=question['source_id']), actor='parent', action_id='material-scope', expected_version=1)
    return db, service, spec, source, binding, text


@pytest.mark.parametrize('subject', ['数学', '物理'])
def test_local_material_publishes_exact_source_and_survives_restart(tmp_path, subject):
    db, service, spec, source, binding, text = material_case(tmp_path, subject)
    published = publish_open(service, spec)
    assert published['eligible']
    from home_study_agent.open_response_materials import bind_material_excerpt, resolve_open_response_materials
    with db.connect() as c:
        item = validate_item(c, CHILD, published['diagnostic_item_id'])
        assert text in item['stimulus_text'] and not item['stimulus_asset']
        assert json.loads(item['expected_answer_json'])['material_bindings'] == [binding]
        assert score_local_item(item, text) is None
        frozen = _version(DiagnosticPromptService._item(c, CHILD, item['diagnostic_item_id']))
        assert bind_material_excerpt(c, child_id=CHILD, excerpt_id=binding['excerpt_id'], material_key='given') == binding
        view = resolve_open_response_materials(c, child_id=CHILD, diagnostic_item_id=item['diagnostic_item_id'], expected_item_version=frozen)
        assert view['status'] == 'ready' and view['materials'][0]['text'] == text
        assert view['local_only'] and not view['cloud_transmission_allowed'] and not view['mastery_claim_allowed']
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
        proof = json.loads(c.execute('SELECT source_snapshot_json FROM curriculum_supply_package WHERE candidate_id=?', (published['candidate_id'],)).fetchone()[0])
        assert proof['open_response_materials']['materials'][0]['binding'] == binding
    reopened = CurriculumSupplyService(Database(db.path)).get(CHILD, published['candidate_id'])
    assert reopened['eligible']
    with Database(db.path).connect() as c:
        restored = resolve_open_response_materials(c, child_id=CHILD, diagnostic_item_id=item['diagnostic_item_id'], expected_item_version=frozen)
        assert restored == view
        assert resolve_open_response_materials(c, child_id=CHILD, diagnostic_item_id=item['diagnostic_item_id'], expected_item_version='old')['status'] == 'blocked'


@pytest.mark.parametrize('fault', ['text', 'hash', 'review', 'license', 'expiry', 'source_delete', 'excerpt_delete', 'tombstone', 'stimulus', 'binding'])
def test_material_change_or_withdrawal_invalidates_published_question(tmp_path, fault):
    db, service, spec, _, binding, _ = material_case(tmp_path)
    published = publish_open(service, spec)
    item_id = published['diagnostic_item_id']
    with db.connect() as c:
        if fault == 'text': c.execute("UPDATE knowledge_excerpt SET content_text=content_text||' changed' WHERE excerpt_id=?", (binding['excerpt_id'],))
        elif fault == 'hash': c.execute("UPDATE knowledge_excerpt SET content_hash='changed' WHERE excerpt_id=?", (binding['excerpt_id'],))
        elif fault == 'review': c.execute("UPDATE knowledge_source SET review_status='quarantined' WHERE source_id=?", (binding['source_id'],))
        elif fault == 'license': c.execute("UPDATE knowledge_source SET license_status='unknown' WHERE source_id=?", (binding['source_id'],))
        elif fault == 'expiry': c.execute("UPDATE knowledge_source SET effective_to='2000-01-01' WHERE source_id=?", (binding['source_id'],))
        elif fault == 'source_delete':
            c.execute('DELETE FROM knowledge_excerpt WHERE source_id=?', (binding['source_id'],))
            c.execute('DELETE FROM knowledge_source WHERE source_id=?', (binding['source_id'],))
        elif fault == 'excerpt_delete': c.execute('DELETE FROM knowledge_excerpt WHERE excerpt_id=?', (binding['excerpt_id'],))
        elif fault == 'tombstone':
            c.execute('CREATE TABLE IF NOT EXISTS privacy_source_erasure(child_id TEXT, source_id TEXT)')
            c.execute('INSERT INTO privacy_source_erasure(child_id,source_id) VALUES(?,?)', (CHILD, binding['source_id']))
        elif fault == 'stimulus': c.execute("UPDATE diagnostic_item SET stimulus_text='replacement' WHERE diagnostic_item_id=?", (item_id,))
        else:
            rubric = json.loads(c.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?', (item_id,)).fetchone()[0])
            rubric['material_bindings'][0]['span_end'] -= 1
            c.execute('UPDATE diagnostic_item SET expected_answer_json=? WHERE diagnostic_item_id=?', (json.dumps(rubric), item_id))
        c.commit()
        with pytest.raises(LocalSupplyError): validate_item(c, CHILD, item_id)
    assert not CurriculumSupplyService(Database(db.path)).get(CHILD, published['candidate_id'])['eligible']


@pytest.mark.parametrize('fault', ['image', 'cross_child', 'partial', 'duplicate', 'bad_hash', 'v1'])
def test_material_contract_cannot_borrow_or_guess_missing_content(tmp_path, fault):
    db, service, spec, source, binding, _ = material_case(tmp_path,
        semantics='image_required' if fault == 'image' else 'text_only', owner='child-primary-4' if fault == 'cross_child' else CHILD)
    if fault == 'partial': source['material_bindings'][0]['span_start'] = 1
    if fault == 'duplicate': source['material_bindings'].append(dict(binding))
    if fault == 'bad_hash': source['material_bindings'][0]['text_hash'] = '0'*64
    if fault == 'v1': source['schema'] = 'reviewed_open_response_source_v1'
    with db.connect() as c:
        before_items = item_rows(c)
        body = json.dumps(source, ensure_ascii=False)
        c.execute('UPDATE knowledge_excerpt SET content_text=?,content_hash=? WHERE excerpt_id=?',
            (body, hashlib.sha256(body.encode()).hexdigest(), spec['excerpt_id']))
        c.commit()
    try:
        result = service.generate_draft(CHILD, spec, author='author', action_id='bad-material')
    except LocalSupplyError:
        pass
    else:
        assert result['state'] == 'blocked' and result['blockers']
        if fault == 'image': assert 'OPEN_RESPONSE_IMAGE_SEMANTICS_REQUIRED' in result['blockers']
    with db.connect() as c:
        assert item_rows(c) == before_items


def test_material_publication_is_atomic_and_replays_original_item(tmp_path):
    import sqlite3
    db, service, spec, _, _, text = material_case(tmp_path)
    draft = service.generate_draft(CHILD, spec, author='author', action_id='draft')
    cid = draft['candidate_id']
    service.validate(CHILD, cid, actor='validator', action_id='validate', expected_version=1)
    service.review(CHILD, cid, decision='approve', actor='parent', note='Checked full text material', action_id='review', expected_version=2)
    with db.connect() as c:
        before_items = item_rows(c)
        c.executescript('''CREATE TRIGGER interrupted_material AFTER INSERT ON diagnostic_item
            WHEN NEW.answer_type='open_response' BEGIN SELECT RAISE(ABORT,'material interrupted'); END;''')
    with pytest.raises(sqlite3.IntegrityError, match='material interrupted'):
        service.publish(CHILD, cid, actor='parent', action_id='publish', expected_version=3)
    with db.connect() as c:
        assert c.execute('SELECT state,version FROM curriculum_supply_package WHERE candidate_id=?', (cid,)).fetchone()[:] == ('reviewed', 3)
        assert item_rows(c) == before_items
        c.execute('DROP TRIGGER interrupted_material')
        c.commit()
    restarted = CurriculumSupplyService(Database(db.path))
    original = restarted.publish(CHILD, cid, actor='parent', action_id='publish', expected_version=3)
    assert restarted.publish(CHILD, cid, actor='parent', action_id='publish', expected_version=3)['diagnostic_item_id'] == original['diagnostic_item_id']
    with db.connect() as c:
        item = validate_item(c, CHILD, original['diagnostic_item_id'])
        assert text in item['stimulus_text']
        c.execute("UPDATE child_profile SET grade='初三' WHERE child_id='child-primary-4'")
        c.commit()
        from home_study_agent.open_response_materials import resolve_open_response_materials
        version = _version(DiagnosticPromptService._item(c, CHILD, item['diagnostic_item_id']))
        other = resolve_open_response_materials(c, child_id='child-primary-4', diagnostic_item_id=item['diagnostic_item_id'], expected_item_version=version)
        assert other['status'] == 'blocked' and not other['materials'] and other['stimulus_text'] is None


def test_material_descriptor_is_readonly_and_rejects_truncated_import(tmp_path):
    from home_study_agent.open_response_materials import bind_material_excerpt
    db, _, _, _, binding, _ = material_case(tmp_path)
    with db.connect() as c:
        before = c.total_changes
        bind_material_excerpt(c, child_id=CHILD, excerpt_id=binding['excerpt_id'], material_key='given')
        assert c.total_changes == before and not c.in_transaction
        c.execute("UPDATE knowledge_excerpt SET content_text=substr(content_text,1,10) WHERE excerpt_id=?", (binding['excerpt_id'],))
        with pytest.raises(LocalSupplyError, match='TRUNCATED'):
            bind_material_excerpt(c, child_id=CHILD, excerpt_id=binding['excerpt_id'], material_key='given')
        assert c.in_transaction


def test_multiple_materials_keep_exact_order_and_expose_no_source_paths(tmp_path):
    from home_study_agent.open_response_materials import bind_material_excerpt, resolve_source_materials
    db, _, _, source, _, _ = material_case(tmp_path)
    kb = KnowledgeBaseService(db)
    second = kb.import_source(CHILD, dict(source_type='school_material', title='Second isolated measurement',
        subject='物理', grade_band='初三', version_label='material-2', source_ref='/private/isolated-material.txt',
        license_status='family_owned_index', parent_reviewed=True, content_text='实际记录：甲的电阻为十欧姆，乙的电阻为二十欧姆。'))
    with db.connect() as c:
        excerpt = c.execute('SELECT excerpt_id FROM knowledge_excerpt WHERE source_id=?', (second['source_id'],)).fetchone()[0]
        binding = bind_material_excerpt(c, child_id=CHILD, excerpt_id=excerpt, material_key='measurement')
        source['material_bindings'].append(binding)
        before = c.total_changes
        resolved = resolve_source_materials(c, child_id=CHILD, source=source)
        assert c.total_changes == before and not c.in_transaction
        assert [m['material_key'] for m in resolved['materials']] == ['given', 'measurement']
        assert resolved['stimulus_text'].index('[given]') < resolved['stimulus_text'].index('[measurement]')
        assert '/private/' not in json.dumps(resolved) and 'source_ref' not in json.dumps(resolved)
        source['material_bindings'].reverse()
        reversed_material = resolve_source_materials(c, child_id=CHILD, source=source)
        assert reversed_material['binding_version'] != resolved['binding_version']
        assert reversed_material['stimulus_text'].index('[measurement]') < reversed_material['stimulus_text'].index('[given]')


@pytest.mark.parametrize('fault', ['v1_extra', 'unknown', 'boolean_offset', 'path', 'self_contained'])
def test_http_material_dto_remains_strict(tmp_path, fault):
    from pydantic import ValidationError
    from home_study_agent.curriculum_supply_http import DraftPayload
    _, _, spec, source, _, _ = material_case(tmp_path)
    if fault == 'v1_extra': source['schema'] = 'reviewed_open_response_source_v1'
    elif fault == 'unknown': source['schema'] = 'reviewed_open_response_source_v3'
    elif fault == 'boolean_offset': source['material_bindings'][0]['span_start'] = False
    elif fault == 'path': source['material_bindings'][0]['path'] = '/private/not-a-material'
    else: source['context_requirement'] = 'self_contained'
    with pytest.raises(ValidationError):
        DraftPayload.model_validate({**spec, 'prompt':source['prompt'], 'expected_answer':None, 'open_response':source})


@pytest.mark.parametrize('fault', ['partial', 'hash', 'duplicate'])
def test_material_parse_preserves_typed_source_error_contract(tmp_path, fault):
    from home_study_agent.open_response_source import parse, OpenResponseSourceError
    _, _, _, source, binding, _ = material_case(tmp_path)
    if fault == 'partial': source['material_bindings'][0]['span_start'] = 1
    elif fault == 'hash': source['material_bindings'][0]['text_hash'] = 'invalid'
    else: source['material_bindings'].append(dict(binding))
    with pytest.raises(OpenResponseSourceError): parse(json.dumps(source))


@pytest.mark.parametrize('subject,material_count', [('数学', 1), ('物理', 1), ('物理', 2)])
def test_real_parent_router_material_review_publish_restart_and_withdraw(tmp_path, subject, material_count):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_pet_reviewed_source_actual_http import NoCloud
    db, _, spec, source, binding, text = material_case(tmp_path, subject, material_count=material_count)
    with db.connect() as c:
        raw = c.execute('SELECT content_text,content_hash FROM knowledge_excerpt WHERE excerpt_id=?', (spec['excerpt_id'],)).fetchone()
        assert json.loads(raw['content_text']) == source
        assert hashlib.sha256(raw['content_text'].encode()).hexdigest() == raw['content_hash']
        if material_count == 2: assert len(raw['content_text']) > 1000 and '\n' in raw['content_text']
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False, session_token='isolated-material-session')
    base = '/api/parent/children/' + CHILD + '/curriculum-supply'
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41900),
                    headers={'X-HSC-Session':settings.session_token}) as client:
        assert client.post(base+'/generate', json={}).status_code == 401
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/setup', json={'pin':'7391'}).json()['parent_token']
        body = dict(action_id='http-material-draft', expected_version=0, expected_scope_version=2, source=spec)
        result = client.post(base+'/generate', json=body)
        assert result.status_code == 200, result.text
        assert result.json()['state'] == 'draft', result.text
        cid = result.json()['candidate_id']
        assert client.post(base+'/generate', json=body).json()['candidate_id'] == cid
        duplicate = client.post(base+'/generate', json={**body, 'action_id':'http-duplicate'})
        assert duplicate.status_code == 422 and 'DUPLICATE' in duplicate.text
        for version, operation in enumerate(('validate', 'review', 'publish'), 1):
            payload = dict(action_id='http-'+operation, expected_version=version)
            if operation == 'review': payload.update(decision='approve', note='Checked exact complete original text and question')
            response = client.post(base+f'/{cid}/{operation}', json=payload)
            assert response.status_code == 200, response.text
        published = response.json()
        assert published['eligible'] and not published['cloud_transmission_allowed']
        with db.connect() as c:
            item = validate_item(c, CHILD, published['diagnostic_item_id'])
            assert text in item['stimulus_text'] and score_local_item(item, text) is None
            if material_count == 2:
                assert item['stimulus_text'].index('[given]') < item['stimulus_text'].index('[measurement]')
                assert '不是孩子实际实验测量' in item['stimulus_text']
            assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41901),
                    headers={'X-HSC-Session':settings.session_token}) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock', json={'pin':'7391'}).json()['parent_token']
        assert client.get(base+f'/{cid}').json()['eligible']
        assert client.get(base.replace(CHILD, 'child-primary-4')+f'/{cid}').status_code == 404
        with db.connect() as c:
            c.execute("UPDATE knowledge_source SET review_status='quarantined' WHERE source_id=?", (binding['source_id'],))
            c.commit()
        current = client.get(base+f'/{cid}')
        assert current.status_code == 200 and not current.json()['eligible']
