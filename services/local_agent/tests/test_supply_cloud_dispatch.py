"""Isolated real gateway dispatch; synthetic catalog only, no network transport."""
import asyncio
import json

import httpx
import pytest

from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.local_supply import LocalSupplyError, item_snapshot, validate_item
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService
from home_study_agent.variant_supply import VariantSupplyService
from normal_provider_owner import normal_provider_owner
from test_specialized_supply import FakeSupplyGateway, prepared
from test_variant_supply import FakeVariantGateway, _prepared


@pytest.fixture(params=['variant', 'specialized'])
def flow(tmp_path, request, normal_provider_owner):
    if request.param == 'variant':
        db, _, mistake_id = _prepared(tmp_path)
        service = VariantSupplyService(db)
        job = service.enqueue(mistake_id, 'child-junior-3', 'isolated-dispatch')
        table, candidates, fake = 'variant_generation_job', 'variant_candidate', FakeVariantGateway()
    else:
        db = prepared(tmp_path)
        service = SpecializedDiagnosticSupplyService(db)
        queue = service.enqueue_gaps('child-primary-4', 'competition_math', 'isolated-dispatch', 4)
        table, candidates, fake = 'specialized_supply_job', 'specialized_supply_candidate', FakeSupplyGateway()
        with db.connect() as c:
            job = dict(c.execute(f'SELECT * FROM {table} WHERE job_id=?', (queue['created_job_ids'][0],)).fetchone())
            c.execute(f'DELETE FROM {table} WHERE job_id!=?', (job['job_id'],))
            c.commit()
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    return db, service, job, table, candidates, fake, privacy


def run_gateway(flow, tmp_path, *, change=None, first_status=200):
    db, service, job, table, candidates, fake, _ = flow
    posts = []

    async def transport(request):
        assert request.url.host in {'primary.invalid', 'fallback.invalid'}
        posts.append(json.loads(request.content))
        if len(posts) == 1 and change:
            change()
        if len(posts) == 1 and first_status != 200:
            return httpx.Response(first_status, request=request)
        # Build the response from the actual admitted request, not a parallel fixture payload.
        class Envelope:
            request_type = 'mistake_variant_generation'
            context_manifest = {'raw_media': False, 'child_identity': False}
            messages = posts[-1]['messages']
        result = await fake.complete(Envelope())
        return httpx.Response(200, json={'choices': [{'message': {'content': result.content}}]})

    gateway = ModelGateway(Settings(data_dir=tmp_path, database_path=db.path,
        model_api_key='synthetic', model_base_url='https://primary.invalid',
        fallback_model_provider='qwen', fallback_model_api_key='synthetic-fallback',
        fallback_model_base_url='https://fallback.invalid', fallback_model_name='isolated'),
        transport=httpx.MockTransport(transport))
    service.gateway = gateway

    async def run():
        try:
            try:
                await service.process_next()
            except LocalSupplyError:
                pass
        finally:
            await gateway.close()
    asyncio.run(run())
    with db.connect() as c:
        current = dict(c.execute(f'SELECT * FROM {table} WHERE job_id=?', (job['job_id'],)).fetchone())
        count = c.execute(f'SELECT COUNT(*) FROM {candidates}').fetchone()[0]
    return posts, current, count


def test_false_permission_prevents_first_physical_post(flow, tmp_path):
    flow[-1].update_preferences({'cloud_model_text_allowed': False})
    posts, job, count = run_gateway(flow, tmp_path)
    assert posts == []
    assert job['status'] == 'blocked' and count == 0


@pytest.mark.parametrize('status', [200, 500, 403])
def test_revocation_stops_retry_fallback_and_late_result(flow, tmp_path, status):
    posts, job, count = run_gateway(flow, tmp_path, first_status=status,
        change=lambda: flow[-1].update_preferences({'cloud_model_text_allowed': False}))
    assert len(posts) == 1
    assert count == 0 and job['status'] == 'blocked'


@pytest.mark.parametrize('status', [200, 500])
def test_knowledge_description_change_cannot_rebind_old_response(flow, tmp_path, status):
    def change():
        with flow[0].connect() as c:
            c.execute("UPDATE knowledge_node SET description=description||' revised', knowledge_version='changed'")
            c.commit()
    posts, job, count = run_gateway(flow, tmp_path, change=change, first_status=status)
    assert len(posts) == 1
    assert count == 0 and job['status'] == 'blocked'


def test_permitted_current_request_persists_only_review_candidates(flow, tmp_path):
    posts, job, count = run_gateway(flow, tmp_path)
    assert len(posts) == 1 and count > 0 and job['status'] == 'completed'
    payload = posts[0]
    assert not {'pre_dispatch', 'child_id', 'source_snapshot', 'consent'} & payload.keys()
    with flow[0].connect() as c:
        assert not c.execute(f"SELECT 1 FROM {flow[4]} WHERE status!='needs_review'").fetchone()
    restarted = type(flow[1])(Database(flow[0].path))
    with restarted.database.connect() as c:
        assert c.execute(f'SELECT COUNT(*) FROM {flow[4]}').fetchone()[0] == count
        audit = json.loads(c.execute(f'SELECT validation_json FROM {flow[4]}').fetchone()[0])['cloud_dispatch']
        assert audit['version'] == 'supply_dispatch_authority_v2' and audit['local_only'] is True
        assert len(audit['scope_hash']) == 64 and audit['scope_hash'] not in json.dumps(posts)


@pytest.mark.parametrize('status', [500, 429, 403])
def test_current_permission_allows_normal_retry_and_fallback(flow, tmp_path, status):
    posts, job, count = run_gateway(flow, tmp_path, first_status=status)
    assert len(posts) == 2 and posts[0]['messages'] == posts[1]['messages']
    assert job['status'] == 'completed' and count > 0 and job['attempt_count'] == 1


@pytest.mark.parametrize('change', ['missing', 'not_parent'])
def test_default_preference_or_nonparent_receipt_is_not_consent(flow, tmp_path, change):
    with flow[0].connect() as c:
        c.execute("DELETE FROM consent_record WHERE scope='cloud_model_text_allowed'")
        if change == 'not_parent':
            c.execute("INSERT INTO consent_record(consent_id,scope,allowed,policy_version,source,occurred_at) VALUES ('fake','cloud_model_text_allowed',1,'privacy_v1','system','2026-09-08')")
        c.commit()
    posts, job, count = run_gateway(flow, tmp_path)
    assert not posts and not count and job['error_code'] == 'SUPPLY_CLOUD_TEXT_CONSENT_REQUIRED'


def test_revoke_then_regrant_does_not_resurrect_inflight_permission(flow, tmp_path):
    def change():
        flow[-1].update_preferences({'cloud_model_text_allowed': False})
        flow[-1].update_preferences({'cloud_model_text_allowed': True})
    posts, job, count = run_gateway(flow, tmp_path, change=change, first_status=500)
    assert len(posts) == 1 and count == 0 and job['status'] == 'blocked'


@pytest.mark.parametrize('when', ['before', 'retry', 'persist'])
def test_current_source_withdrawal_blocks_dispatch_or_persistence(flow, tmp_path, when):
    def change():
        with flow[0].connect() as c:
            c.execute("UPDATE knowledge_node SET status='retired'")
            c.commit()
    if when == 'before':
        change()
    posts, job, count = run_gateway(flow, tmp_path,
        change=change if when != 'before' else None, first_status=500 if when == 'retry' else 200)
    assert len(posts) == (0 if when == 'before' else 1)
    assert count == 0 and job['status'] == 'blocked'


def test_original_seed_snapshot_is_not_rewritten_on_restart(flow, tmp_path):
    db, service, job, table, _, _, _ = flow
    with db.connect() as c:
        item_id = ('diag-j3-discriminant-1' if table == 'variant_generation_job' else job['seed_item_id'])
        snapshot = item_snapshot(validate_item(c, job['child_id'], item_id))
        snapshot['explanation'] = 'An older frozen source, not the current explanation.'
        stored = json.dumps(snapshot, indent=2)
        c.execute(f'UPDATE {table} SET source_snapshot_json=? WHERE job_id=?', (stored, job['job_id']))
        c.commit()
    flow = (db, type(service)(Database(db.path)), *flow[2:])
    posts, current, count = run_gateway(flow, tmp_path)
    assert not posts and not count and current['status'] == 'blocked'
    assert current['source_snapshot_json'] == stored


def test_valid_stored_seed_snapshot_keeps_exact_persisted_bytes(flow, tmp_path):
    with flow[0].connect() as c:
        item_id = 'diag-j3-discriminant-1' if flow[3] == 'variant_generation_job' else flow[2]['seed_item_id']
        stored = json.dumps(item_snapshot(validate_item(c, flow[2]['child_id'], item_id)), indent=4)
        c.execute(f'UPDATE {flow[3]} SET source_snapshot_json=? WHERE job_id=?', (stored, flow[2]['job_id']))
        c.commit()
    posts, job, count = run_gateway(flow, tmp_path, first_status=500)
    assert len(posts) == 2 and count > 0 and job['status'] == 'completed'
    assert job['source_snapshot_json'] == stored


@pytest.mark.parametrize('change', ['job_child', 'generation', 'snapshot', 'knowledge_version'])
def test_frozen_local_scope_rechecked_at_every_post(flow, tmp_path, change):
    def mutate():
        with flow[0].connect() as c:
            if change == 'job_child':
                child = 'child-primary-4' if flow[2]['child_id'] == 'child-junior-3' else 'child-junior-3'
                c.execute(f'UPDATE {flow[3]} SET child_id=? WHERE job_id=?', (child, flow[2]['job_id']))
            elif change == 'generation':
                c.execute(f'UPDATE {flow[3]} SET retry_count=retry_count+1 WHERE job_id=?', (flow[2]['job_id'],))
            elif change == 'snapshot':
                c.execute(f"UPDATE {flow[3]} SET source_snapshot_json='{{}}' WHERE job_id=?", (flow[2]['job_id'],))
            else:
                c.execute("UPDATE knowledge_node SET knowledge_version='new-version'")
            c.commit()
    posts, _, count = run_gateway(flow, tmp_path, change=mutate, first_status=500)
    assert len(posts) == 1 and count == 0


@pytest.mark.parametrize('snapshot', ['', '{broken', 'null', '{}'])
def test_malformed_stored_snapshot_is_not_treated_as_a_new_job(flow, tmp_path, snapshot):
    with flow[0].connect() as c:
        c.execute(f'UPDATE {flow[3]} SET source_snapshot_json=? WHERE job_id=?', (snapshot, flow[2]['job_id']))
        c.commit()
    posts, job, count = run_gateway(flow, tmp_path)
    assert not posts and not count and job['status'] == 'blocked'
    assert job['source_snapshot_json'] == snapshot


def test_explicit_retry_after_restart_uses_same_job_and_seed(flow, tmp_path):
    posts, blocked, _ = run_gateway(flow, tmp_path, first_status=500,
        change=lambda: flow[-1].update_preferences({'cloud_model_text_allowed': False}))
    assert len(posts) == 1 and blocked['status'] == 'blocked'
    restarted = type(flow[1])(Database(flow[0].path))
    restarted.recover()
    flow[-1].update_preferences({'cloud_model_text_allowed': True})
    retried = restarted.retry_job(flow[2]['child_id'], flow[2]['job_id'], allow_model_retry=True)
    assert retried['job_id'] == flow[2]['job_id']
    posts, current, count = run_gateway((flow[0], restarted, *flow[2:]), tmp_path)
    assert len(posts) == 1 and count > 0 and current['status'] == 'completed'
    assert current['source_snapshot_json'] == blocked['source_snapshot_json']


def test_family_owned_published_source_remains_local_and_locally_executable(tmp_path):
    from test_curriculum_supply import prepared as curriculum_prepared, publish, CHILD
    from home_study_agent.local_supply import score_local_item
    db, curriculum, payload = curriculum_prepared(tmp_path)
    published = publish(curriculum, payload)
    item_id = published['diagnostic_item_id']
    service = SpecializedDiagnosticSupplyService(db)
    queued = service.enqueue_gaps(CHILD, 'competition_math', 'restricted-source', 4)
    job_id = queued['created_job_ids'][0]
    with db.connect() as c:
        c.execute('DELETE FROM specialized_supply_job WHERE job_id!=? AND generation_mode=\'model\'', (job_id,))
        c.execute('UPDATE specialized_supply_job SET seed_item_id=? WHERE job_id=?', (item_id, job_id))
        job = dict(c.execute('SELECT * FROM specialized_supply_job WHERE job_id=?', (job_id,)).fetchone())
        item = validate_item(c, CHILD, item_id)
        c.commit()
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    flow = (db, service, job, 'specialized_supply_job', 'specialized_supply_candidate', FakeSupplyGateway(), privacy)
    posts, current, _ = run_gateway(flow, tmp_path)
    assert not posts and current['error_code'] == 'SUPPLY_CLOUD_SOURCE_PURPOSE_REQUIRED'
    with db.connect() as c:
        item = validate_item(c, CHILD, item_id)
        assert score_local_item(item, '5000')[0] == 1.0
    assert curriculum.get(CHILD, published['candidate_id'])['eligible'] is True
