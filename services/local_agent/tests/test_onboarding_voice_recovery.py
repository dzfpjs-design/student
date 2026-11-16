"""Explicit onboarding recovery uses saved speech, never automatic rebilling."""
import asyncio
from dataclasses import replace
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from test_cloud_voice_interaction import setup, request, Gateway
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.cloud_voice_interaction import (
    CloudVoiceInteractionService, VoiceInteractionConflictError,
    VoiceInteractionRecoveryRead, VoiceInteractionRecoveryRetry,
)
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService


@pytest.fixture
def recovery(setup, tmp_path):
    service, gateway, database, settings = setup
    profile = tmp_path / 'desktop'
    profile.mkdir()
    settings = replace(settings, onboarding_profile_dir=profile)
    service = CloudVoiceInteractionService(service.repository, gateway, settings, service.cloud_text_allowed)
    return service, gateway, database, settings


def read_body(context=None, child='child-primary-4'):
    return VoiceInteractionRecoveryRead(child_id=child, context=context or {'phase': 'naming'})


def retry_body(key, action='explicit-retry-1', context=None, child='child-primary-4'):
    return VoiceInteractionRecoveryRetry(**read_body(context, child).model_dump(),
        original_action_key=key, client_action_id=action)


def fail_once(service, gateway):
    gateway.output = RuntimeError('untrusted provider text')
    assert asyncio.run(service.interpret('session-a', request())).source == 'unavailable'
    return service.read_recovery('session-a', read_body())


def test_request_is_saved_before_model_and_read_survives_backend_restart(recovery):
    service, gateway, database, settings = recovery
    async def inspect_saved():
        with database.connect() as c:
            saved = json.loads(c.execute('SELECT request_json FROM cloud_voice_interaction').fetchone()[0])
        assert saved == request().model_dump(exclude_none=True)
    gateway.hook = inspect_saved
    failed = fail_once(service, gateway)
    assert failed.status == 'retryable' and failed.reason == 'VOICE_INTERACTION_GATEWAY_OR_INTERNAL_FAILED'
    reopened = CloudVoiceInteractionService(StudyRepository(Database(settings.database_path)), gateway, settings,
        service.cloud_text_allowed)
    assert reopened.read_recovery('session-a', read_body()) == failed
    assert failed.request.transcript == request().transcript
    assert len(gateway.requests) == 1


def test_explicit_retry_uses_original_and_duplicate_parent_has_one_attempt(recovery):
    service, gateway, database, _ = recovery
    original = fail_once(service, gateway)
    gateway.output = {'route': 'onboarding', 'action': 'name_candidate', 'candidate': '小星', 'response_text': '叫我小星，对吗？'}
    first = asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key)))
    duplicate = asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key, 'different-control-id')))
    assert first == duplicate and first.status == 'completed'
    assert first.request.transcript == request().transcript
    assert first.original_action_key != original.original_action_key
    assert len(gateway.requests) == 2
    with database.connect() as c:
        assert c.execute('SELECT count(*) FROM cloud_voice_interaction WHERE retry_parent_key=?',
            (original.original_action_key,)).fetchone()[0] == 1


def test_latest_failed_attempt_can_retry_but_old_parent_does_not_create_grandchild(recovery):
    service, gateway, _, _ = recovery
    original = fail_once(service, gateway)
    child = asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key)))
    grandchild = asyncio.run(service.retry_recovery('session-a', retry_body(child.original_action_key, 'retry-2')))
    assert len(gateway.requests) == 3
    assert asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key, 'old-duplicate'))) == child
    assert service.read_recovery('session-a', read_body()) == grandchild
    assert len(gateway.requests) == 3


def test_concurrent_retry_waits_on_same_attempt_without_second_dispatch(recovery):
    service, gateway, _, _ = recovery
    original = fail_once(service, gateway)
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def hold():
            started.set()
            await release.wait()
        gateway.hook = hold
        first = asyncio.create_task(service.retry_recovery('session-a', retry_body(original.original_action_key)))
        await started.wait()
        pending = await service.retry_recovery('session-a', retry_body(original.original_action_key, 'duplicate'))
        assert pending.status == 'pending'
        assert len(gateway.requests) == 2
        release.set()
        await first
    asyncio.run(run())


def test_abandoned_pending_or_unknown_never_resends(recovery):
    service, gateway, database, _ = recovery
    service._claim('session-a', request())
    pending = service.read_recovery('session-a', read_body())
    assert pending.status == 'pending'
    assert asyncio.run(service.retry_recovery('session-a', retry_body(pending.original_action_key))).status == 'pending'
    with database.connect() as c:
        c.execute("UPDATE cloud_voice_interaction SET created_at='2020-01-01T00:00:00+00:00'")
        c.commit()
    assert service.read_recovery('session-a', read_body()).status == 'outcome_unknown'
    assert asyncio.run(service.retry_recovery('session-a', retry_body(pending.original_action_key))).status == 'outcome_unknown'
    assert not gateway.requests


@pytest.mark.parametrize('change', ['child', 'ended', 'consent', 'deletion', 'profile', 'context'])
def test_scope_changes_block_read_retry_and_model_dispatch(recovery, change):
    service, gateway, database, settings = recovery
    original = fail_once(service, gateway)
    body = read_body()
    if change == 'child':
        body = read_body(child='child-junior-3')
    elif change == 'context':
        body = read_body({'phase': 'tutorial', 'step': 'study_practice', 'wake_word': '小星'})
    elif change == 'profile':
        (settings.onboarding_profile_dir / 'voice-onboarding').mkdir()
        (settings.onboarding_profile_dir / 'voice-onboarding' / 'child-primary-4.json').write_text(json.dumps({
            'childId': 'child-primary-4', 'version': 1, 'step': 'completed', 'updatedAt': '2026-10-15T00:00:00Z'}))
    else:
        with database.connect() as c:
            if change == 'ended':
                c.execute("UPDATE daily_session SET status='completed' WHERE session_id='session-a'")
            elif change == 'consent':
                c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
            else:
                c.execute("UPDATE child_profile SET status='archived' WHERE child_id='child-primary-4'")
            c.commit()
    with pytest.raises(VoiceInteractionConflictError):
        service.read_recovery('session-a', body)
    with pytest.raises(VoiceInteractionConflictError):
        asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key,
            context=body.context.model_dump(exclude_none=True), child=body.child_id)))
    assert len(gateway.requests) == 1


def test_original_storage_failure_prevents_external_request(recovery):
    service, gateway, database, _ = recovery
    with database.connect() as c:
        c.execute("CREATE TRIGGER reject_request BEFORE INSERT ON cloud_voice_interaction BEGIN SELECT RAISE(FAIL, 'owned fixture'); END")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(service.interpret('session-a', request()))
    assert not gateway.requests


def test_successful_original_only_replays_without_new_charge(recovery):
    service, gateway, _, _ = recovery
    response = asyncio.run(service.interpret('session-a', request()))
    original = service.read_recovery('session-a', read_body())
    assert original.status == 'completed' and original.response == response
    assert asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key))) == original
    assert len(gateway.requests) == 1


def test_real_persisted_consent_version_is_recoverable_but_reconsent_is_not(recovery):
    service, gateway, database, settings = recovery
    privacy = PrivacyLifecycleService(database, settings.data_dir)
    privacy.update_preferences({'cloud_model_text_allowed': False})
    privacy.update_preferences({'cloud_model_text_allowed': True})
    original = fail_once(service, gateway)
    assert original.status == 'retryable'
    privacy.update_preferences({'cloud_model_text_allowed': False})
    privacy.update_preferences({'cloud_model_text_allowed': True})
    with pytest.raises(VoiceInteractionConflictError, match='CONTEXT_CHANGED'):
        service.read_recovery('session-a', read_body())


def test_actual_model_timeout_can_be_retried_only_after_explicit_new_attempt(recovery, monkeypatch):
    service, gateway, _, _ = recovery
    monkeypatch.setattr('home_study_agent.cloud_voice_interaction.CLOUD_TIMEOUT_SECONDS', 0.01)
    async def slow():
        await asyncio.sleep(1)
    gateway.hook = slow
    first = asyncio.run(service.interpret('session-a', request()))
    assert first.source == 'unavailable'
    original = service.read_recovery('session-a', read_body())
    assert original.reason == 'VOICE_INTERACTION_TIMEOUT' and original.status == 'retryable'
    assert asyncio.run(service.interpret('session-a', request())) == first
    assert len(gateway.requests) == 1
    gateway.hook = None
    assert asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key))).status == 'completed'
    assert len(gateway.requests) == 2


def add_voice(database, *, child='child-primary-4', session='session-a', text=None, status='completed'):
    with database.connect() as c:
        c.execute('INSERT INTO voice_input(voice_input_id,child_id,session_id,purpose,transcript,model_version,status,created_at,client_action_id) '
                  'VALUES (?,?,?,\'wake_word_setup\',?,\'test-public\',?,datetime(\'now\'),\'asr-original\')',
                  ('voice-original', child, session, text or request().transcript, status))
        c.commit()


def test_original_asr_binding_survives_retry_and_never_uses_retry_control_asr(recovery):
    service, gateway, database, _ = recovery
    add_voice(database)
    payload = request().model_copy(update={'voice_input_id': 'voice-original'})
    gateway.output = RuntimeError('public fixture failure')
    asyncio.run(service.interpret('session-a', payload))
    original = service.read_recovery('session-a', read_body())
    gateway.output = {'route': 'agent', 'action': 'none', 'response_text': '接着看看原来的问题。'}
    result = asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key)))
    assert result.request.voice_input_id == 'voice-original'
    assert result.request.transcript == payload.transcript
    assert result.response.route == 'agent'


@pytest.mark.parametrize('mutation', ['foreign_child', 'foreign_session', 'different_text', 'ignored', 'deleted_after_failure'])
def test_invalid_original_asr_never_reaches_model_or_recovers(recovery, mutation):
    service, gateway, database, _ = recovery
    add_voice(database,
        child='child-junior-3' if mutation == 'foreign_child' else 'child-primary-4',
        session='session-b' if mutation == 'foreign_session' else 'session-a',
        text='another public phrase' if mutation == 'different_text' else None,
        status='ignored' if mutation == 'ignored' else 'completed')
    payload = request().model_copy(update={'voice_input_id': 'voice-original'})
    if mutation == 'deleted_after_failure':
        gateway.output = RuntimeError('fixture')
        asyncio.run(service.interpret('session-a', payload))
        with database.connect() as c:
            c.execute("DELETE FROM voice_input WHERE voice_input_id='voice-original'")
            c.commit()
        with pytest.raises(VoiceInteractionConflictError):
            service.read_recovery('session-a', read_body())
        assert len(gateway.requests) == 1
    else:
        with pytest.raises(VoiceInteractionConflictError):
            asyncio.run(service.interpret('session-a', payload))
        assert not gateway.requests


def test_confirm_name_context_hydrates_only_from_durable_same_child_candidate(recovery):
    service, gateway, _, _ = recovery
    asyncio.run(service.interpret('session-a', request()))
    gateway.output = RuntimeError('fixture')
    confirmation = request('就用这个名字', 'confirm_name', 'confirm-original', pending_name='小星')
    asyncio.run(service.interpret('session-a', confirmation))
    restored = service.read_recovery('session-a', read_body())
    assert restored.request.context == confirmation.context
    gateway.output = {'route': 'onboarding', 'action': 'confirm_name', 'response_text': '好，叫我小星。'}
    result = asyncio.run(service.retry_recovery('session-a', retry_body(restored.original_action_key,
        context=restored.request.context.model_dump(exclude_none=True))))
    assert result.status == 'completed' and result.response.action == 'confirm_name'


def test_read_after_name_candidate_does_not_offer_previous_phase_as_recovery(recovery):
    service, gateway, _, _ = recovery
    asyncio.run(service.interpret('session-a', request()))
    current = {'phase': 'confirm_name', 'pending_name': '小星'}
    restored = service.read_recovery('session-a', read_body(current))
    assert restored.status == 'none' and restored.request is None
    assert len(gateway.requests) == 1


def test_tutorial_phase_advance_hides_old_recovery_but_cannot_retry_it(recovery):
    service, gateway, _, settings = recovery
    profile = settings.onboarding_profile_dir
    (profile / 'voice-onboarding').mkdir()
    (profile / 'companion-wake-words.json').write_text(json.dumps([{
        'childId': 'child-primary-4', 'wakeWord': '小星', 'enabled': True,
        'updatedAt': '2026-10-15T00:00:00Z'}]))
    progress = profile / 'voice-onboarding' / 'child-primary-4.json'
    def save(step):
        progress.write_text(json.dumps({'childId': 'child-primary-4', 'version': 1,
            'step': step, 'updatedAt': '2026-10-15T00:00:00Z'}))
    save('study_practice')
    old_context = {'phase': 'tutorial', 'step': 'study_practice', 'wake_word': '小星'}
    gateway.output = RuntimeError('public fixture failure')
    asyncio.run(service.interpret('session-a', request('继续', 'tutorial', 'tutorial-original',
        step='study_practice', wake_word='小星')))
    original = service.read_recovery('session-a', read_body(old_context))
    save('document_practice')
    current = {'phase': 'tutorial', 'step': 'document_practice', 'wake_word': '小星'}
    assert service.read_recovery('session-a', read_body(current)).status == 'none'
    with pytest.raises(VoiceInteractionConflictError):
        asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key, context=current)))
    assert len(gateway.requests) == 1


def test_normal_daily_rename_does_not_regress_when_desktop_profile_is_configured(recovery):
    service, gateway, _, settings = recovery
    profile = settings.onboarding_profile_dir
    (profile / 'voice-onboarding').mkdir()
    (profile / 'voice-onboarding' / 'child-primary-4.json').write_text(json.dumps({
        'childId': 'child-primary-4', 'version': 1, 'step': 'completed',
        'updatedAt': '2026-10-15T00:00:00Z'}))
    (profile / 'companion-wake-words.json').write_text(json.dumps([{
        'childId': 'child-primary-4', 'wakeWord': '小知', 'enabled': True,
        'updatedAt': '2026-10-15T00:00:00Z'}]))
    result = asyncio.run(service.interpret('session-a', request('改叫小星', 'naming', 'daily-rename',
        step='completed', wake_word='小知')))
    assert result.source == 'cloud' and result.action == 'name_candidate'
    assert len(gateway.requests) == 1


def test_pending_deletion_and_restore_profile_change_block_recovery(recovery):
    service, gateway, database, settings = recovery
    original = fail_once(service, gateway)
    with database.connect() as c:
        c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,status,created_at,updated_at) "
                  "VALUES ('d','child-primary-4','media','pending',datetime('now'),datetime('now'))")
        c.commit()
    with pytest.raises(VoiceInteractionConflictError):
        service.read_recovery('session-a', read_body())
    with database.connect() as c:
        c.execute("DELETE FROM deletion_job WHERE deletion_id='d'")
        c.commit()
    words = settings.onboarding_profile_dir / 'companion-wake-words.json'
    words.write_text(json.dumps([{'childId': 'child-primary-4', 'wakeWord': '小星', 'enabled': True,
                                 'updatedAt': '2026-10-15T00:00:00Z'}]))
    with pytest.raises(VoiceInteractionConflictError):
        asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key)))
    assert len(gateway.requests) == 1


def test_http_recovery_no_store_restart_and_body_validation(tmp_path):
    profile = tmp_path / 'profile'
    profile.mkdir()
    settings = Settings(data_dir=tmp_path / 'family-data', database_path=tmp_path / 'family-data' / 'http.db',
        onboarding_profile_dir=profile, maintenance_enabled=False)
    gateway = Gateway(RuntimeError('fixture'))
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'start-recovery'}).json()['session_id']
        base = f'/v1/sessions/{sid}/voice-interaction'
        assert client.post(base, json=request().model_dump(exclude_none=True)).status_code == 200
        read = client.post(base + '/recovery/read', json=read_body().model_dump())
        assert read.status_code == 200 and read.headers['cache-control'] == 'no-store'
        key = read.json()['original_action_key']
        assert client.get(base + '/recovery/read').status_code == 405
        assert client.post(base + '/recovery/read', json={'child_id': 'child-primary-4'}).status_code == 422
        assert client.post(base + '/recovery/read', json=read_body(child='child-junior-3').model_dump()).status_code == 409
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        assert client.post(base + '/recovery/read', json=read_body().model_dump()).json() == read.json()
        gateway.output = {'route': 'onboarding', 'action': 'name_candidate', 'candidate': '小星', 'response_text': '叫我小星？'}
        result = client.post(base + '/recovery/retry', json=retry_body(key).model_dump())
        assert result.status_code == 200 and result.json()['status'] == 'completed'
        assert result.headers['cache-control'] == 'no-store'
    assert len(gateway.requests) == 2


def test_legacy_interaction_schema_migrates_without_inventing_original_speech(recovery):
    service, gateway, database, _ = recovery
    with database.connect() as c:
        c.execute('DROP TABLE cloud_voice_interaction')
        c.execute('''CREATE TABLE cloud_voice_interaction (
            action_key TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES daily_session(session_id) ON DELETE CASCADE,
            child_id TEXT NOT NULL REFERENCES child_profile(child_id) ON DELETE CASCADE,
            fingerprint TEXT NOT NULL, phase TEXT NOT NULL, response_json TEXT,
            reason TEXT, created_at TEXT NOT NULL, completed_at TEXT)''')
        c.execute("INSERT INTO cloud_voice_interaction VALUES ('legacy','session-a','child-primary-4',"
                  "'old-fingerprint','naming',NULL,NULL,datetime('now'),NULL)")
        c.commit()
    service.ensure_schema()
    service.ensure_schema()
    with pytest.raises(VoiceInteractionConflictError, match='RECOVERY_UNAVAILABLE'):
        service.read_recovery('session-a', read_body())
    assert not gateway.requests
    original = fail_once(service, gateway)
    assert original.status == 'retryable'


def test_media_deletion_removes_original_speech_and_retry_payloads(recovery):
    service, gateway, database, settings = recovery
    original = fail_once(service, gateway)
    asyncio.run(service.retry_recovery('session-a', retry_body(original.original_action_key)))
    with database.connect() as c:
        assert c.execute('SELECT count(*) FROM cloud_voice_interaction WHERE request_json IS NOT NULL').fetchone()[0] == 2
    PrivacyLifecycleService(database, settings.data_dir).delete_child_scope(
        'child-primary-4', 'media', '确认删除', 'delete-public-recovery')
    with database.connect() as c:
        assert c.execute('SELECT count(*) FROM cloud_voice_interaction').fetchone()[0] == 0
    assert service.read_recovery('session-a', read_body()).status == 'none'
    assert len(gateway.requests) == 2
