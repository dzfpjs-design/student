"""Temporary imported curriculum and physical MockTransport requests only."""
import asyncio
import json

import httpx
import pytest

from normal_provider_owner import normal_provider_owner

from home_study_agent.config import Settings
from home_study_agent.curriculum_internalization import CurriculumInternalizationService
from home_study_agent.curriculum_standards import CurriculumStandardService
from home_study_agent.database import Database
from home_study_agent.knowledge_base import KnowledgeBaseService
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from test_curriculum_corpus import (
    InternalizationGateway, create_official_math_standard, create_seyk_source, import_payload,
)


CHILD = 'child-primary-4'


@pytest.fixture
def dispatch_case(tmp_path):
    db = Database(tmp_path / 'isolated.db')
    db.initialize()
    StudyRepository(db).seed_family()
    corpus = KnowledgeBaseService(db).import_corpus(import_payload(*create_seyk_source(tmp_path / 'source')))
    root = create_official_math_standard(tmp_path / 'standards',
        '小数加法学习中，学生理解小数的意义，能进行简单的小数加减运算，并形成运算能力。')
    CurriculumStandardService(db).import_root(str(root))
    privacy = PrivacyLifecycleService(db, tmp_path)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    return db, corpus, privacy, tmp_path


def make_service(case, mutation=None, *, retry=False):
    db, _, _, root = case
    posts = []
    response_factory = InternalizationGateway()

    async def transport(request):
        assert request.url.host == 'curriculum.invalid'
        payload = json.loads(request.content)
        posts.append(payload)
        assert set(payload) <= {'model', 'messages', 'temperature', 'max_tokens', 'stream', 'response_format', 'enable_thinking'}
        wire = json.dumps(payload, ensure_ascii=False)
        assert str(root) not in wire and 'pre_dispatch' not in wire
        assert 'consent_id' not in wire and 'authority' not in wire
        if mutation and len(posts) == 1:
            mutation()
        if retry and len(posts) == 1:
            return httpx.Response(500)
        from types import SimpleNamespace
        result = await response_factory.complete(SimpleNamespace(messages=payload['messages']))
        return httpx.Response(200, json={'choices': [{'message': {'content': result.content}}]})

    settings = Settings(data_dir=root, database_path=db.path, maintenance_enabled=False,
        model_provider='deepseek', model_api_key='synthetic-not-a-secret',
        model_base_url='https://curriculum.invalid', model_name='isolated-model')
    gateway = ModelGateway(settings, transport=httpx.MockTransport(transport))
    return CurriculumInternalizationService(db, gateway), posts


def create(service, corpus, key='original-job'):
    return service.create_job(corpus['corpus_id'], {'child_id': CHILD, 'subject_title': '小学数学',
        'version_title': '苏教版', 'semester': '下册', 'max_units': 1, 'client_action_id': key})


def change(case, service, kind):
    db, _, privacy, _ = case
    if kind in {'consent', 'consent_cycle'}:
        privacy.update_preferences({'cloud_model_text_allowed': False})
        if kind == 'consent_cycle':
            privacy.update_preferences({'cloud_model_text_allowed': True})
    elif kind == 'stop':
        service.recover_interrupted()
    else:
        sql = {
            'source': "UPDATE curriculum_resource SET transcript_text=transcript_text||' revised source'",
            'title': "UPDATE curriculum_standard_source SET title=title||' revised title'",
            'corpus': "UPDATE curriculum_corpus SET review_status='quarantined'",
            'child': "UPDATE child_profile SET status='inactive' WHERE child_id='child-primary-4'",
        }[kind]
        with db.connect() as c:
            c.execute(sql)
            c.commit()


@pytest.mark.parametrize('phase', ['first', 'retry', 'persist'])
@pytest.mark.parametrize('kind', ['source', 'title', 'corpus', 'consent', 'consent_cycle', 'stop'])
def test_original_job_authority_blocks_changed_source_consent_or_stop(dispatch_case, phase, kind, normal_provider_owner):
    holder = {}
    mutate = lambda: change(dispatch_case, holder['service'], kind)
    service, posts = make_service(dispatch_case, mutate if phase != 'first' else None, retry=phase == 'retry')
    holder['service'] = service
    job = create(service, dispatch_case[1])
    if phase == 'first':
        mutate()
    asyncio.run(service.run_job(job['job_id']))
    assert len(posts) == (0 if phase == 'first' else 1)
    assert service.get_job(job['job_id'])['status'] == ('interrupted' if kind == 'stop' else 'failed')
    with dispatch_case[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM curriculum_capability').fetchone()[0] == 0
    # Reload/replay may neither refreeze the current source nor revive a stopped job.
    restarted = CurriculumInternalizationService(dispatch_case[0], service.gateway)
    assert create(restarted, dispatch_case[1])['job_id'] == job['job_id']
    asyncio.run(restarted.run_job(job['job_id']))
    assert len(posts) == (0 if phase == 'first' else 1)
    assert restarted.get_job(job['job_id'])['status'] == ('interrupted' if kind == 'stop' else 'failed')


@pytest.mark.parametrize('retry', [False, True])
def test_approved_source_normal_retry_and_restart_remain_real_positive(dispatch_case, retry, normal_provider_owner):
    service, posts = make_service(dispatch_case, retry=retry)
    job = create(service, dispatch_case[1])
    restarted = CurriculumInternalizationService(dispatch_case[0], service.gateway)
    asyncio.run(restarted.run_job(job['job_id']))
    assert len(posts) == (2 if retry else 1)
    assert restarted.get_job(job['job_id'])['status'] == 'completed'
    with dispatch_case[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM curriculum_capability').fetchone()[0] == 1
        assert c.execute("SELECT maturity FROM curriculum_capability").fetchone()[0] == 'model_drafted'
    asyncio.run(restarted.run_job(job['job_id']))
    assert len(posts) == (2 if retry else 1)


@pytest.mark.parametrize('permission', ['false', 'missing'])
def test_initial_permission_is_not_inferred_from_configured_gateway(dispatch_case, permission, normal_provider_owner):
    db, _, privacy, _ = dispatch_case
    if permission == 'false':
        privacy.update_preferences({'cloud_model_text_allowed': False})
    else:
        with db.connect() as c:
            c.execute('DELETE FROM consent_record')
            c.commit()
    service, posts = make_service(dispatch_case)
    job = create(service, dispatch_case[1])
    asyncio.run(service.run_job(job['job_id']))
    assert posts == []
    assert service.get_job(job['job_id'])['status'] == 'failed'
    privacy.update_preferences({'cloud_model_text_allowed': True})
    asyncio.run(service.run_job(job['job_id']))
    assert posts == []
    next_job = create(service, dispatch_case[1], 'explicit-new-job')
    asyncio.run(service.run_job(next_job['job_id']))
    assert len(posts) == 1 and service.get_job(next_job['job_id'])['status'] == 'completed'


def test_offline_queue_is_not_upgraded_by_later_parent_consent(dispatch_case, normal_provider_owner):
    dispatch_case[2].update_preferences({'cloud_model_text_allowed': False})
    service, posts = make_service(dispatch_case)
    job = create(service, dispatch_case[1])
    dispatch_case[2].update_preferences({'cloud_model_text_allowed': True})
    asyncio.run(service.run_job(job['job_id']))
    assert not posts
    assert service.get_job(job['job_id'])['error_code'] == 'INTERNALIZATION_TEXT_CONSENT_REQUIRED_NEW_JOB_REQUIRED'


@pytest.mark.parametrize('kind', ['missing', 'quarantined', 'other_child', 'other_authority'])
def test_authority_history_is_not_refrozen_or_revived_after_restore(dispatch_case, kind, normal_provider_owner):
    db = dispatch_case[0]
    service, posts = make_service(dispatch_case)
    job = create(service, dispatch_case[1])
    with db.connect() as c:
        original = dict(c.execute("SELECT * FROM audit_event WHERE action='curriculum_internalization_authority_v1'").fetchone())
        if kind == 'missing':
            c.execute('DELETE FROM audit_event WHERE audit_id=?', (original['audit_id'],))
        else:
            c.execute("INSERT INTO audit_event VALUES('isolated-restore','system','curriculum_internalization_authority_quarantined_v1',?,?,?)",
                (CHILD if kind != 'other_child' else 'child-junior-3', json.dumps({
                    'authority_audit_id': original['audit_id'] if kind != 'other_authority' else 'other-authority',
                    'reason': 'backup_restore'}), original['occurred_at']))
        c.commit()
    restarted = CurriculumInternalizationService(db, service.gateway)
    asyncio.run(restarted.run_job(job['job_id']))
    blocked = kind in {'missing', 'quarantined'}
    assert len(posts) == (0 if blocked else 1)
    assert restarted.get_job(job['job_id'])['status'] == ('failed' if blocked else 'completed')
    if kind == 'quarantined':
        assert restarted.get_job(job['job_id'])['error_code'] == 'INTERNALIZATION_RESTORE_REAUTHORIZATION_REQUIRED'
    with db.connect() as c:
        rows = c.execute("SELECT * FROM audit_event WHERE action='curriculum_internalization_authority_v1'").fetchall()
        assert [dict(row) for row in rows] == ([] if kind == 'missing' else [original])
    assert str(dispatch_case[3]) not in original['summary_json']
    assert '小数' not in original['summary_json']
    assert len(original['summary_json']) < 1400


def test_two_live_runners_only_claim_original_batch_once(dispatch_case, normal_provider_owner):
    service, posts = make_service(dispatch_case)
    job = create(service, dispatch_case[1])
    real = service.gateway.complete

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()

        async def held(request):
            entered.set()
            await release.wait()
            return await real(request)

        service.gateway.complete = held
        first = asyncio.create_task(service.run_job(job['job_id']))
        await asyncio.wait_for(entered.wait(), 3)
        await service.run_job(job['job_id'])
        assert service.get_job(job['job_id'])['status'] == 'processing'
        release.set()
        await first

    asyncio.run(run())
    assert len(posts) == 1
    assert service.get_job(job['job_id'])['processed_batches'] == 1


def test_deleted_authority_during_result_cannot_restore_capability(dispatch_case, normal_provider_owner):
    db = dispatch_case[0]

    def erased():
        with db.connect() as c:
            c.execute("DELETE FROM audit_event WHERE child_id=? AND action='curriculum_internalization_authority_v1'", (CHILD,))
            c.commit()

    service, posts = make_service(dispatch_case, erased)
    job = create(service, dispatch_case[1])
    asyncio.run(service.run_job(job['job_id']))
    assert len(posts) == 1
    assert service.get_job(job['job_id'])['status'] == 'failed'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM curriculum_capability').fetchone()[0] == 0


def test_save_guard_rechecks_after_parser_and_rolls_back(dispatch_case, monkeypatch, normal_provider_owner):
    service, posts = make_service(dispatch_case)
    job = create(service, dispatch_case[1])
    real = service._parse_and_validate

    def changed(*args):
        result = real(*args)
        change(dispatch_case, service, 'consent_cycle')
        return result

    monkeypatch.setattr(service, '_parse_and_validate', changed)
    asyncio.run(service.run_job(job['job_id']))
    assert len(posts) == 1
    with dispatch_case[0].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM curriculum_capability').fetchone()[0] == 0
        assert c.execute('SELECT processed_batches FROM curriculum_internalization_job').fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['topic_kind', 'resource_link', 'standard_alignment', 'consent_policy', 'child_grade'])
def test_full_local_source_basis_not_only_excerpt_is_frozen(dispatch_case, mutation, normal_provider_owner):
    db = dispatch_case[0]
    service, posts = make_service(dispatch_case)
    job = create(service, dispatch_case[1])
    sql = {
        'topic_kind': "UPDATE curriculum_topic SET lesson_kind='experiment'",
        'resource_link': 'DELETE FROM curriculum_resource_topic',
        'standard_alignment': "UPDATE curriculum_topic_standard_candidate SET matched_term=matched_term||' revised'",
        'consent_policy': "UPDATE consent_record SET policy_version=policy_version||'-new' WHERE scope='cloud_model_text_allowed'",
        'child_grade': "UPDATE child_profile SET grade='五年级' WHERE child_id='child-primary-4'",
    }[mutation]
    with db.connect() as c:
        changed = c.execute(sql).rowcount
        assert changed > 0
        c.commit()
    asyncio.run(service.run_job(job['job_id']))
    assert not posts and service.get_job(job['job_id'])['status'] == 'failed'


def test_late_callback_remains_bound_to_original_completed_batch(dispatch_case, monkeypatch, normal_provider_owner):
    from home_study_agent import curriculum_internalization as module
    monkeypatch.setattr(module, 'MAX_TOPICS_PER_BATCH', 1)
    db = dispatch_case[0]
    with db.connect() as c:
        row = dict(c.execute("SELECT * FROM curriculum_topic WHERE grade_title='四年级' AND subject_title='小学数学' AND semester='下册' LIMIT 1").fetchone())
        old_id = row['topic_id']
        row.update(topic_id='isolated-next-topic', title='独立检查小数加法', normalized_title='独立检查小数加法')
        columns = ','.join(row)
        c.execute('INSERT INTO curriculum_topic('+columns+') VALUES('+','.join('?' for _ in row)+')', tuple(row.values()))
        c.execute("INSERT INTO curriculum_resource_topic SELECT row_id,?,relation_type FROM curriculum_resource_topic WHERE topic_id=?",
            (row['topic_id'], old_id))
        c.commit()
    service, posts = make_service(dispatch_case)
    job = create(service, dispatch_case[1])
    assert job['total_batches'] == 2
    real, callbacks, rejected = service.gateway.complete, [], []

    async def request_recording(request):
        if callbacks:
            try:
                callbacks[0]()
            except module.InternalizationAuthorityError:
                rejected.append(True)
        callbacks.append(request.pre_dispatch)
        return await real(request)

    monkeypatch.setattr(service.gateway, 'complete', request_recording)
    asyncio.run(service.run_job(job['job_id']))
    assert len(posts) == 2 and service.get_job(job['job_id'])['status'] == 'completed'
    assert rejected == [True]
