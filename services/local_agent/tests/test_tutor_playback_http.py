"""Actual app router and SQLite; no model, audio, cloud, or family data."""

import json
import os
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.repository import StudyRepository
from test_tutor_playback import CHILD, OTHER, appended, binding, change, count, flow


@pytest.fixture
def environment(flow, tmp_path, monkeypatch):
    db, _, sid, turn, board = flow
    monkeypatch.setenv("HSC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HSC_DATABASE_PATH", str(db.path))
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False)
    return settings, db, sid, turn, board


def base(sid):
    return f"/v1/sessions/{sid}/tutor-playback"


def view(response):
    assert response.status_code == 200, response.text
    value = response.json()
    assert value["local_only"] is True and value["cloud_transmission_allowed"] is False
    return value["playback"]


def begin(client, sid, turn, board=None):
    return view(client.post(base(sid) + "/begin", json={"turn_id": turn["turn_id"],
        "client_action_id": "http-playback-begin", "presentation": board}))


def control(client, sid, state, command, key=None):
    return client.post(base(sid) + "/control", json={**binding(state), "command": command,
                        "client_action_id": key or "http-control-" + command})


def segment(client, sid, state, event, key=None):
    return client.post(base(sid) + "/segment", json={**binding(state), "event": event,
        "segment_id": state["presentation"]["segments"][state["segment_index"]]["segment_id"],
        "client_action_id": key or f"http-{event}-{state['segment_index']}"})


def test_http_local_snapshot_actual_order_replay_and_no_learning_completion(environment):
    settings, db, sid, turn, canonical = environment
    with TestClient(create_app(settings)) as client:
        assert view(client.get(base(sid))) is None
        state = begin(client, sid, turn, appended(canonical))
        assert state["status"] == "paused" and state["evidence_kind"] == "playback_only"
        assert state["child_id"] == CHILD and state["session_id"] == sid
        read = view(client.get(base(sid), params={"playback_id": state["playback_id"]}))
        assert read == state
        assert segment(client, sid, state, "started").status_code == 409
        state = view(control(client, sid, state, "resume"))
        assert segment(client, sid, state, "ended").status_code == 409
        with db.connect() as c:
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                      if r[0] in {"diagnostic_attempt", "tutor_outcome", "learning_snapshot", "mastery_update"}]
        before = {t: count(db, t) for t in tables}
        total = len(state["presentation"]["segments"])
        for _ in range(total):
            state = view(segment(client, sid, state, "started"))
            pending_end = state
            state = view(segment(client, sid, pending_end, "ended"))
            duplicate = view(segment(client, sid, pending_end, "ended"))
            assert duplicate["revision"] == state["revision"] and duplicate["replayed"]
        assert state["status"] == "playback_finished" and state["segment_index"] == total
        assert {t: count(db, t) for t in tables} == before
        with db.connect() as c:
            assert c.execute("SELECT state FROM daily_session WHERE session_id=?", (sid,)).fetchone()[0] != "completed"
        assert count(db, "tutor_playback_event") == 2 + total * 2
    with TestClient(create_app(settings)) as client:
        restored = view(client.get(base(sid)))
        assert restored == state
        assert control(client, sid, restored, "resume", "resume-finished").status_code == 409


def test_http_resume_receipt_is_idempotent_new_resume_repeats_only_incomplete_segment(environment):
    settings, db, sid, turn, _ = environment
    with TestClient(create_app(settings)) as client:
        paused = begin(client, sid, turn)
        first = view(control(client, sid, paused, "resume", "resume-first"))
        replay = view(control(client, sid, paused, "resume", "resume-first"))
        assert replay["generation"] == first["generation"] and replay["revision"] == first["revision"]
        started = view(segment(client, sid, first, "started"))
        second = view(control(client, sid, started, "resume", "resume-other-tab"))
        assert second["generation"] == started["generation"] + 1
        assert second["segment_index"] == 0 and not second["segment_started"]
        late = segment(client, sid, started, "ended")
        assert late.status_code == 409 and late.json()["detail"]["code"] == "STALE_GENERATION"
        assert view(client.get(base(sid)))["revision"] == second["revision"]
        assert control(client, sid, paused, "resume", "resume-first").status_code == 409
        assert segment(client, sid, second, "ended", "no-new-start").status_code == 409
        fresh = view(segment(client, sid, second, "started", "new-start"))
        fresh = view(segment(client, sid, fresh, "ended", "new-end"))
        assert fresh["segment_index"] == 1
        cancelled = view(control(client, sid, fresh, "cancel"))
        assert cancelled["status"] == "cancelled"
        assert segment(client, sid, fresh, "started", "late-start").status_code == 409


def test_real_http_in_new_process_pauses_and_rejects_old_end_then_wakes(environment):
    settings, db, sid, turn, _ = environment
    with TestClient(create_app(settings)) as client:
        state = begin(client, sid, turn)
        state = view(control(client, sid, state, "resume"))
        state = view(segment(client, sid, state, "started"))
    code = """import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
from home_study_agent.app import create_app
from home_study_agent.config import Settings
path, sid, old = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
settings = Settings(data_dir=path.parent, database_path=path, maintenance_enabled=False)
base = '/v1/sessions/' + sid + '/tutor-playback'
def bind(v):
    return dict(playback_id=v['playback_id'], source_version=v['source_version'],
        generation=v['generation'], expected_revision=v['revision'])
with TestClient(create_app(settings)) as client:
    recovered = client.get(base).json()['playback']
    late = client.post(base + '/segment', json={**bind(old), 'event':'ended',
        'segment_id':'segment-1', 'client_action_id':'old-process-end'})
    resumed = client.post(base + '/control', json={**bind(recovered), 'command':'resume',
        'client_action_id':'explicit-wake'}).json()['playback']
    started = client.post(base + '/segment', json={**bind(resumed), 'event':'started',
        'segment_id':'segment-1', 'client_action_id':'new-process-start'}).json()['playback']
    ended = client.post(base + '/segment', json={**bind(started), 'event':'ended',
        'segment_id':'segment-1', 'client_action_id':'new-process-end'}).json()['playback']
    print(json.dumps({'recovered':recovered, 'late_status':late.status_code,
        'late_code':late.json()['detail']['code'], 'ended':ended}))
"""
    result = subprocess.run([sys.executable, "-c", code, str(db.path), sid, json.dumps(state)],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr[-2000:]
    result = json.loads(result.stdout.splitlines()[-1])
    recovered = result["recovered"]
    assert recovered["status"] == "paused" and recovered["reason"] == "runtime_restarted"
    assert recovered["segment_index"] == 0 and not recovered["segment_started"]
    assert recovered["generation"] > state["generation"]
    assert result["late_status"] == 409 and result["late_code"] == "STALE_GENERATION"
    assert result["ended"]["segment_index"] == 1
    with TestClient(create_app(settings)) as client:
        again = view(client.get(base(sid)))
        assert again["status"] == "paused" and again["segment_index"] == 1


@pytest.mark.parametrize("mutation", ["revision", "withdrawn", "ownership"])
def test_http_source_change_invalidates_before_any_callback_can_advance(environment, mutation):
    settings, db, sid, turn, _ = environment
    with TestClient(create_app(settings)) as client:
        state = begin(client, sid, turn)
        state = view(control(client, sid, state, "resume"))
        state = view(segment(client, sid, state, "started"))
        if mutation == "revision":
            change(db, "UPDATE tutor_turn SET answer='Revised explanation' WHERE turn_id=?", (turn["turn_id"],))
        elif mutation == "withdrawn":
            change(db, "UPDATE tutor_turn SET status='withdrawn' WHERE turn_id=?", (turn["turn_id"],))
        else:
            change(db, "UPDATE tutor_turn SET child_id=? WHERE turn_id=?", (OTHER, turn["turn_id"]))
        rejected = segment(client, sid, state, "ended")
        assert rejected.status_code == 409 and rejected.json()["detail"]["code"] == "SOURCE_INVALIDATED"
        invalid = view(client.get(base(sid), params={"playback_id": state["playback_id"]}))
        assert invalid["status"] == "invalidated" and invalid["presentation"] is None
        assert invalid["segment_index"] == 0
        assert invalid["generation"] > state["generation"]


def test_http_strict_binding_payload_and_foreign_session_read(environment):
    settings, db, sid, turn, _ = environment
    with TestClient(create_app(settings)) as client:
        state = begin(client, sid, turn)
        payload = {**binding(state), "command": "resume", "client_action_id": "strict-control"}
        for field in binding(state):
            missing = {k: v for k, v in payload.items() if k != field}
            assert client.post(base(sid) + "/control", json=missing).status_code == 422
        assert client.post(base(sid) + "/control", json={**payload, "generation": True}).status_code == 422
        assert client.post(base(sid) + "/control", json={**payload, "allow": True}).status_code == 422
        assert client.post(base(sid) + "/control", json={**payload, "source_version": "forged"}).status_code == 409
        resumed = view(client.post(base(sid) + "/control", json=payload))
        conflict = client.post(base(sid) + "/control", json={**payload, "command": "cancel"})
        assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "PAYLOAD_CONFLICT"
        change(db, "UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        other_sid = StudyRepository(db).start_session(OTHER, "http-other-child")["session_id"]
        foreign = client.get(base(other_sid), params={"playback_id": state["playback_id"]})
        assert foreign.status_code in {404, 409}
        assert client.get(base(sid)).status_code == 409
        assert control(client, other_sid, resumed, "cancel", "foreign-cancel").status_code == 409
