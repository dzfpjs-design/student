"""Exact captured-page cleanup keeps another child's work and imported sources fenced."""
import pytest

from home_study_agent.database import Database
from home_study_agent.knowledge_graph.parent_textbook_capture import ParentTextbookCaptureService
from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageService, TextbookOcrPageInput
from home_study_agent.privacy_evidence_cleanup import (
    parent_textbook_sources, purge_parent_textbook_sources, PrivacyEvidenceCleanupError,
)
from home_study_agent.repository import utc_now
from test_parent_textbook_capture import setup, Adapter


@pytest.fixture
def captures(tmp_path):
    db = Database(tmp_path / 'textbook.db')
    setup(db)
    with db.connect() as c:
        now = utc_now()
        c.execute("INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('other-child','合成另孩','初三','江苏','active',?,?)", (now, now))
        c.commit()
    service = ParentTextbookCaptureService(db, Adapter())
    lease = service.acquire('child-4', 'bound-document-hash', {})
    first = service.recognize('child-4', lease['lease_id'], 'aGVsbG8=', {'title': '合成教材'}, 'first-page')
    second = service.recognize('child-4', lease['lease_id'], 'c2Vjb25k', {'title': '合成教材'}, 'second-page')
    service.release(lease['lease_id'], 'synthetic switch to other child')
    other_lease = service.acquire('other-child', 'bound-document-hash', {})
    other = service.recognize('other-child', other_lease['lease_id'], 'b3RoZXI=', {'title': '另孩合成教材'}, 'other-page')
    return db, service, first, second, other


def test_single_page_cleanup_preserves_sibling_page_lease_other_child_and_rollback(captures):
    db, service, first, second, other = captures
    with db.connect() as c:
        sources = parent_textbook_sources(c, 'child-4', source_refs=[service.page_source_ref(first['capture_ref'])])
        assert len(sources) == 1
        c.execute('BEGIN IMMEDIATE')
        counts = purge_parent_textbook_sources(c, 'child-4', artifact_ids=[sources[0]['artifact_id']])
        assert counts['kg_parent_textbook_ocr_artifact'] == 1
        assert counts['kg_parent_textbook_capture_lease'] == 0
        c.rollback()
        assert len(parent_textbook_sources(c, 'child-4')) == 2
        c.execute('BEGIN IMMEDIATE')
        counts = purge_parent_textbook_sources(c, 'child-4')
        assert counts['kg_parent_textbook_ocr_artifact'] == 2
        assert counts['kg_parent_textbook_capture_lease'] == 1
        assert len(parent_textbook_sources(c, 'other-child')) == 1
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
        c.commit()
    restarted = ParentTextbookCaptureService(Database(db.path), Adapter())
    assert restarted.pending('child-4') == []
    assert restarted.pending('other-child')[0]['capture_ref'] == other['capture_ref']


def test_imported_page_cannot_claim_erasure_by_only_removing_capture(captures):
    db, service, first, _, _ = captures
    ref = service.page_source_ref(first['capture_ref'])
    page = TextbookOcrPageService(db).import_page(TextbookOcrPageInput(title='合成已导入页', ocr_text='Synthetic retained source text', source_ref=ref))
    with db.connect() as c:
        source = parent_textbook_sources(c, 'child-4', source_refs=[ref])[0]
        assert source['resource_ids']
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_TEXTBOOK_IMPORT_CLEANUP_REQUIRED'):
            purge_parent_textbook_sources(c, 'child-4', source_refs=[ref])
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_TEXTBOOK_IMPORTED_BODY_REMAINS'):
            purge_parent_textbook_sources(c, 'child-4', source_refs=[ref], purge_imported=lambda *args: None)
        assert c.execute('SELECT 1 FROM kg_resource WHERE resource_id=?', (source['resource_ids'][0],)).fetchone()
        assert len(parent_textbook_sources(c, 'child-4')) == 2
        c.rollback()


def test_wrong_child_id_and_malformed_capture_journal_fail_closed(captures):
    db, _, _, _, _ = captures
    with db.connect() as c:
        other_id = parent_textbook_sources(c, 'other-child')[0]['artifact_id']
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_SOURCE_NOT_OWNED'):
            purge_parent_textbook_sources(c, 'child-4', artifact_ids=[other_id])
        c.execute("UPDATE kg_parent_textbook_ocr_artifact SET import_request_json='{broken' WHERE child_id='child-4'")
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_JSON_INVALID'):
            purge_parent_textbook_sources(c, 'child-4')
        assert c.execute("SELECT COUNT(*) FROM kg_parent_textbook_ocr_artifact WHERE child_id='child-4'").fetchone()[0] == 2
        c.rollback()
