"""Exact local authoring imports; synthetic sources never certify a child."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService, InvalidKnowledgeSourceError
from home_study_agent.repository import StudyRepository


CHILD = 'child-primary-4'
OTHER = 'child-junior-3'
KINDS = ('writing', 'listening', 'objective')


def packet(kind, number=1):
    if kind == 'writing':
        from test_writing_source_publication import source_packet
        value = source_packet()
        value['prompt'] = f"Task {number}. " + value['prompt']
        value['task_points'] += [
            'For this synthetic local practice, describe the event in your own words, '
            'explain how you felt and provide a clear invitation with a time and place. ' * 2]
        value['task_points'] += ['Organize the message so the reader can identify the event, '
                                 'your opinion and the invitation without guessing. ' * 2]
        return value
    if kind == 'listening':
        from test_pet_reviewed_source_contract import packet as listening
        return listening(prompt=f'Task {number}. Where should Mira wait?',
            stimulus_text='This is synthetic local practice, not an official recording. ' * 18
                          + 'Mira, please wait at the red bridge.',
            instruction='Listen twice and answer the question.', required_play_count=2)
    return {'schema': 'curriculum_objective_source_v1', 'subject': '英语', 'grade': '四年级',
        'knowledge_node_id': 'node-pet-reading-vocabulary', 'assessment_mode': 'objective',
        'answer_type': 'text',
        'prompt': f'Task {number}. ' + 'This synthetic practice asks for the complete room name. ' * 18
                  + "Read: The meeting is in Room 207. Which room is the meeting in?",
        'expected_answer': {'local_rule': 'local_exact_v1', 'exact_answers': ['Room 207']}}


def encode(value, **kwargs):
    return json.dumps(value, ensure_ascii=False, **kwargs)


def values(text, **changes):
    return dict(source_type='school_material', title='Synthetic local typed original, not official PET',
        subject='英语', grade_band='四年级', version_label='isolated-c03-v1',
        source_ref='isolated:c03-author', license_status='family_owned_index',
        parent_reviewed=True, content_text=text, **changes)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / 'typed-original.db')
    database.initialize()
    StudyRepository(database).seed_family()
    return database


def rows(database, source_id):
    with database.connect() as c:
        return [dict(row) for row in c.execute(
            'SELECT * FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (source_id,))]


def counts(database):
    with database.connect() as c:
        return tuple(c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                     for table in ('knowledge_source', 'knowledge_excerpt', 'audit_event'))


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('layout', ['pretty', 'long', 'ndjson'])
def test_real_import_readback_preserves_complete_typed_source(db, kind, layout):
    originals = [packet(kind)] + ([packet(kind, 2)] if layout == 'ndjson' else [])
    texts = [encode(value, indent=2 if layout == 'pretty' else None) for value in originals]
    assert all(1000 < len(text) < 16000 for text in texts)
    text = '\n'.join(texts)
    imported = KnowledgeBaseService(db).import_source(CHILD, values(text))
    reopened = Database(db.path)
    read = rows(reopened, imported['source_id'])
    assert [row['content_text'] for row in read] == texts
    assert [json.loads(row['content_text']) for row in read] == originals
    assert [row['sequence'] for row in read] == list(range(1, len(texts) + 1))
    assert [row['content_hash'] for row in read] == [hashlib.sha256(t.encode()).hexdigest() for t in texts]
    assert imported['content_hash'] == hashlib.sha256(text.encode()).hexdigest()
    before = counts(db)
    replay = KnowledgeBaseService(reopened).import_source(CHILD, values(text))
    assert replay['source_id'] == imported['source_id'] and counts(db) == before
    assert KnowledgeBaseService(reopened).list_sources(OTHER) == []


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('fault', ['extra', 'oversized', 'duplicate', 'duplicate_schema',
    'malformed', 'mixed_malformed', 'mixed_plain', 'mixed_unknown', 'mixed_invalid', 'too_many'])
def test_invalid_typed_input_is_atomic_and_never_legacy_text(db, kind, fault):
    original = packet(kind)
    text = encode(original)
    if fault == 'extra':
        text = encode({**original, 'cloud_allowed': True})
    elif fault == 'oversized':
        text = encode({**original, 'prompt': 'x' * 17000})
    elif fault == 'duplicate':
        text = text[:-1] + ',"prompt":"replacement"}'
    elif fault == 'duplicate_schema':
        text = text[:-1] + ',"schema":"unrecognized"}'
    elif fault == 'malformed':
        text = encode(original, indent=2)[:-1]
    elif fault == 'mixed_malformed':
        text += '\n' + encode(packet(kind, 2))[:-1]
    elif fault == 'mixed_plain':
        text += '\nA chapter index is not another complete authoring question.'
    elif fault == 'mixed_unknown':
        text += '\n' + encode({'schema': 'unrecognized', 'prompt': 'Do not import partially.'})
    elif fault == 'mixed_invalid':
        text += '\n' + encode({**packet(kind, 2), 'prompt': ''})
    elif fault == 'too_many':
        text = '\n'.join(encode(packet(kind, n)) for n in range(501))
    before = counts(db)
    with pytest.raises(InvalidKnowledgeSourceError):
        KnowledgeBaseService(db).import_source(CHILD, values(text))
    assert counts(db) == before


def test_import_does_not_upgrade_unknown_rights_or_original_private_source(db):
    data = values(encode(packet('writing')))
    data.update(license_status='unknown')
    imported = KnowledgeBaseService(db).import_source(CHILD, data)
    assert imported['license_status'] == 'unknown' and imported['review_status'] == 'quarantined'
    data.update(license_status='public', parent_reviewed=True)
    replay = KnowledgeBaseService(db).import_source(CHILD, data)
    assert replay['source_id'] == imported['source_id']
    assert replay['license_status'] == 'unknown' and replay['review_status'] == 'quarantined'


def test_plain_text_retains_complete_lines_and_hashes_the_stored_text(db):
    text = 'A' * 1100 + '\nSecond index line'
    imported = KnowledgeBaseService(db).import_source(CHILD, values(text))
    read = rows(db, imported['source_id'])
    assert [row['content_text'] for row in read] == ['A' * 1100, 'Second index line']
    assert all(row['content_hash'] == hashlib.sha256(row['content_text'].encode()).hexdigest() for row in read)


def test_mixed_supported_ndjson_preserves_each_original_schema_without_permissions(db):
    originals = [packet(kind) for kind in KINDS]
    texts = [encode(value) for value in originals]
    imported = KnowledgeBaseService(db).import_source(CHILD, values('\n'.join(texts)))
    assert [row['content_text'] for row in rows(Database(db.path), imported['source_id'])] == texts
    assert imported['source_ref'] == 'isolated:c03-author'
    assert imported['license_status'] == 'family_owned_index'
    assert imported['excerpt_count'] == 3


def test_second_excerpt_write_failure_rolls_back_whole_source_and_retry_is_exact(db):
    texts = [encode(packet('writing', n)) for n in (1, 2)]
    content = '\n'.join(texts)
    with db.connect() as c:
        c.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON knowledge_excerpt
            WHEN NEW.sequence=2 BEGIN SELECT RAISE(ABORT,'synthetic second row failure'); END""")
        c.commit()
    before = counts(db)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic second row'):
        KnowledgeBaseService(db).import_source(CHILD, values(content))
    assert counts(Database(db.path)) == before
    with db.connect() as c:
        c.execute('DROP TRIGGER reject_second')
        c.commit()
    imported = KnowledgeBaseService(Database(db.path)).import_source(CHILD, values(content))
    assert [row['content_text'] for row in rows(db, imported['source_id'])] == texts


def add_standard(database):
    # Isolated reviewed standard fixture, never an official/production attestation.
    quote = '理解原题要求，核对信息与完整原答，区分听读理解和书面表达。'
    standard_root = database.path.parent.resolve(strict=True) / 'c03-standards'
    standard_root.mkdir()
    (standard_root / 'manifest.json').write_text(json.dumps({
        'format': 'hsc_official_curriculum_sources_v1',
        'sources': [{'source_key': 'c03-synthetic', 'pdf_filename': None, 'pages_filename': None}],
    }), encoding='utf-8')
    with database.connect() as c:
        c.execute('''INSERT INTO curriculum_standard_source(
            source_id,source_key,jurisdiction,source_type,subject_title,title,version_label,
            official_url,local_root_path,status,imported_at,updated_at)
            VALUES('c03-std','c03-synthetic','CN','national_standard','英语','Synthetic standard fixture','2026',
            'isolated:c03-standard',?,'structured','2026-09-01','2026-09-01')''', (str(standard_root),))
        c.execute('INSERT INTO curriculum_standard_page VALUES(?,?,?,?,?)',
                  ('c03-std', 12, quote, hashlib.sha256(quote.encode()).hexdigest(), len(quote)))
        c.execute('''INSERT INTO curriculum_standard_requirement(
            requirement_id,source_id,subject_title,stage_scope,requirement_type,title,
            requirement_text,observable_outcome,page_start,page_end,confidence,maturity,created_at,updated_at)
            VALUES('c03-req','c03-std','英语','第二学段','content_requirement',?,?,
            'Original response to task',12,12,.9,'verified','2026-09-01','2026-09-01')''', (quote, quote))
        c.commit()


@pytest.mark.parametrize('kind', KINDS)
def test_actual_parent_http_import_to_exact_published_prompt_restart_and_withdraw(db, tmp_path, kind):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.local_supply import validate_item, LocalSupplyError
    from test_pet_reviewed_source_actual_http import NoCloud

    add_standard(db)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        session_token='isolated-c03-typed-import')
    local = {'X-HSC-Session': settings.session_token}
    base = f'/api/parent/children/{CHILD}/curriculum-supply'
    import_path = f'/v1/parent/children/{CHILD}/knowledge-sources'
    originals = [packet(kind)] + ([packet(kind, 2)] if kind == 'listening' else [])
    texts = [encode(value, indent=2 if kind == 'writing' else None) for value in originals]
    payload = values('\n'.join(texts))
    validator = dict(writing='pet_writing_rubric_source_v1', listening='pet_listening_exact_source_v1',
                     objective='curriculum_objective_exact_source_v1')[kind]

    def post(client, path, body):
        response = client.post(path, json=body)
        assert response.status_code == 200, response.text[:800]
        return response.json()

    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41400), headers=local) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/setup', {'pin': '7391'})['parent_token']
        assert client.post(import_path, json=payload, headers={'X-HSC-Parent': 'invalid'}).status_code == 401
        imported = post(client, import_path, payload)
        source_id = imported['source_id']
        assert post(client, import_path, payload)['source_id'] == source_id
        detail = client.get(base + f'/sources/{source_id}/options')
        assert detail.status_code == 200, detail.text[:800]
        options = detail.json()
        assert options['local_only'] and not options['cloud_transmission_allowed']
        assert [row['text'] for row in options['excerpts']] == texts
        assert client.get(base.replace(CHILD, OTHER) + f'/sources/{source_id}/options').status_code == 404
        seed = next(row for row in options['seeds'] if row['validator'] == validator)
        standard = next(row for row in options['standards'] if row['requirement_id'] == 'c03-req')
        assert standard['available']
        selected = post(client, base + '/select', {'action_id': 'select', 'expected_version': 0, 'scope': {
            'grade': '四年级', 'subject': '英语', 'edition': payload['version_label'], 'semester': '上学期',
            'standard_scope': standard['stage_scope'], 'textbook_source_id': source_id}})
        frozen = []
        for index, (excerpt, original) in enumerate(zip(options['excerpts'], originals)):
            request = {'action_id': f'generate-{index}', 'expected_version': 0, 'expected_scope_version': selected['version'],
                'source': {'seed_item_id': seed['seed_item_id'], 'stage': 'parallel', 'excerpt_id': excerpt['excerpt_id'],
                    'requirement_id': standard['requirement_id'], 'standard_page': standard['page_number'],
                    'standard_quote': standard['quote'], 'validator': validator, 'difficulty': 1,
                    'explanation': 'Synthetic source review: compare the full original task and its original requirements.'}}
            generated = post(client, base + '/generate', request)
            assert generated['state'] == 'draft', generated['blockers']
            cid = generated['candidate_id']
            assert post(client, base + '/generate', request)['candidate_id'] == cid
            for version, operation in enumerate(('validate', 'review', 'publish'), 1):
                body = {'expected_version': version, 'action_id': f'{operation}-{index}'}
                if operation == 'review':
                    body.update(decision='approve', note='Checked original task/source; no child skill claim')
                published = post(client, base + f'/{cid}/{operation}', body)
                assert published['state'] == dict(validate='validated', review='reviewed', publish='published')[operation]
            assert published['eligible']
            with db.connect() as c:
                item = validate_item(c, CHILD, published['diagnostic_item_id'])
                assert item['prompt'] == original['prompt']
                rubric = json.loads(item['expected_answer_json'])
                if kind == 'writing':
                    assert item['assessment_mode'] == 'pet_reference_rubric' and 'exact_answers' not in rubric
                    assert rubric['task_points'] == original['task_points']
                elif kind == 'listening':
                    assert item['stimulus_text'] == original['stimulus_text']
                    assert published['payload']['pet_listening']['required_play_count'] == 2
                else:
                    assert item['assessment_mode'] == 'objective' and rubric['exact_answers'] == ['room 207']
                assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            frozen.append((cid, published['diagnostic_item_id']))
        response = client.post('/v1/sessions', json={'child_id': CHILD, 'client_action_id': 'c03-start-session'})
        assert response.status_code == 201, response.text[:800]
        sid = response.json()['session_id']
        prompt = post(client, f'/v1/sessions/{sid}/diagnostic-prompt/prepare',
            {'track': 'pet', 'preferred_item_id': frozen[0][1], 'client_action_id': 'exact-original'})['prompt']
        assert prompt['diagnostic_item_id'] == frozen[0][1]
        assert prompt['item']['prompt'] == originals[0]['prompt']

    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 41401), headers=local) as client:
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin': '7391'})['parent_token']
        for index, (cid, item_id) in enumerate(frozen):
            restored = client.get(base + f'/{cid}').json()
            assert restored['eligible'] and restored['diagnostic_item_id'] == item_id
            replay = post(client, base + f'/{cid}/publish', {'expected_version': 3, 'action_id': f'publish-{index}'})
            assert replay['diagnostic_item_id'] == item_id
            post(client, base + f'/{cid}/withdraw', {'expected_version': 4, 'action_id': f'withdraw-{index}', 'note': 'Withdraw synthetic original'})
            with db.connect() as c, pytest.raises(LocalSupplyError):
                validate_item(c, CHILD, item_id)
        assert [row['content_text'] for row in rows(db, source_id)] == texts
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 0
            assert not c.execute('PRAGMA foreign_key_check').fetchall()
