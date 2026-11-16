"""Real page import and parent-approved shared language projections survive scoped erasure."""
import json
import sqlite3

import pytest

from home_study_agent.knowledge_graph.language_candidate_service import TextbookFocusCandidateService
from home_study_agent.knowledge_graph.textbook_page_service import TextbookOcrPageService, TextbookOcrPageInput
from home_study_agent.knowledge_graph.privacy_cleanup import purge_imported
from home_study_agent.privacy_evidence_cleanup import parent_textbook_sources, purge_parent_textbook_sources
from test_parent_textbook_privacy_cleanup import captures


@pytest.fixture
def pages(captures):
    db, capture, first, _, other = captures
    page_service = TextbookOcrPageService(db)
    approved = TextbookFocusCandidateService(db)
    targets = {}
    # Approve the retained child's page first; the later shared lexeme update
    # otherwise leaves the deleted child's annotation in the global projection.
    for child, artifact, text in [('other-child', other, 'Words: apple'), ('child-4', first, 'Words: apple, pear')]:
        page = page_service.import_page(TextbookOcrPageInput(title='合成教材', ocr_text=text,
            subject_scope='英语', source_ref=capture.page_source_ref(artifact['capture_ref'])))
        candidates = approved.extract(page['resource_id'])
        for candidate in candidates:
            approved.save(candidate['candidate_id'], term=candidate['term'], focus_kind='word',
                proposed={'senses':['synthetic annotation ' + child]}, status='confirmed',
                client_action_id='approve-' + candidate['candidate_id'])
        targets[child] = page['resource_id']
    with db.connect() as c:
        node = c.execute("SELECT node_id FROM kg_node WHERE canonical_key='language:english:word:pear:recognition'").fetchone()[0]
        c.execute("INSERT INTO kg_review_plan VALUES('pear-plan','child-4','active','fixture',1,1,?,'pear-plan','now','now')",
                  (json.dumps({'items':[{'knowledge_node_id':node}]}),))
        c.execute("INSERT INTO kg_mapping_review_event VALUES('private-review',?,NULL,?,'explains','decision','approved','parent',?,NULL,'private-action','now')",
                  (targets['child-4'], node, 'synthetic annotation child-4'))
        c.execute("""INSERT INTO kg_candidate_mapping_job(mapping_job_id,idempotency_key,status,source_ref,options_json,created_at,updated_at)
            VALUES('source-job','source-job','failed','synthetic','{}','now','now')""")
        c.execute("""INSERT INTO kg_candidate_mapping_failure(failure_id,mapping_job_id,resource_id,error_code,detail,status,created_at,updated_at)
            VALUES('source-failure','source-job',?,'SYNTHETIC','synthetic annotation child-4','open','now','now')""", (targets['child-4'],))
        c.execute("INSERT INTO kg_mapping_audit_pack VALUES('audit-pack','digest','fixture','open',1,'now','now')")
        c.execute("""INSERT INTO kg_mapping_audit_item(audit_item_id,audit_pack_id,resource_id,segment_id,node_id,relation_type,
            lexical_evidence,scope_match,mapping_source_ref,precision_proxy,created_at)
            VALUES('source-audit','audit-pack',?,'synthetic-segment',?,'explains','synthetic annotation child-4','same','synthetic',1,'now')""", (targets['child-4'], node))
        c.commit()
    return db, targets


def test_imported_cleanup_erases_fts_unbound_history_and_reprojects_shared_lexeme(pages):
    db, targets = pages
    with db.connect() as c:
        other_before = [tuple(r) for r in c.execute('SELECT * FROM kg_language_focus_candidate WHERE resource_id=?', (targets['other-child'],))]
        pear_id = c.execute("SELECT node_id FROM kg_node WHERE canonical_key='language:english:word:pear:recognition'").fetchone()[0]
        c.execute('BEGIN IMMEDIATE')
        sources = parent_textbook_sources(c, 'child-4')
        counts = purge_parent_textbook_sources(c, 'child-4', purge_imported=purge_imported)
        assert counts['kg_resource'] == 1 and counts['kg_segment_fts'] == 1
        assert c.in_transaction and c.execute('PRAGMA foreign_key_check').fetchall() == []
        assert c.execute('SELECT 1 FROM kg_resource WHERE resource_id=?', (targets['child-4'],)).fetchone() is None
        assert c.execute('SELECT COUNT(*) FROM kg_mapping_review_event WHERE resource_id=?', (targets['child-4'],)).fetchone()[0] == 0
        for table in ('kg_mapping_audit_item', 'kg_candidate_mapping_failure'):
            assert c.execute('SELECT COUNT(*) FROM ' + table + ' WHERE resource_id=?', (targets['child-4'],)).fetchone()[0] == 0
        assert tuple(c.execute("SELECT status,item_count FROM kg_mapping_audit_pack WHERE audit_pack_id='audit-pack'").fetchone()) == ('superseded', 0)
        assert c.execute("SELECT COUNT(*) FROM kg_segment_fts WHERE text LIKE '%pear%'").fetchone()[0] == 0
        assert [tuple(r) for r in c.execute('SELECT * FROM kg_language_focus_candidate WHERE resource_id=?', (targets['other-child'],))] == other_before
        apple = c.execute("SELECT senses_json FROM kg_language_lexeme WHERE normalized='apple'").fetchone()[0]
        assert json.loads(apple) == ['synthetic annotation other-child']
        assert c.execute("SELECT COUNT(*) FROM kg_language_lexeme WHERE normalized='pear'").fetchone()[0] == 0
        assert tuple(c.execute("SELECT status,review_status FROM kg_node WHERE canonical_key='language:english:word:apple:recognition'").fetchone()) == ('active','approved')
        pear = c.execute('SELECT * FROM kg_node WHERE node_id=?', (pear_id,)).fetchone()
        assert (pear['status'],pear['review_status']) == ('conflicted','unreviewed')
        assert pear['canonical_key'].startswith('privacy-deleted:') and pear['title'] == '来源已删除' and pear['description'] == ''
        assert c.execute("SELECT status FROM kg_review_plan WHERE review_plan_id='pear-plan'").fetchone()[0] == 'superseded'
        c.commit()
    with db.connect() as c:
        assert len(parent_textbook_sources(c, 'other-child')) == 1
        assert not parent_textbook_sources(c, 'child-4')
        assert c.execute("SELECT senses_json FROM kg_language_lexeme WHERE normalized='apple'").fetchone()[0] == apple


def test_failure_mid_purge_rolls_back_source_history_fts_and_approval(pages):
    from home_study_agent.privacy_evidence_cleanup import PrivacyEvidenceCleanupError
    db, targets = pages
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_synthetic_kg_delete BEFORE DELETE ON kg_resource BEGIN SELECT RAISE(ABORT,'synthetic purge failure'); END")
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_STORAGE_FAILURE') as failure:
            purge_parent_textbook_sources(c, 'child-4', purge_imported=purge_imported)
        assert isinstance(failure.value.__cause__, sqlite3.IntegrityError)
        assert c.execute('SELECT 1 FROM kg_resource WHERE resource_id=?', (targets['child-4'],)).fetchone()
        assert c.execute("SELECT COUNT(*) FROM kg_segment_fts WHERE text LIKE '%pear%'").fetchone()[0] == 1
        assert c.execute("SELECT status FROM kg_review_plan WHERE review_plan_id='pear-plan'").fetchone()[0] == 'active'
        assert c.execute("SELECT COUNT(*) FROM kg_mapping_review_event WHERE event_id='private-review'").fetchone()[0] == 1
        c.rollback()


def test_cross_child_forged_resource_list_cannot_delete(pages):
    from home_study_agent.privacy_evidence_cleanup import PrivacyEvidenceCleanupError
    db, targets = pages
    with db.connect() as c:
        sources = parent_textbook_sources(c, 'child-4')
        sources[0]['resource_ids'] = [targets['other-child']]
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_SOURCE_NOT_OWNED'):
            purge_imported(c, 'child-4', sources)
        assert c.execute('SELECT COUNT(*) FROM kg_resource').fetchone()[0] == 2
        c.rollback()


def test_malformed_retained_projection_prevents_erasure_and_preserves_both_children(pages):
    from home_study_agent.privacy_evidence_cleanup import PrivacyEvidenceCleanupError
    db, targets = pages
    with db.connect() as c:
        c.execute("UPDATE kg_language_focus_candidate SET proposed_json='{broken' WHERE resource_id=?", (targets['other-child'],))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_EVIDENCE_JSON_INVALID'):
            purge_parent_textbook_sources(c, 'child-4', purge_imported=purge_imported)
        assert c.execute('SELECT COUNT(*) FROM kg_resource').fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM kg_segment_fts WHERE text LIKE '%pear%'").fetchone()[0] == 1
        c.rollback()


def test_remaining_mapping_does_not_wash_away_preexisting_node_conflict(pages):
    db, _ = pages
    with db.connect() as c:
        c.execute("UPDATE kg_node SET status='conflicted',review_status='rejected' WHERE canonical_key='language:english:word:apple:recognition'")
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        purge_parent_textbook_sources(c, 'child-4', purge_imported=purge_imported)
        assert tuple(c.execute("SELECT status,review_status FROM kg_node WHERE canonical_key='language:english:word:apple:recognition'").fetchone()) == ('conflicted','rejected')
        c.rollback()


def test_actual_privacy_textbook_cleanup_and_restart_preserve_other_child(pages):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db, targets = pages
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='kg-cleanup-isolated')
    local = {'X-HSC-Session': settings.session_token}
    payload = {'scope':'knowledge','confirmation':'确认删除','client_action_id':'delete-captured-source'}
    for restart in (False, True):
        with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
            token = client.post('/v1/parent/' + ('unlock' if restart else 'setup'), headers=local, json={'pin':'2468'}).json()['parent_token']
            response = client.post('/v1/parent/children/child-4/delete', headers={**local,'X-HSC-Parent':token}, json=payload)
            assert response.status_code == 200, response.text
            assert response.json()['status'] == 'completed', response.text
            with db.connect() as c:
                assert not parent_textbook_sources(c, 'child-4')
                assert len(parent_textbook_sources(c, 'other-child')) == 1
                assert not c.execute('SELECT 1 FROM kg_resource WHERE resource_id=?', (targets['child-4'],)).fetchone()
                assert c.execute('SELECT 1 FROM kg_resource WHERE resource_id=?', (targets['other-child'],)).fetchone()


def test_actual_native_item_source_is_erased_not_permanently_blocked(pages):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db, targets = pages
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='kg-cleanup-isolated')
    local = {'X-HSC-Session':settings.session_token}
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin':'2468'}).json()['parent_token']
        with db.connect() as c:
            original = dict(c.execute('SELECT * FROM diagnostic_item LIMIT 1').fetchone())
            original.update(diagnostic_item_id='synthetic-page-derived-item', source_ref=targets['child-4'], prompt='Synthetic derived private page text')
            c.execute('INSERT INTO diagnostic_item (' + ','.join(original) + ') VALUES (' + ','.join('?' for _ in original) + ')', tuple(original.values()))
            c.commit()
        response = client.post('/v1/parent/children/child-4/delete', headers={**local,'X-HSC-Parent':token},
            json={'scope':'knowledge','confirmation':'确认删除','client_action_id':'blocked-host-reference'})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'completed'
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM kg_resource').fetchone()[0] == 1
            assert len(parent_textbook_sources(c, 'child-4')) == 0
            assert c.execute("SELECT status FROM deletion_job WHERE client_action_id='blocked-host-reference'").fetchone()[0] == 'completed'
            item = c.execute("SELECT status,prompt,explanation FROM diagnostic_item WHERE diagnostic_item_id='synthetic-page-derived-item'").fetchone()
            assert tuple(item) == ('retired', '题源已删除', '')


def test_actual_import_guard_busy_never_reserves_and_concurrent_delete_cannot_be_revived(captures, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db, capture, first, _, _ = captures
    capture.review('child-4', first['capture_ref'], 'Words: apple', 'ready-for-import')
    source_ref = capture.page_source_ref(first['capture_ref'])
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='kg-concurrency-isolated',
                        data_encryption_key=db.local_media_runtime.data_encryption_key)
    local = {'X-HSC-Session':settings.session_token}
    payload = {'title':'合成并发教材','ocr_text':'Words: apple','child_id':'child-4','capture_ref':first['capture_ref'],'client_action_id':'concurrent-page-import'}
    reached, release = Event(), Event()
    delete_body = {'scope':'knowledge','confirmation':'确认删除','client_action_id':'concurrent-source-delete'}
    original = TextbookOcrPageService.import_page
    def paused(self, page):
        reached.set()
        if not release.wait(20):
            raise RuntimeError('synthetic import wait expired')
        return original(self, page)
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin':'2468'}).json()['parent_token']
        headers = {**local,'X-HSC-Parent':token}
        with db.external_effect_guard(timeout=0):
            busy = client.post('/v1/parent/knowledge-graph/textbook-pages', headers=headers, json=payload)
        assert busy.status_code == 409, busy.text
        with db.connect() as c:
            assert c.execute("SELECT status FROM kg_parent_textbook_ocr_artifact WHERE client_action_id='first-page'").fetchone()[0] == 'ready'
            assert not c.execute('SELECT 1 FROM kg_resource WHERE source_ref=?', (source_ref,)).fetchone()
        monkeypatch.setattr(TextbookOcrPageService, 'import_page', paused)
        with ThreadPoolExecutor(max_workers=2) as pool:
            importing = pool.submit(client.post, '/v1/parent/knowledge-graph/textbook-pages', headers=headers, json=payload)
            try:
                assert reached.wait(15)
                with db.connect() as c:
                    sources_before = {table: [tuple(r) for r in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                        for table in ('kg_parent_textbook_ocr_artifact', 'kg_resource', 'deletion_job', 'privacy_operation_intent')}
                deleting = pool.submit(client.post, '/v1/parent/children/child-4/delete', headers=headers,
                    json=delete_body)
                assert reached.is_set() and not release.is_set() and not importing.done()
                blocked = deleting.result(timeout=10)
                assert blocked.status_code == 409 and blocked.json()['code'] == 'RESTORE_EXTERNAL_EFFECT_BUSY', blocked.text
                with db.connect() as c:
                    assert {table: [tuple(r) for r in c.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
                        for table in sources_before} == sources_before
            finally:
                release.set()
            imported = importing.result(timeout=30)
        assert imported.status_code == 200, imported.text
        deleted = client.post('/v1/parent/children/child-4/delete', headers=headers, json=delete_body)
        assert deleted.status_code == 200 and deleted.json()['status'] == 'completed', deleted.text
        replay = client.post('/v1/parent/knowledge-graph/textbook-pages', headers=headers, json=payload)
        assert replay.status_code in {404,409,422}, replay.text
        with db.connect() as c:
            assert not c.execute('SELECT 1 FROM kg_resource WHERE source_ref=?', (source_ref,)).fetchone()
            assert not parent_textbook_sources(c, 'child-4')
            assert len(parent_textbook_sources(c, 'other-child')) == 1


def test_actual_storage_failure_is_persistent_and_original_delete_can_retry(pages):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db, targets = pages
    settings = Settings(data_dir=db.path.parent, database_path=db.path, maintenance_enabled=False, session_token='kg-storage-isolated')
    local = {'X-HSC-Session':settings.session_token}
    payload = {'scope':'knowledge','confirmation':'确认删除','client_action_id':'retry-storage-erasure'}
    with TestClient(create_app(settings, model_gateway=NoCloud()), raise_server_exceptions=False) as client:
        token = client.post('/v1/parent/setup', headers=local, json={'pin':'2468'}).json()['parent_token']
        headers = {**local,'X-HSC-Parent':token}
        with db.connect() as c:
            c.execute("CREATE TRIGGER fail_synthetic_kg_delete BEFORE DELETE ON kg_resource BEGIN SELECT RAISE(ABORT,'synthetic purge failure'); END")
            c.commit()
        failed = client.post('/v1/parent/children/child-4/delete', headers=headers, json=payload)
        assert failed.json()['code'] == 'PRIVACY_EVIDENCE_CLEANUP_INCOMPLETE'
        with db.connect() as c:
            job = c.execute("SELECT deletion_id,status FROM deletion_job WHERE client_action_id='retry-storage-erasure'").fetchone()
            assert job['status'] == 'failed'
            assert c.execute('SELECT 1 FROM kg_resource WHERE resource_id=?', (targets['child-4'],)).fetchone()
            assert c.execute("SELECT COUNT(*) FROM kg_mapping_review_event WHERE event_id='private-review'").fetchone()[0] == 1
            c.execute('DROP TRIGGER fail_synthetic_kg_delete')
            c.commit()
        retried = client.post('/v1/parent/children/child-4/delete', headers=headers, json=payload)
        assert retried.status_code == 200 and retried.json()['status'] == 'completed', retried.text
        assert retried.json()['deletion_id'] == job['deletion_id']
        with db.connect() as c:
            assert c.execute('SELECT COUNT(*) FROM deletion_job').fetchone()[0] == 1
            assert not c.execute('SELECT 1 FROM kg_resource WHERE resource_id=?', (targets['child-4'],)).fetchone()


def test_mixed_frozen_atomic_batch_is_blocked_without_deleting_other_sources(pages):
    from home_study_agent.privacy_evidence_cleanup import PrivacyEvidenceCleanupError
    db, targets = pages
    with db.connect() as c:
        # Historical synthetic rows only; no extraction worker is started.
        c.execute("""INSERT INTO kg_atomic_extraction_job(job_id,idempotency_key,status,scope_json,prompt_version,created_at,updated_at)
            VALUES('historical-only','historical-only','interrupted','{}','fixture','now','now')""")
        c.execute("""INSERT INTO kg_atomic_extraction_batch(batch_id,job_id,batch_index,resource_ids_json,input_json,input_sha256,status,created_at,updated_at)
            VALUES('mixed-history','historical-only',0,?,'{"frozen":"synthetic source copies"}','digest','interrupted','now','now')""", (json.dumps(list(targets.values())),))
        c.commit()
        c.execute('BEGIN IMMEDIATE')
        with pytest.raises(PrivacyEvidenceCleanupError, match='PRIVACY_TEXTBOOK_ATOMIC_BATCH_REQUIRES_SCOPED_ERASURE'):
            purge_parent_textbook_sources(c, 'child-4', purge_imported=purge_imported)
        assert c.execute('SELECT COUNT(*) FROM kg_resource').fetchone()[0] == 2
        assert c.execute("SELECT status FROM kg_atomic_extraction_batch WHERE batch_id='mixed-history'").fetchone()[0] == 'interrupted'
        c.rollback()
