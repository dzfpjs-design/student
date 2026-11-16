"""Confirmed synthetic paper -> parent purpose -> real variant job, plus lifecycle."""

from local_restore_operation_fixture import restore_local
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from test_paper_variant_journey import paper_case, PaperGateway
from test_writing_question_authorization_http import parent

CHILD = 'child-primary-4'
LICENSE = {'license_id': 'original_author', 'reference': 'Synthetic arithmetic fixture, local test author',
           'evidence': 'This exact arithmetic question was created for this isolated test; its author permits cloud variant generation.'}


@pytest.fixture
def paper_host(tmp_path):
    db, book, mid = paper_case(tmp_path)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        data_encryption_key=b'P' * 32)
    privacy = PrivacyLifecycleService(db, tmp_path, data_encryption_key=b'P' * 32)
    privacy.initialize()
    return db, settings, mid, privacy


def url(mid, child=CHILD):
    return f'/v1/parent/children/{child}/paper-variant-authorizations/{mid}'


def body(view, key='paper-purpose-http-grant'):
    return {name: view[name] for name in ('question_version', 'source_version')} | {
        'license_basis': LICENSE, 'expected_revision': view['revision'], 'client_action_id': key}


def test_parent_purpose_produces_reviewable_variants_and_durable_revoke(paper_host):
    db, settings, mid, _ = paper_host
    gateway = PaperGateway()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert client.get(url(mid)).status_code == 401
        headers = parent(client, True)
        initial = client.get(url(mid), headers=headers)
        assert initial.status_code == 200, initial.text
        assert initial.headers['cache-control'] == 'no-store'
        view = initial.json()
        assert view['status'] == 'authorization_required'
        assert view['question_preview'] == '计算 (37+13)×4，只回答一个整数。'
        assert '孩子原答案-禁止上传' not in initial.text
        payload = body(view)
        assert client.post(url(mid)+'/grant', headers=headers, json={**payload, 'actor_id': 'parent'}).status_code == 422
        granted = client.post(url(mid)+'/grant', headers=headers, json=payload)
        assert granted.status_code == 200, granted.text
        first = granted.json()
        assert first['current']['status'] == 'authorized'
        assert first['generation_job']['status'] == 'pending'
        generated = client.post('/v1/parent/variant-jobs/process-next', headers=headers)
        assert generated.status_code == 200, generated.text
        assert generated.json()['job']['status'] == 'completed'
        candidates = client.get(f'/v1/parent/children/{CHILD}/variant-candidates', headers=headers).json()['items']
        assert len(candidates) == 4
        review = client.post('/v1/parent/variant-candidates/'+candidates[0]['candidate_id']+'/review',
                             headers=headers, json={'decision': 'approve', 'note': '核对了新题与答案。', 'client_action_id': 'paper-purpose-review'})
        assert review.status_code == 200, review.text
        assert review.json()['diagnostic_item_id']
        assert len(gateway.requests) == 1
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        headers = parent(client)
        assert client.get(url(mid), headers=headers).json()['status'] == 'authorized'
        revoked = client.post(url(mid)+'/revoke', headers=headers,
                              json={'expected_revision': 1, 'client_action_id': 'paper-purpose-http-revoke'})
        assert revoked.status_code == 200, revoked.text
        replay = client.post(url(mid)+'/grant', headers=headers, json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()['receipt'] == first['receipt']
        assert replay.json()['current']['status'] == 'revoked'
        assert replay.json()['generation_job']['job_id'] == first['generation_job']['job_id']
        other = client.get(url(mid, 'child-junior-3'), headers=headers)
        assert other.status_code == 404 and 'question_preview' not in other.json()
        assert client.post(url(mid, 'child-junior-3')+'/grant', headers=headers, json=payload).status_code == 404
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_variant_authorization_event').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM paper_variant_job_authorization').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM report_delivery_outbox').fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('scope', ['learning', 'knowledge', 'media', 'all_history'])
def test_paper_purpose_export_and_deletion_do_not_leave_live_permission(paper_host, scope):
    from home_study_agent.paper_variant_authorization import PaperVariantAuthorizationService
    db, _, mid, privacy = paper_host
    service = PaperVariantAuthorizationService(db)
    service.initialize()
    service.grant(CHILD, mistake_id=mid, actor_id='parent_ui', **body(service.read(CHILD, mistake_id=mid)))
    other_session = 'other-historical-session'
    with db.connect() as c:
        c.execute('''INSERT INTO daily_session(session_id,child_id,status,state,started_at,ended_at,last_checkpoint_at,
            client_action_id) VALUES (?,'child-junior-3','completed','completed',datetime('now','-1 day'),
            datetime('now','-1 day'),datetime('now','-1 day'),'other-historical-session')''', (other_session,))
        c.execute('''INSERT INTO capture_artifact(artifact_id,child_id,session_id,artifact_type,extracted_text,confidence,
            details_json,status,created_at,client_action_id) VALUES ('other-paper','child-junior-3',?,'document_ocr',
            'Unrelated synthetic paper',.95,'{}','completed',datetime('now'),'other-paper')''', (other_session,))
        c.execute('''INSERT INTO mistake_capture_candidate(candidate_id,artifact_id,child_id,question_index,
            question_text,confidence,status,created_at,updated_at) VALUES ('other-candidate','other-paper','child-junior-3',
            0,'Unrelated synthetic question',.95,'pending',datetime('now'),datetime('now'))''')
        c.commit()
    assert privacy.deletion_preview(CHILD)['paper_variant_authorizations'] == 1
    exported = json.loads(Path(privacy.export_child(CHILD)['path']).read_text())
    assert len(exported['paper_variant_authorizations']) == 1
    assert len(exported['paper_variant_job_authorizations']) == 1
    result = privacy.delete_child_scope(CHILD, scope, '确认删除', 'paper-purpose-delete-'+scope)
    assert result['status'] == 'completed', result
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_variant_authorization_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM paper_variant_job_authorization').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM mistake_capture_candidate WHERE child_id='child-junior-3'").fetchone()[0] == 1
        if scope in {'learning', 'media', 'all_history'}:
            assert c.execute('SELECT COUNT(*) FROM mistake_capture_candidate WHERE child_id=?', (CHILD,)).fetchone()[0] == 0
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_paper_purpose_backup_restore_revokes_old_job_and_old_command(paper_host):
    from home_study_agent.paper_variant_authorization import PaperVariantAuthorizationService
    db, _, mid, privacy = paper_host
    service = PaperVariantAuthorizationService(db)
    service.initialize()
    payload = body(service.read(CHILD, mistake_id=mid))
    original = service.grant(CHILD, mistake_id=mid, actor_id='parent_ui', **payload)
    backup = privacy.create_backup()
    service.revoke(CHILD, mistake_id=mid, expected_revision=1, client_action_id='revoke-after-backup', actor_id='parent_ui')
    from home_study_agent.privacy_lifecycle import RestoreWhileSessionActiveError
    with pytest.raises(RestoreWhileSessionActiveError):
        restore_local(privacy,backup['backup_id'], '确认恢复')
    # The restore scenario begins after the synthetic lesson has ended. This
    # fixture transition is not claimed as voice lesson-closure evidence.
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=datetime('now') WHERE status='active'")
        c.commit()
    restore_local(privacy,backup['backup_id'], '确认恢复')
    replay = service.grant(CHILD, mistake_id=mid, actor_id='parent_ui', **payload)
    assert replay['receipt'] == original['receipt']
    assert replay['current']['status'] == 'revoked'
    assert 'RESTORE' in replay['current']['reason']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_variant_job_authorization').fetchone()[0] == 1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
