"""Actual isolated Electron RTC data channel -> original Runtime -> SQLite.

No physical input, supplier request, household record or cloud credential.
"""
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys

import httpx
import pytest

from test_realtime_output_authority import case
from test_realtime_input_source import input_case
from test_realtime_input_source_tcp import runtime_server


def current_native_client(root, tmp_path, evidence_name):
    evidence = root / 'docs' / 'evidence' / evidence_name
    script = tmp_path / 'native-client.cjs'
    fixture_name = {'2026-10-05-production-rtc-wire': 'wire', '2026-10-05-rtc-input-handoff': 'input_handoff'}[evidence_name]
    fixture = Path(__file__).parent / 'fixtures' / 'realtime_rtc_native' / fixture_name
    shutil.copyfile(fixture / 'native-client.cjs', script)
    built = subprocess.run([str(root / 'node_modules' / '.bin' / 'esbuild'),
        str(evidence / 'native-entry.ts'), '--bundle', '--platform=browser',
        '--format=iife', '--global-name=remoteVoice', f'--outfile={tmp_path / "native-protocol.js"}'],
        capture_output=True, text=True, timeout=20)
    assert built.returncode == 0, built.stderr[-1200:]
    return script


def assert_actual_native_tracks(observed, pair_count):
    pairs = observed['native_track_pairs']
    assert len(pairs) == pair_count and observed['stopped_tracks'] == pair_count * 2
    identities = [p[name] for p in pairs for name in ['root_id', 'cloud_id']]
    assert len(set(identities)) == pair_count * 2
    for p in pairs:
        assert p['native_root_clone_distinct'] is True and p['root_is_stream_member'] is True
        assert p['native_clone_calls'] == p['root_stop_calls'] == p['cloud_stop_calls'] == 1
        assert p['root_ready_state'] == p['cloud_ready_state'] == 'ended' and p['context_state'] == 'closed'
        assert p['media']['inactive_negotiated'] is True
        assert p['media']['observation'] in {'observed', 'unknown'}
        if p['media']['observation'] == 'unknown':
            assert p['media']['audio_packets_sent'] is None and p['media']['audio_bytes_sent'] is None


def planning_calls(db):
    with db.connect() as c:
        return {row['call_id']: dict(row) for row in c.execute(
            "SELECT * FROM model_call WHERE purpose='agent_plan'")}


def assert_one_new_planning_call(db, before, scope):
    after = planning_calls(db)
    # The output-authority fixture already seeds an unrelated Gateway call.
    # Compare all new calls, regardless of provider, so a retry cannot hide
    # behind that fixture or a provider-specific count.
    assert {key: after.get(key) for key in before} == before
    added = [row for key, row in after.items() if key not in before]
    assert len(added) == 1
    assert added[0]['provider'] == 'test' and added[0]['status'] == 'completed'
    assert added[0]['session_id'] == scope['session_id']
    assert added[0]['child_id'] == scope['child_id']


@pytest.mark.skipif(sys.platform != 'darwin', reason='This qualification requires the local macOS Electron runtime')
@pytest.mark.parametrize('replay_delivery_delay_ms', [0, 150])
def test_current_rtc_protocol_withholds_protected_result_and_keeps_original_after_restart(input_case, tmp_path, replay_delivery_delay_ms):
    db, scope, capture = input_case
    before_calls = planning_calls(db)
    root = Path(__file__).resolve().parents[3]
    script = current_native_client(root, tmp_path, '2026-10-05-production-rtc-wire')
    executable = root / 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'
    env = dict(os.environ)
    env.pop('ELECTRON_RUN_AS_NODE', None)
    with runtime_server(db) as (origin, worker):
        child = subprocess.Popen([str(executable), str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
        try:
            stdout, stderr = child.communicate(json.dumps({'scope': scope, 'original': capture['original_transcript'],
                'replay_delivery_delay_ms': replay_delivery_delay_ms,
                'runtime': {'localApiUrl': origin, 'sessionToken': 'synthetic-output-session-token'}}), timeout=40)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(10)
        assert child.returncode == 0, stderr[-1800:] + stdout[-1800:]
        observed = json.loads(stdout)
        assert capture['original_transcript'] not in stdout and 'signature' not in stdout
        assert len(observed['scenarios']) == 3
        assert observed['runtime_requests'] == 1
        assert observed['closed_peers'] == observed['peer_count'] == 6
        assert_actual_native_tracks(observed, 3)
        assert observed['response_requests'] == observed['complete_playback_receipts'] == observed['audio_frames'] == 0
        assert not observed['supplier_real'] and not observed['microphone_used']
        with db.connect() as c:
            turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (observed['runtime_turn_id'],)).fetchone()
            action = json.loads(turn['action_json'])
        assert turn['transcript'] == capture['original_transcript'] and turn['tool_status'] == 'completed'
        assert action['local_only'] is True and action['cloud_transmission_allowed'] is False
        worker.kill(); worker.join(10)
        assert worker.exitcode == -9
    with runtime_server(db) as (origin, _), httpx.Client(base_url=origin, trust_env=False) as client:
        receipt = client.get(f"/v1/sessions/{scope['session_id']}/agent-request-receipts/realtime-{scope['voice_input_id']}",
            headers={'X-HSC-Session': 'synthetic-output-session-token'})
        assert receipt.status_code == 200 and receipt.json()['status'] == 'recorded_completed'
        repeated = client.post(f"/v1/sessions/{scope['session_id']}/voice/realtime/actions/{scope['client_action_id']}/resume",
            json={'child_id': scope['child_id'], 'voice_input_id': scope['voice_input_id']},
            headers={'X-HSC-Session': 'synthetic-output-session-token'})
        assert repeated.status_code == 409
    with db.connect() as c:
        assert c.execute('SELECT count(*) FROM agent_turn WHERE client_action_id=?', ('realtime-' + scope['voice_input_id'],)).fetchone()[0] == 1
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert_one_new_planning_call(db, before_calls, scope)
    observed.update(runtime_restart_receipt='recorded_completed', repeated_resume_status=409, database_integrity='ok')
    print(json.dumps(observed, ensure_ascii=False))


@pytest.mark.skipif(sys.platform != 'darwin', reason='This qualification requires the local macOS Electron runtime')
def test_native_notification_failure_and_lost_admission_ack_keep_exact_original_through_restart(case, tmp_path):
    db, scope, _ = case
    before_calls = planning_calls(db)
    original = '陪我聊聊花园里的花，第一句还有第二句。'
    root = Path(__file__).resolve().parents[3]
    script = current_native_client(root, tmp_path, '2026-10-05-rtc-input-handoff')
    executable = root / 'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'
    env = dict(os.environ)
    env.pop('ELECTRON_RUN_AS_NODE', None)
    with runtime_server(db) as (origin, worker):
        child = subprocess.Popen([str(executable), str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
        try:
            stdout, stderr = child.communicate(json.dumps({'scope': scope, 'original': original,
                'runtime': {'localApiUrl': origin, 'sessionToken': 'synthetic-output-session-token'}}), timeout=40)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(10)
        assert child.returncode == 0, stderr[-1200:] + stdout[-1200:]
        observed = json.loads(stdout)
        assert original not in stdout and 'private detail' not in stdout and 'signature' not in stdout
        assert observed['original_preserved'] and observed['capture_preserved']
        assert observed['acknowledgement_lost_after_commit'] and observed['callback_failure_reported']
        assert observed['runtime_resumes'] == 1 and observed['failed_connection_tool_calls'] == 0
        assert observed['closed_peers'] == 4
        assert_actual_native_tracks(observed, 2)
        assert observed['paid_requests'] == observed['complete_playback_receipts'] == 0
        with db.connect() as c:
            turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (observed['runtime_turn_id'],)).fetchone()
            assert turn['transcript'] == original and turn['tool_status'] == 'completed'
            assert json.loads(turn['action_json'])['local_only'] is True
        worker.kill(); worker.join(10)
        assert worker.exitcode == -9
    with runtime_server(db) as (origin, _), httpx.Client(base_url=origin, trust_env=False,
            headers={'X-HSC-Session': 'synthetic-output-session-token'}) as client:
        capture = client.get(f"/v1/sessions/{scope['session_id']}/voice/realtime/actions/{observed['capture_action_id']}")
        assert capture.status_code == 200 and capture.json()['status'] == 'completed'
        assert capture.json()['voice_input_id'] == observed['voice_input_id']
        assert 'original_transcript' not in capture.json() and capture.json()['replay_allowed'] is False
        receipt = client.get(f"/v1/sessions/{scope['session_id']}/agent-request-receipts/realtime-{observed['voice_input_id']}")
        assert receipt.status_code == 200 and receipt.json()['status'] == 'recorded_completed'
        repeated = client.post(f"/v1/sessions/{scope['session_id']}/voice/realtime/actions/{observed['capture_action_id']}/resume",
            json={'child_id': scope['child_id'], 'voice_input_id': observed['voice_input_id']})
        assert repeated.status_code == 409
    with db.connect() as c:
        assert c.execute('SELECT transcript FROM voice_input WHERE voice_input_id=?', (observed['voice_input_id'],)).fetchone()[0] == original
        assert c.execute('SELECT count(*) FROM voice_capture_binding WHERE client_action_id=?', (observed['capture_action_id'],)).fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM agent_turn WHERE client_action_id=?', ('realtime-' + observed['voice_input_id'],)).fetchone()[0] == 1
        assert c.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert_one_new_planning_call(db, before_calls, scope)
    observed.update(runtime_restart_receipt='recorded_completed', repeated_resume_status=409, database_integrity='ok')
    print(json.dumps(observed, ensure_ascii=False))
