"""Parent-authorized synthetic paper questions, never production media or rights proof."""
import asyncio
import json

import httpx
import pytest

from home_study_agent.database import Database
from home_study_agent.config import Settings
from home_study_agent.local_supply import LocalSupplyError
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.paper_variant_authorization import (
    PaperVariantAuthorizationService, export_child, delete_child_scope,
    quarantine_after_restore, validate_job_authorization,
)
from home_study_agent.variant_supply import VariantSupplyService
import test_paper_variant_journey as journey
from normal_provider_owner import normal_provider_owner


CHILD = 'child-primary-4'
LICENSE = {'license_id': 'original_author', 'reference': 'Isolated original exercise declaration',
           'evidence': 'Parent confirms this synthetic arithmetic question is original and permits cloud variant generation.'}


@pytest.fixture
def paper(tmp_path):
    db, book, mid = journey.paper_case(tmp_path)
    service = PaperVariantAuthorizationService(db)
    service.initialize()
    return db, book, mid, service


def grant(paper, key='grant-paper'):
    _, _, mid, service = paper
    view = service.read(CHILD, mistake_id=mid)
    return service.grant(CHILD, mistake_id=mid, question_version=view['question_version'],
        source_version=view['source_version'], license_basis=LICENSE, expected_revision=view['revision'],
        client_action_id=key, actor_id='parent_ui')


def test_grant_creates_job_once_and_old_receipt_cannot_regrant(paper):
    db, _, mid, service = paper
    blocked = VariantSupplyService(db, journey.PaperGateway())
    old = blocked.enqueue(mid, CHILD, 'old-child-diagnosis-action')
    assert asyncio.run(blocked.process_next())['status'] == 'blocked'
    view = service.read(CHILD, mistake_id=mid)
    args = dict(mistake_id=mid, question_version=view['question_version'], source_version=view['source_version'],
        license_basis=LICENSE, expected_revision=0, client_action_id='one-parent-operation', actor_id='parent_ui')
    allowed = service.grant(CHILD, **args)
    jid = allowed['generation_job']['job_id']
    assert jid != old['job_id'] and allowed['current']['status'] == 'authorized'
    restarted = PaperVariantAuthorizationService(Database(db.path))
    assert restarted.grant(CHILD, **args)['generation_job']['job_id'] == jid
    revoked = restarted.revoke(CHILD, mistake_id=mid, expected_revision=1,
        client_action_id='revoke', actor_id='parent_ui')
    assert revoked['current']['status'] == 'revoked'
    assert revoked['current']['reason'] == 'PAPER_VARIANT_REVOKED'
    second = grant((db, None, mid, restarted), 'another-parent-operation')
    assert second['generation_job']['job_id'] not in {old['job_id'], jid}
    replay = restarted.grant(CHILD, **args)
    assert replay['receipt'] == allowed['receipt'] and replay['generation_job']['status'] == 'blocked'
    with db.connect() as c:
        c.execute('BEGIN')
        with pytest.raises(ValueError, match='PAPER_VARIANT_'):
            validate_job_authorization(c, job_id=jid)
    with pytest.raises(LocalSupplyError):
        blocked.retry_from_parent(CHILD, old['job_id'], 'retry-old-parent-key')
    gateway = journey.PaperGateway()
    assert asyncio.run(VariantSupplyService(db, gateway).process_next())['job_id'] == second['generation_job']['job_id']
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('case', ['wrong_child', 'wrong_version', 'wrong_actor', 'different_payload', 'writing_purpose'])
def test_parent_binding_and_purpose_not_boolean_or_other_grant(paper, case):
    db, _, mid, service = paper
    view = service.read(CHILD, mistake_id=mid)
    args = dict(mistake_id=mid, question_version=view['question_version'], source_version=view['source_version'],
        license_basis=LICENSE, expected_revision=0, client_action_id='parent-action', actor_id='parent_ui')
    child = CHILD
    if case == 'wrong_child':
        child = 'child-junior-3'
    elif case == 'wrong_version':
        args['question_version'] = 'changed'
    elif case == 'wrong_actor':
        args['actor_id'] = 'child'
    elif case == 'different_payload':
        service.grant(CHILD, **args)
        args['license_basis'] = {**LICENSE, 'reference': 'A different declaration'}
    else:
        args['license_basis'] = {**LICENSE, 'purpose': 'writing_question_formative_text_v1'}
    with pytest.raises((ValueError, KeyError)):
        service.grant(child, **args)


@pytest.mark.parametrize('change', ['page', 'candidate', 'cross_child', 'ambiguous', 'restricted', 'graph', 'quality'])
def test_exact_paper_source_changes_or_restrictions_do_not_inherit_grant(paper, change):
    db, _, mid, service = paper
    allowed = grant(paper)
    with db.connect() as c:
        if change == 'page':
            c.execute("UPDATE capture_artifact SET extracted_text=extracted_text||' new revision' WHERE artifact_id='paper'")
        elif change == 'candidate':
            c.execute("UPDATE mistake_capture_candidate SET question_text='Different question' WHERE artifact_id='paper'")
        elif change == 'cross_child':
            c.execute("UPDATE capture_artifact SET child_id='child-junior-3' WHERE artifact_id='paper'")
        elif change == 'ambiguous':
            c.execute("UPDATE capture_artifact SET extracted_text=extracted_text||extracted_text WHERE artifact_id='paper'")
        elif change == 'restricted':
            c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='paper'", (json.dumps({'source': {'local_only': True}}),))
        elif change == 'graph':
            c.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id='paper'", (json.dumps({'knowledge_context': 'private graph excerpt'}),))
        else:
            c.execute("UPDATE capture_artifact SET confidence=.1 WHERE artifact_id='paper'")
        c.commit()
    view = service.read(CHILD, mistake_id=mid)
    assert view['status'] == 'invalidated'
    gateway = journey.PaperGateway()
    result = asyncio.run(VariantSupplyService(db, gateway).process_next())
    assert result['status'] == 'blocked' and not gateway.requests
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM variant_candidate WHERE job_id=?', (allowed['generation_job']['job_id'],)).fetchone()


@pytest.mark.parametrize('status', [200, 500, 403])
@pytest.mark.parametrize('withdraw', [False, True])
def test_actual_gateway_retry_and_withdrawal_preserve_original_authorization(paper, tmp_path, status, withdraw, normal_provider_owner):
    db, _, mid, service = paper
    allowed = grant(paper)
    posts = []
    fake = journey.PaperGateway()
    async def transport(request):
        assert request.url.host in {'primary.invalid', 'fallback.invalid'}
        posts.append(json.loads(request.content))
        if len(posts) == 1 and withdraw:
            service.revoke(CHILD, mistake_id=mid, expected_revision=1, client_action_id='during-post', actor_id='parent_ui')
        if len(posts) == 1 and status != 200:
            return httpx.Response(status, request=request)
        class Envelope:
            messages = posts[-1]['messages']
        result = await fake.complete(Envelope())
        return httpx.Response(200, json={'choices': [{'message': {'content': result.content}}]})
    gateway = ModelGateway(Settings(data_dir=tmp_path, database_path=db.path,
        model_api_key='synthetic', model_base_url='https://primary.invalid',
        fallback_model_provider='qwen', fallback_model_api_key='synthetic',
        fallback_model_base_url='https://fallback.invalid', fallback_model_name='isolated'), transport=httpx.MockTransport(transport))
    async def run():
        try:
            try:
                return await VariantSupplyService(db, gateway).process_next()
            except LocalSupplyError:
                return None
        finally:
            await gateway.close()
    asyncio.run(run())
    assert len(posts) == (1 if withdraw or status == 200 else 2)
    with db.connect() as c:
        assert bool(c.execute('SELECT 1 FROM variant_candidate WHERE job_id=?', (allowed['generation_job']['job_id'],)).fetchone()) is not withdraw
    assert '孩子原答案-禁止上传' not in json.dumps(posts, ensure_ascii=False)
    assert allowed['receipt']['event_id'] not in json.dumps(posts)


@pytest.mark.parametrize('case', ['review', 'daily_plan', 'delete', 'edit', 'cross_child'])
def test_parent_grant_unblocks_real_existing_paper_journey(tmp_path, monkeypatch, case):
    if case == 'review':
        journey.test_paper_without_catalog_item_generates_reviewable_exact_items_and_withdraws(tmp_path)
    elif case == 'daily_plan':
        journey.test_paper_exercise_real_attempt_feedback_next_day_plan_restart_and_revocation(tmp_path, monkeypatch)
    else:
        journey.test_promoted_material_source_is_revalidated_after_restart(tmp_path, case)


def test_lifecycle_hooks_borrow_transaction_and_restore_does_not_regrant(paper):
    db, _, mid, service = paper
    original = service.read(CHILD, mistake_id=mid)
    allowed = grant(paper)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        exported = export_child(c, CHILD)
        assert len(exported['paper_variant_authorizations']) == len(exported['paper_variant_job_authorizations']) == 1
        assert '孩子原答案-禁止上传' not in json.dumps(exported, ensure_ascii=False)
        assert quarantine_after_restore(c, 'isolated-restore') == 1
        assert quarantine_after_restore(c, 'isolated-restore') == 0
        assert c.in_transaction
        c.rollback()
    assert service.read(CHILD, mistake_id=mid)['status'] == 'authorized'
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        quarantine_after_restore(c, 'isolated-restore')
        c.commit()
    restored = PaperVariantAuthorizationService(Database(db.path)).read(CHILD, mistake_id=mid)
    assert restored['status'] == 'revoked'
    assert restored['reason'] == 'PAPER_VARIANT_RESTORE_REAUTHORIZATION_REQUIRED'
    assert '重新授权' in restored['message']
    replay = service.grant(CHILD, mistake_id=mid, question_version=original['question_version'],
        source_version=original['source_version'], license_basis=LICENSE,
        expected_revision=original['revision'], client_action_id='grant-paper', actor_id='parent_ui')
    assert replay['receipt'] == allowed['receipt']
    assert replay['current']['reason'] == 'PAPER_VARIANT_RESTORE_REAUTHORIZATION_REQUIRED'
    assert replay['generation_job']['status'] == 'blocked'
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert delete_child_scope(c, 'child-junior-3', 'media') == {'paper_variant_job_authorization': 0, 'paper_variant_authorization_event': 0}
        counts = delete_child_scope(c, CHILD, 'media')
        assert counts['paper_variant_job_authorization'] == 1 and counts['paper_variant_authorization_event'] == 2
        assert c.in_transaction
        c.rollback()
    assert service.read(CHILD, mistake_id=mid)['status'] == 'revoked'


def test_grant_job_failure_rolls_back_authorization_and_old_job_stop(paper, monkeypatch):
    db, _, mid, service = paper
    old = VariantSupplyService(db).enqueue(mid, CHILD, 'old-pending')
    create = service._create_job
    def fail(*args):
        create(*args)
        raise RuntimeError('isolated write failure after job creation')
    monkeypatch.setattr(service, '_create_job', fail)
    with pytest.raises(RuntimeError, match='isolated write failure'):
        grant(paper)
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_variant_authorization_event').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM paper_variant_job_authorization').fetchone()[0] == 0
        assert c.execute('SELECT status FROM variant_generation_job WHERE job_id=?', (old['job_id'],)).fetchone()[0] == 'pending'
    monkeypatch.setattr(service, '_create_job', create)
    assert grant(paper)['generation_job']['status'] == 'pending'


@pytest.mark.parametrize('mutation', ['false', 'missing', 'system', 'future'])
def test_grant_requires_current_explicit_parent_text_consent(paper, tmp_path, mutation):
    db, _, mid, service = paper
    with db.connect() as c:
        if mutation == 'false':
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        elif mutation == 'missing':
            c.execute("DELETE FROM consent_record WHERE scope='cloud_model_text_allowed'")
        elif mutation == 'system':
            c.execute("UPDATE consent_record SET source='system' WHERE scope='cloud_model_text_allowed'")
        else:
            c.execute("UPDATE consent_record SET occurred_at='2999-01-01T00:00:00+00:00' WHERE scope='cloud_model_text_allowed'")
        c.commit()
    with pytest.raises(ValueError, match='PAPER_VARIANT_'):
        grant(paper)
    assert service.read(CHILD, mistake_id=mid)['status'] == 'blocked'
    with db.connect() as c:
        assert not c.execute('SELECT 1 FROM paper_variant_authorization_event').fetchone()


def test_consent_regrant_requires_new_parent_grant_job_not_old_retry(paper, tmp_path):
    db, _, mid, service = paper
    first = grant(paper)
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.update_preferences({'cloud_model_text_allowed': False})
    privacy.update_preferences({'cloud_model_text_allowed': True})
    assert service.read(CHILD, mistake_id=mid)['status'] == 'invalidated'
    gateway = journey.PaperGateway()
    supply = VariantSupplyService(db, gateway)
    assert asyncio.run(supply.process_next())['status'] == 'blocked'
    with pytest.raises(LocalSupplyError, match='PAPER_VARIANT_'):
        supply.retry_job(CHILD, first['generation_job']['job_id'], allow_model_retry=True)
    current = grant(paper, 'new-consented-parent-grant')
    assert current['generation_job']['job_id'] != first['generation_job']['job_id']
    assert asyncio.run(supply.process_next())['status'] == 'completed'
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('license_id', ['CC0-1.0', 'explicit_cloud_processing_permission'])
def test_supported_explicit_license_declaration_allows_paper_not_just_original_author(paper, license_id):
    _, _, mid, service = paper
    view = service.read(CHILD, mistake_id=mid)
    result = service.grant(CHILD, mistake_id=mid, question_version=view['question_version'],
        source_version=view['source_version'], license_basis={**LICENSE, 'license_id': license_id,
        'reference': 'https://isolated.invalid/permission'}, expected_revision=0, client_action_id='licensed', actor_id='parent_ui')
    assert result['generation_job']['status'] == 'pending'


@pytest.mark.parametrize('scope', ['learning', 'knowledge', 'media', 'all_history'])
def test_lifecycle_delete_is_child_scoped_and_old_job_has_no_authority(paper, scope):
    db, _, mid, _ = paper
    allowed = grant(paper)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert delete_child_scope(c, 'child-junior-3', scope)['paper_variant_authorization_event'] == 0
        counts = delete_child_scope(c, CHILD, scope)
        assert counts == {'paper_variant_job_authorization': 1, 'paper_variant_authorization_event': 1}
        c.commit()
    gateway = journey.PaperGateway()
    assert asyncio.run(VariantSupplyService(db, gateway).process_next())['status'] == 'blocked'
    assert not gateway.requests


@pytest.mark.parametrize('source', ['candidate', 'artifact'])
def test_native_source_fk_cascade_removes_authority_without_new_schema_migration(paper, source):
    db, _, mid, _ = paper
    grant(paper)
    with db.connect() as c:
        if source == 'candidate':
            c.execute("DELETE FROM mistake_capture_candidate WHERE artifact_id='paper'")
        else:
            c.execute("DELETE FROM capture_artifact WHERE artifact_id='paper'")
        c.commit()
        assert not c.execute('SELECT 1 FROM paper_variant_authorization_event').fetchone()
        assert not c.execute('SELECT 1 FROM paper_variant_job_authorization').fetchone()
        assert not c.execute('PRAGMA foreign_key_check').fetchall()


def test_read_and_export_are_query_only_and_hide_original_answer_and_page(paper):
    db, _, mid, service = paper
    with db.connect() as c:
        c.execute("UPDATE capture_artifact SET extracted_text='Private page margin. '||extracted_text||' Private page footer.' WHERE artifact_id='paper'")
        c.commit()
    grant(paper)
    with db.connect() as c:
        c.execute('BEGIN')
        c.execute('PRAGMA query_only=ON')
        before = c.total_changes
        exported = export_child(c, CHILD)
        assert c.total_changes == before
    view = service.read(CHILD, mistake_id=mid)
    assert view['question_preview'] == '计算 (37+13)×4，只回答一个整数。'
    text = json.dumps([view, exported], ensure_ascii=False)
    assert 'Private page' not in text and '孩子原答案-禁止上传' not in text and 'local_path' not in text
