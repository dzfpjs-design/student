"""Synthetic ASGI HTTP/SQLite only; no product server, devices, model or family DB."""
from __future__ import annotations
from synthetic_native_restore_fixture import KEY, signed_local_restore_post

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings


def client_for(path, *, native_launch_attestation_key=None):
    return TestClient(create_app(Settings(data_dir=path, database_path=path / "synthetic.db",
        data_encryption_key=b"0" * 32, maintenance_enabled=False, agent_runtime_enabled=False, native_launch_attestation_key=native_launch_attestation_key)))


def create_session(client, action="synthetic-intent-session"):
    value = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": action})
    assert value.status_code == 201, value.status_code
    return value.json()["session_id"]


def state(client, sid):
    value = client.get(f"/v1/sessions/{sid}")
    assert value.status_code == 200
    return value.json()["session"]


def test_workspace_exposes_known_revision_before_a_new_explicit_intent(tmp_path):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        value = state(client, sid)
        assert isinstance(value.get("observation_revision"), int)
        assert value["observation_revision"] >= 0
        assert len(value["observation_generation"]) == 64


def test_older_resume_held_before_handler_cannot_overwrite_committed_pause_and_restart(tmp_path):
    entered, release = Event(), Event()
    client = client_for(tmp_path)

    @client.app.middleware("http")
    async def hold_only_synthetic_old_resume(request, call_next):
        if request.url.path.endswith("/observation/resume") and request.headers.get("X-Synthetic-Held-Intent") == "one":
            entered.set()
            for _ in range(1000):
                if release.is_set():
                    break
                await asyncio.sleep(0.005)
            assert release.is_set(), "synthetic barrier must close"
        return await call_next(request)

    with client:
        sid = create_session(client)
        initial = state(client, sid)
        # Before implementation the new field is absent. Zero models the new
        # schema's initial version and lets the old endpoint prove that it ignores
        # the supplied fence, independently of the separate missing-field RED.
        known = initial.get("observation_revision", 0)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, f"/v1/sessions/{sid}/observation/resume",
                headers={"X-Synthetic-Held-Intent": "one"},
                json={"client_action_id": "synthetic-older-resume", "expected_revision": known, "expected_generation": initial["observation_generation"]})
            assert entered.wait(5)
            try:
                paused = client.post(f"/v1/sessions/{sid}/observation/pause", json={"client_action_id": "synthetic-newer-pause"})
                assert paused.status_code == 200
                assert state(client, sid)["observation_status"] == "paused"
            finally:
                release.set()
            old = future.result(timeout=10)
        before_restart = state(client, sid)
    with client_for(tmp_path) as restarted:
        after_restart = state(restarted, sid)
    if os.environ.get("HSC_SYNTHETIC_FENCE_WITNESS"):
        Path(os.environ["HSC_SYNTHETIC_FENCE_WITNESS"]).write_text(json.dumps({
            "scope": "synthetic_ASGI_HTTP_SQLite_only", "pause_HTTP": paused.status_code,
            "old_resume_HTTP": old.status_code, "old_resume_code": old.json().get("code"),
            "before_restart": before_restart["observation_status"],
            "after_restart": after_restart["observation_status"],
            "known_revision_at_original_intent": known, "family_DB_read": False,
        }, indent=2, sort_keys=True) + "\n")
    assert before_restart["observation_status"] == "paused"
    assert after_restart["observation_status"] == "paused"
    assert old.status_code == 409
    assert old.json()["code"] == "OBSERVATION_CONTROL_REVISION_CHANGED"


def test_missing_revision_old_client_cannot_resume_after_pause(tmp_path):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        assert client.post(f"/v1/sessions/{sid}/observation/pause", json={"client_action_id": "synthetic-legacy-pause"}).status_code == 200
        result = client.post(f"/v1/sessions/{sid}/observation/resume", json={"client_action_id": "synthetic-legacy-old-resume"})
        assert result.status_code == 409
        assert result.json()["code"] == "OBSERVATION_CONTROL_REVISION_REQUIRED"
        assert state(client, sid)["observation_status"] == "paused"


def test_replayed_original_resume_does_not_overwrite_newer_pause(tmp_path):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        known = state(client, sid).get("observation_revision", 0)
        payload = {"client_action_id": "synthetic-original-resume-action", "expected_revision": known, "expected_generation": state(client, sid)["observation_generation"]}
        original = client.post(f"/v1/sessions/{sid}/observation/resume", json=payload)
        assert original.status_code == 200
        assert client.post(f"/v1/sessions/{sid}/observation/pause", json={"client_action_id": "synthetic-later-privacy-pause"}).status_code == 200
        replay = client.post(f"/v1/sessions/{sid}/observation/resume", json=payload)
        assert replay.status_code == 409
        assert replay.json()["code"] == "OBSERVATION_CONTROL_ACTION_SUPERSEDED"
        assert state(client, sid)["observation_status"] == "paused"


def spy_effects(client, monkeypatch):
    from home_study_agent.app import DeviceRegistry
    calls = []
    engine = client.app.state.observation_engine
    for name in ("release", "pause_qualification", "begin_qualification"):
        target = "begin_qualification_in" if name == "begin_qualification" and hasattr(engine, "begin_qualification_in") else name
        original = getattr(engine, target)
        def tracked(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(engine, target, tracked)
    original = DeviceRegistry.release_session
    def release(self, *args, **kwargs):
        calls.append("release_session")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(DeviceRegistry, "release_session", release)
    return calls


def test_same_revision_duplicates_and_legacy_enabled_noop_never_repeat_lease_or_qualification(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        calls = spy_effects(client, monkeypatch)
        legacy = client.post(f"/v1/sessions/{sid}/observation/resume", json={"client_action_id": "synthetic-noop-legacy"})
        assert legacy.status_code == 200
        assert legacy.json()["observation_control"] | {} == {
            "client_action_id": "synthetic-noop-legacy", "operation": "enabled", "expected_revision": None,
            "expected_generation": None, "result_revision": 0, "effect": "no_op", "applied": False, "fresh": False}
        assert calls == []
        payload = {"client_action_id": "synthetic-pause-duplicate"}
        first = client.post(f"/v1/sessions/{sid}/observation/pause", json=payload)
        assert first.status_code == 200
        assert first.json()["observation_control"]["applied"] is True
        after_first = list(calls)
        assert after_first == ["release_session", "release", "pause_qualification"]
        second = client.post(f"/v1/sessions/{sid}/observation/pause", json=payload)
        assert second.status_code == 200
        assert second.json()["observation_revision"] == first.json()["observation_revision"] == 1
        assert second.json()["observation_control"]["applied"] is False
        assert second.json()["observation_control"]["fresh"] is False
        assert calls == after_first
        resume_payload = {"client_action_id": "synthetic-fresh-resume-duplicate", "expected_revision": 1, "expected_generation": state(client, sid)["observation_generation"]}
        resumed = client.post(f"/v1/sessions/{sid}/observation/resume", json=resume_payload)
        assert resumed.status_code == 200
        assert resumed.json()["observation_revision"] == 3
        assert calls == after_first + ["release", "begin_qualification"]
        count = list(calls)
        duplicate = client.post(f"/v1/sessions/{sid}/observation/resume", json=resume_payload)
        assert duplicate.status_code == 200
        assert duplicate.json()["observation_control"]["effect"] == "replayed"
        assert duplicate.json()["observation_control"]["applied"] is False
        assert duplicate.json()["observation_control"]["fresh"] is False
        assert calls == count


def test_superseded_legacy_noop_receipt_is_not_an_enabled_ack_after_pause(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        payload = {"client_action_id": "synthetic-legacy-replay-noop"}
        assert client.post(f"/v1/sessions/{sid}/observation/resume", json=payload).status_code == 200
        client.post(f"/v1/sessions/{sid}/observation/pause", json={"client_action_id": "synthetic-new-pause-noop"})
        calls = spy_effects(client, monkeypatch)
        replay = client.post(f"/v1/sessions/{sid}/observation/resume", json=payload)
        assert replay.status_code == 409
        assert replay.json()["code"] == "OBSERVATION_CONTROL_REVISION_REQUIRED"
        assert state(client, sid)["observation_status"] == "paused"
        assert calls == []


def test_action_fingerprint_cannot_be_reused_with_a_fresh_revision_or_other_operation(tmp_path):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        payload = {"client_action_id": "synthetic-fixed-original-intent", "expected_revision": 0, "expected_generation": state(client, sid)["observation_generation"]}
        assert client.post(f"/v1/sessions/{sid}/observation/resume", json=payload).status_code == 200
        payload["expected_revision"] = 1
        changed = client.post(f"/v1/sessions/{sid}/observation/resume", json=payload)
        assert changed.status_code == 409
        assert changed.json()["code"] == "OBSERVATION_CONTROL_ACTION_REUSED"
        changed_op = client.post(f"/v1/sessions/{sid}/observation/pause", json={"client_action_id": payload["client_action_id"]})
        assert changed_op.status_code == 409
        assert state(client, sid)["observation_revision"] == 2


def test_rejected_revision_does_not_consume_action_or_increment_revision(tmp_path):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        client.post(f"/v1/sessions/{sid}/observation/pause", json={"client_action_id": "synthetic-rollback-pause"})
        rejected = client.post(f"/v1/sessions/{sid}/observation/resume", json={"client_action_id": "synthetic-rejected-old-action", "expected_revision": 0, "expected_generation": state(client, sid)["observation_generation"]})
        assert rejected.status_code == 409
        assert state(client, sid)["observation_revision"] == 1
        with client.app.state.repository.database.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM observation_control_action WHERE client_action_id=?", ("synthetic-rejected-old-action",)).fetchone()[0]
        assert count == 0
        # A new explicit action, rather than rewriting the old intent, may resume.
        fresh = client.post(f"/v1/sessions/{sid}/observation/resume", json={"client_action_id": "synthetic-fresh-new-action", "expected_revision": 1, "expected_generation": state(client, sid)["observation_generation"]})
        assert fresh.status_code == 200
        assert fresh.json()["observation_revision"] == 3


def test_session_cancel_blocks_old_resume_and_duplicate_receipt_in_same_transaction(tmp_path):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        payload = {"client_action_id": "synthetic-resume-before-cancel", "expected_revision": 0, "expected_generation": state(client, sid)["observation_generation"]}
        assert client.post(f"/v1/sessions/{sid}/observation/resume", json=payload).status_code == 200
        with client.app.state.repository.database.connect() as db:
            db.execute("UPDATE daily_session SET status='cancelled' WHERE session_id=?", (sid,))
            db.commit()
        result = client.post(f"/v1/sessions/{sid}/observation/resume", json=payload)
        assert result.status_code == 409
        assert state(client, sid)["status"] == "cancelled"


def test_revision_validation_rejects_bool_float_negative_and_string(tmp_path):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        for value in (True, False, -1, 0.5, "0", 9007199254740991):
            result = client.post(f"/v1/sessions/{sid}/observation/resume", json={"client_action_id": "synthetic-invalid-revision", "expected_revision": value})
            assert result.status_code == 422
        assert state(client, sid)["observation_revision"] == 0


def test_snapshot_restore_rotates_existing_storage_generation_and_rejects_old_prepared_resume(tmp_path):
    import hashlib
    from uuid import uuid4
    with client_for(tmp_path, native_launch_attestation_key=KEY) as client:
        headers = {"X-HSC-Parent": client.post('/v1/parent/setup', json={'pin': '4286'}).json()['parent_token']}
        sid = create_session(client)
        initial = state(client, sid)
        old_generation = client.get('/v1/parent/privacy-operations/generation', headers=headers).json()['generation']
        old_payload = {"client_action_id": "synthetic-pre-restore-intent", "expected_revision": initial['observation_revision'],
                       "expected_generation": initial.get('observation_generation') or hashlib.sha256(('observation-intent-v1:' + sid + ':' + old_generation).encode()).hexdigest()}
        backup = client.post('/v1/parent/backups', headers=headers)
        assert backup.status_code == 200
        assert client.post(f'/v1/sessions/{sid}/observation/pause', json={'client_action_id': 'synthetic-post-backup-pause'}).status_code == 200
        # The real restore API forbids live sessions. This synthetic fixture closes
        # the current session, while the earlier actual backup contains it active.
        with client.app.state.repository.database.connect() as db:
            db.execute("UPDATE daily_session SET status='cancelled' WHERE session_id=?", (sid,))
            db.commit()
        operation = {'request_id': str(uuid4()), 'expected_generation': old_generation,
                     'cleanup_manifest_sha256': hashlib.sha256(b'synthetic exact cleanup keys').hexdigest()}
        restored = signed_local_restore_post(client, headers=headers,
            json={'backup_id': backup.json()['backup_id'], 'confirmation': '确认恢复', 'operation': operation})
        assert restored.status_code == 200, restored.text[:300]
        current_generation = client.get('/v1/parent/privacy-operations/generation', headers=headers).json()['generation']
        assert current_generation != old_generation
        response = client.post(f'/v1/sessions/{sid}/observation/resume', json=old_payload)
        if os.environ.get('HSC_SYNTHETIC_RESTORE_WITNESS'):
            Path(os.environ['HSC_SYNTHETIC_RESTORE_WITNESS']).write_text(json.dumps({
                'scope': 'synthetic_actual_HTTP_backup_restore_SQLite', 'restore_HTTP': restored.status_code,
                'generation_rotated': current_generation != old_generation, 'old_resume_HTTP': response.status_code,
                'old_resume_code': response.json().get('code'), 'family_DB_read': False}, sort_keys=True, indent=2)+'\n')
        assert response.status_code == 409
        assert response.json()['code'] == 'OBSERVATION_CONTROL_GENERATION_CHANGED'
        restored_state = state(client, sid)
        assert restored_state['observation_generation'] != old_payload['expected_generation']
        # A new explicit intent, with a new action and freshly read generation,
        # can use the restored revision. The old action is never silently retried.
        fresh = client.post(f'/v1/sessions/{sid}/observation/resume', json={
            'client_action_id': 'synthetic-post-restore-new-intent',
            'expected_revision': restored_state['observation_revision'],
            'expected_generation': restored_state['observation_generation']})
        assert fresh.status_code == 200
        assert fresh.json()['observation_control']['applied'] is True


def test_two_explicit_intents_cannot_both_commit_the_same_revision(tmp_path):
    from threading import Barrier
    with client_for(tmp_path) as client:
        sid = create_session(client)
        known = state(client, sid)
        barrier = Barrier(2)
        def send(index):
            barrier.wait(timeout=5)
            return client.post(f'/v1/sessions/{sid}/observation/resume', json={
                'client_action_id': f'synthetic-concurrent-intent-{index}',
                'expected_revision': known['observation_revision'], 'expected_generation': known['observation_generation']})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, range(2)))
        assert sorted(r.status_code for r in results) == [200, 409]
        assert state(client, sid)['observation_revision'] == 2
        with client.app.state.repository.database.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM observation_control_action WHERE session_id=?', (sid,)).fetchone()[0] == 1


def test_revision_without_original_generation_cannot_bypass_restore_fence(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        calls = spy_effects(client, monkeypatch)
        response = client.post(f'/v1/sessions/{sid}/observation/resume', json={
            'client_action_id': 'synthetic-missing-original-generation', 'expected_revision': 0})
        assert response.status_code == 409
        assert response.json()['code'] == 'OBSERVATION_CONTROL_GENERATION_REQUIRED'
        assert state(client, sid)['observation_revision'] == 0
        assert calls == []


def test_receipt_insert_failure_rolls_back_state_revision_and_skips_side_effects(tmp_path, monkeypatch):
    import pytest
    import sqlite3
    with client_for(tmp_path) as client:
        sid = create_session(client)
        client.post(f'/v1/sessions/{sid}/observation/pause', json={'client_action_id': 'synthetic-before-insert-failure'})
        known = state(client, sid)
        calls = spy_effects(client, monkeypatch)
        with client.app.state.repository.database.connect() as db:
            db.execute("CREATE TRIGGER synthetic_fail_receipt BEFORE INSERT ON observation_control_action BEGIN SELECT RAISE(ABORT, 'synthetic receipt write failure'); END")
            db.commit()
        with pytest.raises(sqlite3.IntegrityError, match='synthetic receipt write failure'):
            client.post(f'/v1/sessions/{sid}/observation/resume', json={
                'client_action_id': 'synthetic-failed-atomic-insert', 'expected_revision': known['observation_revision'],
                'expected_generation': known['observation_generation']})
        current = state(client, sid)
        assert current['observation_status'] == 'paused'
        assert current['observation_revision'] == known['observation_revision']
        assert calls == []


def test_workspace_revision_and_restore_generation_are_one_sqlite_read_snapshot(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import hashlib
    with client_for(tmp_path) as client:
        sid = create_session(client)
        db = client.app.state.repository.database
        original_connect = db.connect
        with original_connect() as connection:
            old_raw = connection.execute('SELECT generation FROM privacy_storage_generation WHERE singleton=1').fetchone()[0]
        old_opaque = hashlib.sha256(('observation-intent-v1:' + sid + ':' + old_raw).encode()).hexdigest()
        new_raw = 'synthetic-new-restore-generation'
        new_opaque = hashlib.sha256(('observation-intent-v1:' + sid + ':' + new_raw).encode()).hexdigest()
        changed = []
        class Cursor:
            def __init__(self, cursor): self.cursor = cursor
            def fetchone(self):
                row = self.cursor.fetchone()
                self.cursor.close()
                if not changed:
                    changed.append(True)
                    with original_connect() as writer:
                        writer.execute('BEGIN IMMEDIATE')
                        writer.execute('UPDATE privacy_storage_generation SET generation=? WHERE singleton=1', (new_raw,))
                        writer.execute('UPDATE daily_session SET observation_revision=1 WHERE session_id=?', (sid,))
                        writer.commit()
                return row
        class ReadHook:
            def __init__(self, connection): self.connection = connection
            def __getattr__(self, name): return getattr(self.connection, name)
            def execute(self, sql, *args):
                cursor = self.connection.execute(sql, *args)
                if not changed and 'SELECT session_id, child_id' in sql and 'FROM daily_session WHERE session_id' in sql:
                    return Cursor(cursor)
                return cursor
        @contextmanager
        def hook():
            with original_connect() as connection:
                yield ReadHook(connection)
        monkeypatch.setattr(db, 'connect', hook)
        observed = state(client, sid)
        assert changed == [True]
        assert (observed['observation_revision'], observed['observation_generation']) in {(0, old_opaque), (1, new_opaque)}


def test_actual_version83_database_upgrades_existing_paused_session_and_reopens(tmp_path):
    from importlib.machinery import SourceFileLoader
    from importlib.util import spec_from_loader, module_from_spec
    import sys
    name = 'home_study_agent._synthetic_before_fence_database'
    loader = SourceFileLoader(name, str(Path(__file__).parent / 'fixtures' / 'observation_fence_v83_database.py.txt'))
    spec = spec_from_loader(name, loader)
    before = module_from_spec(spec)
    missing = object()
    previous = sys.modules.get(name, missing)
    sys.modules[name] = before
    try:
        loader.exec_module(before)
    finally:
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    legacy = before.Database(tmp_path / 'synthetic.db')
    legacy.initialize()
    with legacy.connect() as db:
        assert db.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == 83
        db.execute("INSERT INTO child_profile VALUES('child-primary-4','Synthetic','4','synthetic','active',datetime('now'),datetime('now'))")
        db.execute("INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,observation_status,client_action_id) VALUES('synthetic-existing-session','child-primary-4','active','check_in',datetime('now'),datetime('now'),'paused','synthetic-original-create')")
        db.commit()
    for _ in range(2):
        with client_for(tmp_path) as client:
            current = state(client, 'synthetic-existing-session')
            assert current['observation_status'] == 'paused'
            assert current['observation_revision'] == 0
            assert len(current['observation_generation']) == 64
            with client.app.state.repository.database.connect() as db:
                from home_study_agent.database import LATEST_SCHEMA_VERSION
                assert db.execute('SELECT MAX(version) FROM schema_migration').fetchone()[0] == LATEST_SCHEMA_VERSION
                assert not db.execute('PRAGMA foreign_key_check').fetchall()


import pytest

@pytest.mark.parametrize(('operation', 'effect'), [('pause','pause_qualification'),('resume','begin_qualification')])
def test_unfinished_effects_never_replay_as_success_and_a_new_explicit_pause_recovers(tmp_path, monkeypatch, operation, effect):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        known = state(client, sid)
        calls = spy_effects(client, monkeypatch)
        engine = client.app.state.observation_engine
        effect = "begin_qualification_in" if effect == "begin_qualification" and hasattr(engine, "begin_qualification_in") else effect
        original = getattr(engine, effect)
        def fail_first(*args, **kwargs):
            calls.append(effect + '_failed')
            raise RuntimeError('synthetic unfinished observation effects')
        monkeypatch.setattr(engine, effect, fail_first)
        payload = {'client_action_id': 'synthetic-unfinished-' + operation}
        if operation == 'resume':
            payload.update(expected_revision=known['observation_revision'], expected_generation=known['observation_generation'])
        with pytest.raises(RuntimeError, match='synthetic unfinished observation effects'):
            client.post(f'/v1/sessions/{sid}/observation/{operation}', json=payload)
        assert state(client, sid)['observation_revision'] == 1
        monkeypatch.setattr(engine, effect, original)
        after_failure = list(calls)
        repeated = client.post(f'/v1/sessions/{sid}/observation/{operation}', json=payload)
        assert repeated.status_code == 409
        assert repeated.json()['code'] == 'OBSERVATION_CONTROL_EFFECTS_UNCONFIRMED'
        assert calls == after_failure
        fresh_payload = {'client_action_id': 'synthetic-new-explicit-safety-pause'}
        fresh = client.post(f'/v1/sessions/{sid}/observation/pause', json=fresh_payload)
        assert fresh.status_code == 200
        assert fresh.json()['observation_control']['applied'] is True
        assert fresh.json()['observation_status'] == 'paused'
        after_success = list(calls)
        duplicate = client.post(f'/v1/sessions/{sid}/observation/pause', json=fresh_payload)
        assert duplicate.status_code == 200
        assert duplicate.json()['observation_control']['applied'] is False
        assert calls == after_success


def test_failed_resume_get_never_publishes_enabled_as_a_capture_permission(tmp_path, monkeypatch):
    with client_for(tmp_path) as client:
        sid = create_session(client)
        client.post(f'/v1/sessions/{sid}/observation/pause', json={'client_action_id':'synthetic-before-resume-failure'})
        known = state(client, sid)
        engine = client.app.state.observation_engine
        target = 'begin_qualification_in' if hasattr(engine, 'begin_qualification_in') else 'begin_qualification'
        def fail(*args, **kwargs): raise RuntimeError('synthetic qualification failure before publication')
        monkeypatch.setattr(engine, target, fail)
        with pytest.raises(RuntimeError, match='synthetic qualification failure before publication'):
            client.post(f'/v1/sessions/{sid}/observation/resume', json={
                'client_action_id':'synthetic-no-enabled-publication', 'expected_revision':known['observation_revision'],
                'expected_generation':known['observation_generation']})
        observed = state(client, sid)
        if os.environ.get('HSC_SYNTHETIC_PUBLICATION_WITNESS'):
            Path(os.environ['HSC_SYNTHETIC_PUBLICATION_WITNESS']).write_text(json.dumps({
                'scope':'synthetic_real_ASGI_SQLite_only', 'qualification_failed':True,
                'fresh_workspace_HTTP':200, 'observation_status':observed['observation_status'],
                'original_pause_revision':known['observation_revision'], 'observed_revision':observed['observation_revision'],
                'family_DB_read':False},indent=2,sort_keys=True)+'\n')
        assert observed['observation_status'] == 'paused'


def test_new_pause_supersedes_reserved_resume_before_permission_finalization(tmp_path, monkeypatch):
    entered, release = Event(), Event()
    with client_for(tmp_path) as client:
        sid = create_session(client)
        known = state(client, sid)
        engine = client.app.state.observation_engine
        original = engine.release
        def hold_only_original_resume(session_id):
            if not entered.is_set():
                entered.set()
                assert release.wait(5), 'synthetic resume release barrier closed'
            return original(session_id)
        monkeypatch.setattr(engine, 'release', hold_only_original_resume)
        other = TestClient(client.app)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, f'/v1/sessions/{sid}/observation/resume', json={
                'client_action_id':'synthetic-reserved-old-resume','expected_revision':known['observation_revision'],
                'expected_generation':known['observation_generation']})
            assert entered.wait(5)
            try:
                during = state(other, sid)
                assert during['observation_status'] == 'paused'
                paused = other.post(f'/v1/sessions/{sid}/observation/pause', json={'client_action_id':'synthetic-superseding-pause'})
                assert paused.status_code == 200
                assert paused.json()['observation_status'] == 'paused'
            finally:
                release.set()
            result = future.result(timeout=10)
        assert result.status_code == 409
        assert result.json()['code'] == 'OBSERVATION_CONTROL_ACTION_SUPERSEDED'
        assert state(client, sid)['observation_status'] == 'paused'


def test_qualification_write_failure_rolls_back_permission_and_original_public_guard_is_retained(tmp_path, monkeypatch):
    from home_study_agent.observation_engine import ObservationPausedError
    with client_for(tmp_path) as client:
        sid = create_session(client)
        client.post(f'/v1/sessions/{sid}/observation/pause', json={'client_action_id':'synthetic-transaction-qualification-pause'})
        known = state(client, sid)
        engine = client.app.state.observation_engine
        with pytest.raises(ObservationPausedError): engine.begin_qualification(sid)
        original = engine.begin_qualification_in
        def write_then_fail(connection, session_id):
            original(connection, session_id)
            raise RuntimeError('synthetic rollback after actual qualification SQL')
        monkeypatch.setattr(engine, 'begin_qualification_in', write_then_fail)
        with pytest.raises(RuntimeError, match='synthetic rollback after actual qualification SQL'):
            client.post(f'/v1/sessions/{sid}/observation/resume', json={
                'client_action_id':'synthetic-qualification-finalize-rollback','expected_revision':known['observation_revision'],
                'expected_generation':known['observation_generation']})
        current = state(client, sid)
        assert current['observation_status'] == 'paused'
        assert current['observation_revision'] == known['observation_revision'] + 1
        with client.app.state.repository.database.connect() as db:
            assert db.execute('SELECT status FROM observation_qualification WHERE session_id=?', (sid,)).fetchone()[0] == 'paused'
            assert db.execute('SELECT effects_complete FROM observation_control_action WHERE client_action_id=?', ('synthetic-qualification-finalize-rollback',)).fetchone()[0] == 0
