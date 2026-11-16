"""Source-specific erasure uses real reviewed audio, not invented mastery gates."""
import json
import sqlite3

import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.privacy_evidence_cleanup import purge_rubric_sources, PrivacyEvidenceCleanupError
from test_pet_audio_rubric_review import prepared
from test_pet_media_evidence import media, CHILD


def test_audio_erase_removes_both_copies_and_reconciles_without_changing_triggers(media):
    db, service, snapshot, body, _ = prepared(media)
    service.review(CHILD, snapshot['assessment_id'], **body)
    assert service.qualification(CHILD, snapshot['assessment_id'])['eligible_for_mastery_gate']
    runtime = AgentRuntimeStore(db)
    with db.connect() as c:
        before = c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name LIKE 'rubric_%' ORDER BY name").fetchall()
        copies = c.execute('SELECT payload_json,request_json FROM rubric_review_event').fetchone()
        assert all('Synthetic fixture annotation' in value for value in copies)
        c.execute('BEGIN IMMEDIATE')
        counts = purge_rubric_sources(c, CHILD, media_ids=[body['audio_evidence']['media_id']], reconcile=runtime.reconcile_learning_state)
        assert counts == {'rubric_review_event': 1, 'rubric_assessment': 1}
        assert c.in_transaction
        assert c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name LIKE 'rubric_%' ORDER BY name").fetchall() == before
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        c.commit()
    with pytest.raises(ValueError, match='RUBRIC_ASSESSMENT_NOT_OWNED'):
        service.get(CHILD, snapshot['assessment_id'])
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        c.execute('BEGIN IMMEDIATE')
        assert purge_rubric_sources(c, CHILD, media_ids=[body['audio_evidence']['media_id']], reconcile=runtime.reconcile_learning_state) == {'rubric_review_event': 0, 'rubric_assessment': 0}
        c.rollback()


@pytest.mark.parametrize('column', ['payload_json', 'request_json'])
def test_corrupt_review_copy_blocks_erasure_without_silent_partial_cleanup(media, column):
    db, service, snapshot, body, _ = prepared(media)
    service.review(CHILD, snapshot['assessment_id'], **body)
    with db.connect() as c:
        row = dict(c.execute('SELECT * FROM rubric_review_event').fetchone())
        row.update(event_id='corrupt-history', revision=2, client_action_id='corrupt-review')
        row[column] = '{broken'
        c.execute('INSERT INTO rubric_review_event (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_JSON_INVALID'):
            purge_rubric_sources(c, CHILD, media_ids=[body['audio_evidence']['media_id']], reconcile=lambda *a, **k: None)
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 2
        c.rollback()


def test_borrowed_rollback_and_wrong_child_preserve_real_review(media):
    db, service, snapshot, body, _ = prepared(media)
    service.review(CHILD, snapshot['assessment_id'], **body)
    runtime = AgentRuntimeStore(db)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_SOURCE_NOT_OWNED'):
            purge_rubric_sources(c, 'child-junior-3', media_ids=[body['audio_evidence']['media_id']], reconcile=runtime.reconcile_learning_state)
        def fail(*args, **kwargs):
            raise OSError('synthetic reconcile persistence failure')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_STORAGE_FAILURE') as failure:
            purge_rubric_sources(c, CHILD, media_ids=[body['audio_evidence']['media_id']], reconcile=fail)
        assert isinstance(failure.value.__cause__, OSError)
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 1
        purge_rubric_sources(c, CHILD, source_evidence_ids=[snapshot['source_evidence_id']], reconcile=runtime.reconcile_learning_state)
        c.rollback()
    assert service.qualification(CHILD, snapshot['assessment_id'])['eligible_for_mastery_gate']


def test_knowledge_erasure_matches_exact_frozen_item_not_similar_text(media):
    db, service, snapshot, body, prompt = prepared(media)
    service.review(CHILD, snapshot['assessment_id'], **body)
    runtime = AgentRuntimeStore(db)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert purge_rubric_sources(c, CHILD, diagnostic_item_ids=['not-this-item'], reconcile=runtime.reconcile_learning_state)['rubric_assessment'] == 0
        assert purge_rubric_sources(c, CHILD, diagnostic_item_ids=[prompt['diagnostic_item_id']], reconcile=runtime.reconcile_learning_state)['rubric_assessment'] == 1
        c.commit()


def test_uninitialized_historical_database_no_schema_side_effects():
    with sqlite3.connect(':memory:') as c:
        c.execute('BEGIN IMMEDIATE')
        assert purge_rubric_sources(c, 'child', media_ids=[], reconcile=None) == {'rubric_review_event': 0, 'rubric_assessment': 0}
        assert c.execute('SELECT name FROM sqlite_master').fetchall() == []
        assert c.in_transaction


def test_historical_missing_audio_binding_can_be_erased_only_in_explicit_child_audio_scope(media):
    db, service, snapshot, body, _ = prepared(media)
    service.review(CHILD, snapshot['assessment_id'], **body)
    runtime = AgentRuntimeStore(db)
    with db.connect() as c:
        c.execute('DELETE FROM pet_media_binding WHERE child_id=?', (CHILD,))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        assert purge_rubric_sources(c, CHILD, reconcile=runtime.reconcile_learning_state)['rubric_assessment'] == 0
        assert purge_rubric_sources(c, CHILD, all_child_audio=True, reconcile=runtime.reconcile_learning_state)['rubric_assessment'] == 1
        c.commit()
