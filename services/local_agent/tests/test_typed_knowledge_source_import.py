"""Typed authoring documents must not be truncated into invalid excerpts."""
import hashlib
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService, InvalidKnowledgeSourceError
from home_study_agent.repository import StudyRepository


CHILD = 'child-junior-3'


def source():
    return {'schema': 'reviewed_open_response_source_v1', 'subject': '物理', 'grade': '初三',
        'knowledge_node_id': 'node-j3-physics-experiment-reasoning', 'answer_type': 'open_response',
        'assessment_mode': 'open_response_rubric', 'task_family': 'scientific_explanation',
        'response_mode': 'written', 'prompt': '说明题目中的条件并设计检验方案。',
        'task_points': ['证据与推理'+str(index)+'。'*260 for index in range(5)],
        'context_requirement': 'self_contained'}


def setup(tmp_path):
    db = Database(tmp_path / 'typed-import.db')
    db.initialize()
    StudyRepository(db).seed_family()
    values = dict(source_type='school_material', title='Isolated typed source', subject='物理',
        grade_band='初三', license_status='family_owned_index', parent_reviewed=True,
        version_label='isolated-1', source_ref='isolated:typed-author')
    return db, values


@pytest.mark.parametrize('indent', [None, 2])
def test_typed_source_complete_hash_and_idempotence_survive_restart(tmp_path, indent):
    db, values = setup(tmp_path)
    text = json.dumps(source(), ensure_ascii=False, indent=indent)
    assert len(text) > 1000
    result = KnowledgeBaseService(db).import_source(CHILD, {**values, 'content_text': text})
    with Database(db.path).connect() as c:
        rows = c.execute('SELECT content_text,content_hash FROM knowledge_excerpt WHERE source_id=?', (result['source_id'],)).fetchall()
        assert len(rows) == 1
        assert rows[0]['content_text'] == text
        assert rows[0]['content_hash'] == hashlib.sha256(text.encode()).hexdigest()
        assert json.loads(rows[0]['content_text']) == source()
    repeated = KnowledgeBaseService(Database(db.path)).import_source(CHILD, {**values, 'content_text': text})
    assert repeated['source_id'] == result['source_id'] and repeated['excerpt_count'] == 1


@pytest.mark.parametrize('fault', ['extra_field', 'oversized', 'duplicate_key'])
def test_invalid_recognized_typed_source_is_rejected_before_any_write(tmp_path, fault):
    db, values = setup(tmp_path)
    data = source()
    if fault == 'extra_field': data['automatic_certification'] = True
    elif fault == 'oversized': data['prompt'] = 'x' * 17000
    text = json.dumps(data, ensure_ascii=False)
    if fault == 'duplicate_key': text = text[:-1]+',"prompt":"replacement"}'
    with pytest.raises(InvalidKnowledgeSourceError):
        KnowledgeBaseService(db).import_source(CHILD, {**values, 'content_text': text})
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM knowledge_excerpt').fetchone()[0] == 0


def test_ndjson_keeps_each_validated_question_whole(tmp_path):
    db, values = setup(tmp_path)
    texts = [json.dumps({**source(), 'prompt': prompt}, ensure_ascii=False)
             for prompt in ('说明方案并核对条件。', '解释方案的适用边界。')]
    result = KnowledgeBaseService(db).import_source(CHILD, {**values, 'content_text': '\n'.join(texts)})
    with db.connect() as c:
        rows = c.execute('SELECT content_text,content_hash FROM knowledge_excerpt WHERE source_id=? ORDER BY sequence', (result['source_id'],)).fetchall()
        assert [row['content_text'] for row in rows] == texts
        assert [row['content_hash'] for row in rows] == [hashlib.sha256(text.encode()).hexdigest() for text in texts]


def test_ndjson_invalid_second_question_does_not_partially_save_first(tmp_path):
    db, values = setup(tmp_path)
    text = '\n'.join(json.dumps(row, ensure_ascii=False) for row in (source(), {**source(), 'extra': True}))
    with pytest.raises(InvalidKnowledgeSourceError):
        KnowledgeBaseService(db).import_source(CHILD, {**values, 'content_text': text})
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0] == 0
