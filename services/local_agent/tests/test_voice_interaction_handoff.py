"""A durable, exact tutorial deferral precedes one owned Agent handoff."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import importlib
import json
import re
import sqlite3

from fastapi.testclient import TestClient
import pytest

from test_cloud_voice_interaction import setup, request, Gateway
from home_study_agent.app import create_app
from home_study_agent.cloud_voice_interaction import CloudVoiceInteractionService, VoiceInteractionConflictError
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from home_study_agent.learning_input_revision import LearningInputRevisionService
from home_study_agent.agent_request_ownership import (
    claim, permit_local_retry, voice_interaction_handoff_admission, AgentRequestOwnershipError,
)


def source_key(action='handoff-source'):
    return 'voice-interaction:' + hashlib.sha256(action.encode()).hexdigest()


@pytest.fixture
def handoff(setup, tmp_path):
    voice, gateway, database, settings = setup
    profile = tmp_path / 'desktop-profile'
    profile.mkdir()
    settings = replace(settings, onboarding_profile_dir=profile)
    voice = CloudVoiceInteractionService(voice.repository, gateway, settings, voice.cloud_text_allowed)
    LearningInputRevisionService(database).initialize()
    module = importlib.import_module('home_study_agent.voice_interaction_handoff')
    service = module.VoiceInteractionHandoffService(voice)
    return service, voice, gateway, database, settings


def prepare(service, action='handoff-source'):
    module = importlib.import_module('home_study_agent.voice_interaction_handoff')
    return service.prepare('session-a', module.VoiceInteractionHandoffPrepare(
        child_id='child-primary-4', original_action_key=source_key(action)))


def read(service, child='child-primary-4', session='session-a'):
    module = importlib.import_module('home_study_agent.voice_interaction_handoff')
    return service.read(session, module.VoiceInteractionHandoffRead(child_id=child))


def invalidate(service):
    module = importlib.import_module('home_study_agent.voice_interaction_handoff')
    return service.invalidate('session-a', module.VoiceInteractionHandoffRead(child_id='child-primary-4'))


def interpreted(voice, gateway, *, action='handoff-source', transcript='教程先放一边，先帮我学分数', **context):
    gateway.output = {'route': 'agent', 'action': 'none', 'response_text': '我们接着处理刚才的问题。'}
    payload = request(transcript, context.pop('phase', 'naming'), action, **context)
    assert asyncio.run(voice.interpret('session-a', payload)).route == 'agent'
    return payload


def save_deferred(settings, envelope):
    directory = settings.onboarding_profile_dir / 'voice-onboarding'
    directory.mkdir(exist_ok=True)
    progress = envelope.deferred_progress.model_dump()
    (directory / 'child-primary-4.json').write_text(json.dumps(progress))


def admit(service, database, prepared, original):
    with voice_interaction_handoff_admission(lambda c: service.authorize(c, 'session-a',
            prepared.handoff_id, prepared.agent_action_id, original.transcript, original.voice_input_id)):
        return claim(database, 'session-a', prepared.agent_action_id, original.transcript,
                     voice_input_id=original.voice_input_id, allow_local_resume=True)


def test_http_handoff_prepared_before_deferral_survives_service_restart(tmp_path):
    profile = tmp_path / 'profile'
    profile.mkdir()
    settings = Settings(data_dir=tmp_path / 'data', database_path=tmp_path / 'data' / 'http.db',
        onboarding_profile_dir=profile, maintenance_enabled=False)
    gateway = Gateway({'route': 'agent', 'action': 'none', 'response_text': '我们处理刚才的问题。'})
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        sid = client.post('/v1/sessions', json={'child_id': 'child-primary-4', 'client_action_id': 'handoff-http'}).json()['session_id']
        base = f'/v1/sessions/{sid}/voice-interaction'
        response = client.post(base, json=request('先帮我学分数', action_id='handoff-source').model_dump())
        assert response.status_code == 200
        recovery = client.post(base + '/recovery/read', json={'child_id': 'child-primary-4', 'context': {'phase': 'naming'}})
        assert recovery.status_code == 200
        assert recovery.json()['request'] == request('先帮我学分数', action_id='handoff-source').model_dump(exclude_none=True)
        prepared = client.post(base + '/recovery/handoff/prepare', json={
            'child_id': 'child-primary-4', 'original_action_key': source_key()})
        assert prepared.status_code == 200
        assert prepared.headers['cache-control'] == 'no-store'
        value = prepared.json()
        assert value['status'] == 'needs_deferral' and value['request'] is None
        assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', value['deferred_progress']['updatedAt'])
        directory = profile / 'voice-onboarding'
        directory.mkdir()
        (directory / 'child-primary-4.json').write_text(json.dumps(value['deferred_progress']))
        assert client.get(base + '/recovery/handoff/read').status_code == 405
        assert client.post(base + '/recovery/handoff/read', json={'child_id': 'child-primary-4', 'transcript': 'forbidden'}).status_code == 422
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        ready = client.post(base + '/recovery/handoff/read', json={'child_id': 'child-primary-4'})
        assert ready.status_code == 200 and ready.headers['cache-control'] == 'no-store'
        assert ready.json()['status'] == 'ready'
        assert ready.json()['request']['transcript'] == '先帮我学分数'
        assert 'voice_input_id' not in ready.json()['request']
        assert ready.json()['request']['context'] == {'phase': 'naming'}
        assert ready.json()['agent_action_id'] == value['agent_action_id']
        assert client.post(base + '/recovery/handoff/read', json={'child_id': 'child-junior-3'}).status_code == 409
        stopped = client.post(base + '/recovery/handoff/invalidate', json={'child_id': 'child-primary-4'})
        assert stopped.status_code == 200 and stopped.json()['status'] == 'invalidated'
        assert stopped.headers['cache-control'] == 'no-store'
    assert len(gateway.requests) == 1


def test_prepare_is_durable_idempotent_and_stores_no_original_text(handoff):
    service, voice, gateway, database, settings = handoff
    original = interpreted(voice, gateway)
    first = prepare(service)
    assert first.status == 'needs_deferral' and first.request is None
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,160}', first.agent_action_id)
    assert prepare(service) == first
    with database.connect() as c:
        audit = c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?', (first.handoff_id,)).fetchone()[0]
        assert original.transcript not in audit and '小星' not in audit
        assert 'request' not in json.loads(audit)
    reopened = importlib.import_module('home_study_agent.voice_interaction_handoff').VoiceInteractionHandoffService(
        CloudVoiceInteractionService(StudyRepository(Database(settings.database_path)), gateway, settings, voice.cloud_text_allowed))
    assert read(reopened) == first
    save_deferred(settings, first)
    ready = read(reopened)
    assert ready.status == 'ready' and ready.request == original
    assert len(gateway.requests) == 1


def test_only_exact_intended_deferred_record_authorizes_original(handoff):
    service, voice, gateway, _, settings = handoff
    interpreted(voice, gateway)
    prepared = prepare(service)
    save_deferred(settings, prepared)
    path = settings.onboarding_profile_dir / 'voice-onboarding' / 'child-primary-4.json'
    changed = json.loads(path.read_text())
    changed['updatedAt'] = '2026-01-01T00:00:00.000Z'
    path.write_text(json.dumps(changed))
    invalid = read(service)
    assert invalid.status == 'invalidated' and invalid.request is None
    save_deferred(settings, prepared)
    assert read(service).status == 'invalidated'
    assert prepare(service).status == 'invalidated'


@pytest.mark.parametrize('mutation', ['consent', 'reconsent', 'wake', 'phase', 'new_voice', 'new_agent', 'new_claim', 'archived', 'ended', 'deletion'])
def test_different_scope_invalidates_without_model_or_original_disclosure(handoff, mutation):
    service, voice, gateway, database, settings = handoff
    interpreted(voice, gateway)
    prepared = prepare(service)
    save_deferred(settings, prepared)
    with database.connect() as c:
        if mutation == 'consent':
            c.execute('UPDATE privacy_preference SET cloud_model_text_allowed=0')
        elif mutation == 'new_agent':
            c.execute("INSERT INTO agent_turn VALUES ('other','child-primary-4','session-a','different input','none','reply','{}','test','completed',NULL,NULL,datetime('now'),'other-action')")
        elif mutation == 'archived':
            c.execute("UPDATE child_profile SET status='archived' WHERE child_id='child-primary-4'")
        elif mutation == 'ended':
            c.execute("UPDATE daily_session SET status='completed' WHERE session_id='session-a'")
        elif mutation == 'deletion':
            c.execute("INSERT INTO deletion_job(deletion_id,child_id,scope,status,created_at,updated_at) VALUES ('d','child-primary-4','media','pending',datetime('now'),datetime('now'))")
        c.commit()
    if mutation == 'reconsent':
        privacy = PrivacyLifecycleService(database, settings.data_dir)
        privacy.update_preferences({'cloud_model_text_allowed': False})
        privacy.update_preferences({'cloud_model_text_allowed': True})
    elif mutation == 'wake':
        (settings.onboarding_profile_dir / 'companion-wake-words.json').write_text(json.dumps([{
            'childId': 'child-primary-4', 'wakeWord': '小星', 'enabled': True, 'updatedAt': '2026-01-01T00:00:00Z'}]))
    elif mutation == 'phase':
        path = settings.onboarding_profile_dir / 'voice-onboarding' / 'child-primary-4.json'
        changed = json.loads(path.read_text())
        changed['step'] = 'completed'
        path.write_text(json.dumps(changed))
    elif mutation == 'new_voice':
        interpreted(voice, gateway, action='different-voice')
    elif mutation == 'new_claim':
        claim(database, 'session-a', 'different-agent-claim', 'a different original input')
    invalid = read(service)
    assert invalid.status == 'invalidated' and invalid.request is None
    assert len(gateway.requests) == (2 if mutation == 'new_voice' else 1)


def test_explicit_invalidation_before_prepare_blocks_late_response_revival(handoff):
    service, voice, gateway, _, _ = handoff
    interpreted(voice, gateway)
    assert invalidate(service).status == 'invalidated'
    assert prepare(service).status == 'invalidated'


def test_concurrent_prepares_share_timestamp_and_one_pointer(handoff):
    service, voice, gateway, database, _ = handoff
    interpreted(voice, gateway)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: prepare(service), range(4)))
    assert all(result == results[0] for result in results)
    with database.connect() as c:
        assert c.execute('SELECT count(*) FROM audit_event WHERE audit_id=?', (results[0].handoff_id,)).fetchone()[0] == 1


def test_existing_owned_claim_is_unknown_and_cannot_charge_again(handoff):
    service, voice, gateway, database, settings = handoff
    original = interpreted(voice, gateway)
    prepared = prepare(service)
    save_deferred(settings, prepared)
    assert admit(service, database, prepared, original)
    unknown = read(service)
    assert unknown.status == 'outcome_unknown' and unknown.request is None
    with pytest.raises(AgentRequestOwnershipError):
        admit(service, database, prepared, original)
    permit_local_retry(database, 'session-a', prepared.agent_action_id, None)
    assert read(service).status == 'outcome_unknown'
    with pytest.raises(AgentRequestOwnershipError):
        admit(service, database, prepared, original)
    assert read(service).status == 'outcome_unknown'
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('tool_status,expected', [('pending', 'interpreted'), ('completed', 'completed'), ('failed', 'failed')])
def test_existing_agent_receipt_replaces_replay_authority(handoff, tool_status, expected):
    service, voice, gateway, database, settings = handoff
    original = interpreted(voice, gateway)
    prepared = prepare(service)
    save_deferred(settings, prepared)
    with database.connect() as c:
        c.execute("INSERT INTO agent_turn VALUES ('owned','child-primary-4','session-a',?,'none','reply','{\"type\":\"none\"}','test',?,NULL,NULL,datetime('now'),?)",
            (original.transcript, tool_status, prepared.agent_action_id))
        c.commit()
    result = read(service)
    assert result.status == expected and result.request is None
    assert result.receipt.client_action_id == prepared.agent_action_id
    assert result.receipt.replay_allowed is False


def test_original_asr_binding_is_preserved_and_later_deletion_invalidates(handoff):
    service, voice, gateway, database, settings = handoff
    with database.connect() as c:
        c.execute("INSERT INTO voice_input(voice_input_id,child_id,session_id,purpose,transcript,model_version,status,created_at,client_action_id) VALUES ('original-asr','child-primary-4','session-a','wake_word_setup','先帮我学分数','test','completed',datetime('now'),'original-asr-action')")
        c.commit()
    gateway.output = {'route': 'agent', 'action': 'none', 'response_text': '接着处理这个问题。'}
    original = request('先帮我学分数', action_id='handoff-source').model_copy(update={'voice_input_id': 'original-asr'})
    asyncio.run(voice.interpret('session-a', original))
    prepared = prepare(service)
    save_deferred(settings, prepared)
    assert read(service).request.voice_input_id == 'original-asr'
    with database.connect() as c:
        c.execute("DELETE FROM voice_input WHERE voice_input_id='original-asr'")
        c.commit()
    assert read(service).status == 'invalidated'


def test_non_agent_or_daily_rename_source_cannot_prepare_handoff(handoff):
    service, voice, gateway, _, settings = handoff
    asyncio.run(voice.interpret('session-a', request(action_id='handoff-source')))
    with pytest.raises(VoiceInteractionConflictError):
        prepare(service)
    directory = settings.onboarding_profile_dir / 'voice-onboarding'
    directory.mkdir()
    (directory / 'child-primary-4.json').write_text(json.dumps({'childId': 'child-primary-4', 'version': 1,
        'step': 'completed', 'updatedAt': '2026-01-01T00:00:00Z'}))
    interpreted(voice, gateway, action='daily-source', step='completed')
    with pytest.raises(VoiceInteractionConflictError):
        prepare(service, 'daily-source')


def test_agent_route_during_name_confirmation_proves_request_not_name_mutation(handoff):
    service, voice, gateway, _, settings = handoff
    asyncio.run(voice.interpret('session-a', request(action_id='candidate')))
    original = interpreted(voice, gateway, phase='confirm_name', pending_name='小星')
    prepared = prepare(service)
    save_deferred(settings, prepared)
    assert read(service).request == original


def test_locked_admission_rejects_intervening_input_after_ready_read(handoff):
    service, voice, gateway, database, settings = handoff
    original = interpreted(voice, gateway)
    prepared = prepare(service)
    save_deferred(settings, prepared)
    assert read(service).status == 'ready'
    interpreted(voice, gateway, action='later-input')
    with pytest.raises(AgentRequestOwnershipError):
        admit(service, database, prepared, original)
    with database.connect() as c:
        assert not c.execute("SELECT 1 FROM audit_event WHERE action='agent_request_handoff_claimed'").fetchone()
    assert read(service).status == 'invalidated'


def test_concurrent_locked_admission_consumes_once_and_reserved_id_cannot_bypass(handoff):
    service, voice, gateway, database, settings = handoff
    original = interpreted(voice, gateway)
    prepared = prepare(service)
    save_deferred(settings, prepared)
    with pytest.raises(AgentRequestOwnershipError):
        claim(database, 'session-a', prepared.agent_action_id, original.transcript)
    def run(_):
        try:
            return admit(service, database, prepared, original)
        except AgentRequestOwnershipError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(run, range(4))).count(True) == 1
    assert read(service).status == 'outcome_unknown'
    assert len(gateway.requests) == 1


def test_http_agent_dispatch_requires_locked_original_proof_and_never_replays(handoff):
    service, voice, gateway, database, settings = handoff
    original = interpreted(voice, gateway, transcript='你好')
    prepared = prepare(service)
    save_deferred(settings, prepared)
    # Exercise actual HTTP admission and Agent persistence. The bounded public
    # gateway stub supplies the initial voice decision and first classifier;
    # repeated/invalid proof must never reach either model path.
    with TestClient(create_app(replace(settings, agent_runtime_enabled=False), model_gateway=gateway)) as client:
        base = '/v1/sessions/session-a'
        payload = {'transcript': original.transcript, 'client_action_id': prepared.agent_action_id}
        assert client.post(base + '/agent-turn', json=payload).status_code == 409
        payload['voice_interaction_handoff_id'] = prepared.handoff_id
        wrong = client.post(base + '/agent-turn', json={**payload, 'transcript': 'a different request'})
        assert wrong.status_code == 409
        assert len(gateway.requests) == 1
        first = client.post(base + '/agent-turn', json=payload)
        assert first.status_code == 200
        assert first.json()['transcript'] == original.transcript
        calls_after_first = len(gateway.requests)
        assert calls_after_first == 2
        duplicate = client.post(base + '/agent-turn', json=payload)
        assert duplicate.status_code == 409
        receipt = client.post(base + '/voice-interaction/recovery/handoff/read', json={'child_id': 'child-primary-4'})
        assert receipt.status_code == 200 and receipt.json()['status'] in {'interpreted', 'completed'}
        assert receipt.json()['request'] is None and receipt.json()['receipt']['replay_allowed'] is False
    with database.connect() as c:
        assert c.execute('SELECT count(*) FROM agent_turn WHERE client_action_id=?', (prepared.agent_action_id,)).fetchone()[0] == 1
    assert len(gateway.requests) == calls_after_first


def test_received_arrival_is_reported_without_original_replay(handoff):
    service, voice, gateway, database, settings = handoff
    original = interpreted(voice, gateway)
    prepared = prepare(service)
    save_deferred(settings, prepared)
    assert admit(service, database, prepared, original)
    with database.connect() as c:
        c.execute("INSERT INTO learning_input_arrival(arrival_id,child_id,session_id,client_action_id,utterance,snapshot_json,created_at) VALUES ('arrival','child-primary-4','session-a',?,?,'{}',datetime('now'))",
            (prepared.agent_action_id, original.transcript))
        c.commit()
    result = read(service)
    assert result.status == 'received' and result.request is None
    assert result.receipt.status == 'received'


@pytest.mark.parametrize('explicitly_invalidated', [False, True])
def test_database_restore_quarantines_prepared_handoff_and_preserves_live_stop(handoff, tmp_path, explicitly_invalidated):
    service, voice, gateway, database, settings = handoff
    original = interpreted(voice, gateway)
    prepared = prepare(service)
    save_deferred(settings, prepared)
    assert read(service).status == 'ready'
    snapshot = tmp_path / 'before-stop.db'
    with database.connect() as source:
        with sqlite3.connect(snapshot) as target:
            source.backup(target)
    live_stop = None
    if explicitly_invalidated:
        assert invalidate(service).status == 'invalidated'
        with database.connect() as c:
            live_stop = tuple(c.execute("SELECT * FROM audit_event WHERE action='voice_interaction_handoff_invalidated'").fetchone())
    restored = PrivacyLifecycleService(database, settings.data_dir)._restore_database_snapshot(snapshot)
    assert restored == 'ok'
    quarantined = read(service)
    assert quarantined.status == 'invalidated' and quarantined.request is None
    with pytest.raises(AgentRequestOwnershipError):
        admit(service, database, prepared, original)
    if live_stop:
        with database.connect() as c:
            assert tuple(c.execute('SELECT * FROM audit_event WHERE audit_id=?', (live_stop[0],)).fetchone()) == live_stop
    assert len(gateway.requests) == 1


@pytest.mark.parametrize('intervening', ['claim', 'arrival', 'turn', 'during_model'])
def test_prepare_cannot_swallow_different_input_since_original_source_creation(handoff, intervening):
    service, voice, gateway, database, _ = handoff
    if intervening == 'during_model':
        async def other_claim():
            claim(database, 'session-a', 'intervening-action', 'a different learning request')
        gateway.hook = other_claim
    interpreted(voice, gateway)
    if intervening == 'claim':
        claim(database, 'session-a', 'intervening-action', 'a different learning request')
    elif intervening in {'arrival', 'turn'}:
        with database.connect() as c:
            if intervening == 'arrival':
                c.execute("INSERT INTO learning_input_arrival(arrival_id,child_id,session_id,client_action_id,utterance,snapshot_json,created_at) VALUES ('other-arrival','child-primary-4','session-a','intervening-action','other input','{}',datetime('now'))")
            else:
                c.execute("INSERT INTO agent_turn VALUES ('other-turn','child-primary-4','session-a','other input','none','reply','{}','test','completed',NULL,NULL,datetime('now'),'intervening-action')")
            c.commit()
    result = prepare(service)
    assert result.status == 'invalidated' and result.request is None
    assert len(gateway.requests) == 1


def test_legacy_source_without_baseline_keeps_normal_voice_recovery_but_cannot_handoff(handoff):
    from home_study_agent.cloud_voice_interaction import VoiceInteractionRecoveryRead
    service, voice, gateway, database, _ = handoff
    payload = interpreted(voice, gateway)
    with database.connect() as c:
        row = c.execute('SELECT summary_json FROM audit_event WHERE audit_id=?', (source_key(),)).fetchone()
        summary = json.loads(row[0])
        summary.pop('agent_handoff_baseline', None)
        c.execute('UPDATE audit_event SET summary_json=? WHERE audit_id=?', (json.dumps(summary), source_key()))
        c.commit()
    assert asyncio.run(voice.interpret('session-a', payload)).route == 'agent'
    assert voice.read_recovery('session-a', VoiceInteractionRecoveryRead(child_id='child-primary-4', context={'phase': 'naming'})).status == 'completed'
    with pytest.raises(VoiceInteractionConflictError):
        prepare(service)
    assert len(gateway.requests) == 1


def test_backup_from_before_prepare_cannot_erase_live_handoff_quarantine(handoff, tmp_path):
    service, voice, gateway, database, settings = handoff
    interpreted(voice, gateway)
    snapshot = tmp_path / 'before-prepare.db'
    with database.connect() as source:
        with sqlite3.connect(snapshot) as target:
            source.backup(target)
    prepared = prepare(service)
    save_deferred(settings, prepared)
    assert read(service).status == 'ready'
    assert PrivacyLifecycleService(database, settings.data_dir)._restore_database_snapshot(snapshot) == 'ok'
    assert read(service).status == 'none'
    assert prepare(service).status == 'invalidated'
    assert len(gateway.requests) == 1
