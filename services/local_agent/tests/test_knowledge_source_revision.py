"""Isolated C03: original text is not a complete stored excerpt receipt."""
import hashlib
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService, InvalidKnowledgeSourceError
from test_typed_knowledge_source_import import CHILD, setup, source


def legacy(tmp_path):
    db, values = setup(tmp_path)
    text = json.dumps(source(), ensure_ascii=False)
    original = KnowledgeBaseService(db).import_source(CHILD, {**values, 'content_text': text})
    with db.connect() as c:
        c.execute('UPDATE knowledge_excerpt SET content_text=?,content_hash=? WHERE source_id=?',
                  (text[:1000], hashlib.sha256(text[:1000].encode()).hexdigest(), original['source_id']))
        c.commit()
    return db, values, text, original


def test_duplicate_incomplete_is_not_approved_success(tmp_path):
    db, values, text, old = legacy(tmp_path)
    with pytest.raises(InvalidKnowledgeSourceError, match='KNOWLEDGE_SOURCE_REVISION_REQUIRED'):
        KnowledgeBaseService(db).import_source(CHILD, {**values, 'content_text': text})
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0] == 1
        assert c.execute('SELECT content_text FROM knowledge_excerpt').fetchone()[0] == text[:1000]


def test_revision_preserves_old_source_and_requires_separate_review(tmp_path):
    from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService
    db, values, text, old = legacy(tmp_path)
    service = KnowledgeSourceRevisionService(db)
    preview = service.preview(CHILD, old['source_id'], text)
    assert preview['status'] == 'incomplete'
    assert preview['stored_excerpts'][0]['text'] == text[:1000]
    command = dict(content_text=text, expected_version=preview['source_version'], client_action_id='revision-1')
    result = service.revise(CHILD, old['source_id'], **command)
    new = result['current']
    assert new['source_id'] != old['source_id']
    assert new['review_status'] == 'quarantined'
    assert new['content_hash'] == old['content_hash']
    assert KnowledgeBaseService(db).get_source(old['source_id'])['review_status'] == 'superseded'
    with db.connect() as c:
        assert c.execute('SELECT content_text FROM knowledge_excerpt WHERE source_id=?', (old['source_id'],)).fetchone()[0] == text[:1000]
        assert c.execute('SELECT content_text FROM knowledge_excerpt WHERE source_id=?', (new['source_id'],)).fetchone()[0] == text
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
    restarted = KnowledgeSourceRevisionService(Database(db.path))
    assert restarted.revise(CHILD, old['source_id'], **command)['receipt'] == result['receipt']
    assert KnowledgeBaseService(db).import_source(CHILD, {**values, 'content_text': text})['review_status'] == 'quarantined'


def test_preview_and_cancel_are_zero_write(tmp_path):
    from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService
    db, _, text, old = legacy(tmp_path)
    with db.connect() as c:
        before = '\n'.join(c.iterdump())
    KnowledgeSourceRevisionService(db).preview(CHILD, old['source_id'], text)
    with db.connect() as c:
        assert '\n'.join(c.iterdump()) == before


def test_conflicts_wrong_child_deleted_and_no_permission_transfer(tmp_path):
    from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService, SourceRevisionConflict
    db, _, text, old = legacy(tmp_path)
    service = KnowledgeSourceRevisionService(db)
    with pytest.raises(KeyError):
        service.preview('child-primary-4', old['source_id'], text)
    with pytest.raises(SourceRevisionConflict, match='HASH_MISMATCH'):
        service.preview(CHILD, old['source_id'], text.replace('条件', '环境'))
    preview = service.preview(CHILD, old['source_id'], text)
    command = dict(content_text=text, expected_version=preview['source_version'], client_action_id='same-key')
    result = service.revise(CHILD, old['source_id'], **command)
    with pytest.raises(SourceRevisionConflict):
        service.revise(CHILD, old['source_id'], **{**command, 'client_action_id': 'competing'})
    with pytest.raises(SourceRevisionConflict):
        service.revise(CHILD, old['source_id'], **{**command, 'expected_version': '0'*64})
    reviewed = service.review(CHILD, result['current']['source_id'],
        expected_version=result['source_version'], client_action_id='separate-review')
    assert reviewed['current']['review_status'] == 'approved'
    assert reviewed['purpose_permission_inherited'] is False
    with db.connect() as c:
        c.execute('DELETE FROM knowledge_excerpt WHERE source_id=?', (result['current']['source_id'],))
        c.execute('DELETE FROM knowledge_source WHERE source_id=?', (result['current']['source_id'],))
        c.commit()
    with pytest.raises(SourceRevisionConflict):
        service.revise(CHILD, old['source_id'], **command)


def test_failure_in_reconcile_rolls_back_all_source_and_receipt_writes(tmp_path):
    from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService
    db, _, text, old = legacy(tmp_path)
    def fail(*args, **kwargs):
        raise ValueError('isolated replay failure')
    service = KnowledgeSourceRevisionService(db, reconcile=fail)
    version = service.preview(CHILD, old['source_id'], text)['source_version']
    with db.connect() as c:
        before = '\n'.join(c.iterdump())
    with pytest.raises(ValueError, match='isolated replay failure'):
        service.revise(CHILD, old['source_id'], content_text=text, expected_version=version, client_action_id='retry')
    with db.connect() as c:
        assert '\n'.join(c.iterdump()) == before
    service.reconcile = None
    assert service.revise(CHILD, old['source_id'], content_text=text,
        expected_version=version, client_action_id='retry')['current']['review_status'] == 'quarantined'


def test_concurrent_same_request_creates_one_version(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService
    db, _, text, old = legacy(tmp_path)
    version = KnowledgeSourceRevisionService(db).preview(CHILD, old['source_id'], text)['source_version']
    def run():
        return KnowledgeSourceRevisionService(Database(db.path)).revise(CHILD, old['source_id'],
            content_text=text, expected_version=version, client_action_id='concurrent')['receipt']['event_id']
    with ThreadPoolExecutor(2) as executor:
        ids = list(executor.map(lambda _: run(), range(2)))
    assert ids[0] == ids[1]
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM knowledge_source').fetchone()[0] == 2


def test_same_hash_other_child_is_not_superseded_or_repaired(tmp_path):
    from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService
    db, values, text, old = legacy(tmp_path)
    other = KnowledgeBaseService(db).import_source('child-primary-4', {**values, 'content_text': text})
    with db.connect() as c:
        before = [dict(r) for r in c.execute('SELECT * FROM knowledge_excerpt WHERE source_id=?', (other['source_id'],))]
    service = KnowledgeSourceRevisionService(db)
    version = service.preview(CHILD, old['source_id'], text)['source_version']
    service.revise(CHILD, old['source_id'], content_text=text, expected_version=version, client_action_id='owned')
    assert KnowledgeBaseService(db).get_source(other['source_id']) == other
    with db.connect() as c:
        assert [dict(r) for r in c.execute('SELECT * FROM knowledge_excerpt WHERE source_id=?', (other['source_id'],))] == before
        assert not c.execute("SELECT 1 FROM knowledge_source_revision_event WHERE child_id='child-primary-4'").fetchone()


def test_review_cannot_approve_changed_new_excerpt_even_with_fresh_version(tmp_path):
    from home_study_agent.knowledge_source_revision import KnowledgeSourceRevisionService, SourceRevisionConflict
    db, _, text, old = legacy(tmp_path)
    service = KnowledgeSourceRevisionService(db)
    p = service.preview(CHILD, old['source_id'], text)
    result = service.revise(CHILD, old['source_id'], content_text=text, expected_version=p['source_version'], client_action_id='create')
    new_id = result['current']['source_id']
    with db.connect() as c:
        c.execute('UPDATE knowledge_excerpt SET content_text=? WHERE source_id=?', ('damaged again', new_id))
        c.commit()
    current = service.get(CHILD, new_id)
    with pytest.raises(SourceRevisionConflict, match='EXCERPTS_CHANGED'):
        service.review(CHILD, new_id, expected_version=current['source_version'], client_action_id='review-damaged')
    assert service.get(CHILD, new_id)['current']['review_status'] == 'quarantined'
