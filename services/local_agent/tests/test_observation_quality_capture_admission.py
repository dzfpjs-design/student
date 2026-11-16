"""Quality telemetry must be bound to the original live capture before writing."""
import pytest
from test_observation_capture_admission import actual, managed, start, body, evidence, EPOCH


def quality(epoch=EPOCH, sequence=2):
    return {"scene_quality": "too_dark", "reason": "public synthetic dark frame", "capture_sample": body(epoch, sequence)["capture_sample"]}


def test_late_old_quality_cannot_overwrite_new_epoch_qualified_capture(actual):
    db, app, client, engine, _ = actual
    start(client)
    start(client, "public-new-quality-epoch", "public-new-quality-start")
    frame = client.post("/v1/sessions/session/observation-frame", json=body("public-new-quality-epoch"))
    assert frame.status_code == 200 and frame.json()["qualification"]["status"] == "qualified"
    before = evidence(db)
    trackers = dict(engine._sessions)
    result = client.post("/v1/sessions/session/observation/qualification/unavailable", json=quality())
    assert result.status_code == 409, result.text
    assert result.json()["code"] == "OBSERVATION_CAPTURE_STALE"
    assert evidence(db) == before and engine._sessions == trackers
    assert engine.qualification("session")["status"] == "qualified"


def test_stopped_quality_is_stale_without_any_evidence_write(actual):
    db, app, client, engine, _ = actual
    start(client)
    stop = client.post("/v1/sessions/session/observation/stream/stop", json={"stream_epoch": EPOCH, "sequence": 1,
        "reason": "disconnected", "client_action_id": "public-quality-stop"})
    assert stop.status_code == 200
    before = evidence(db)
    result = client.post("/v1/sessions/session/observation/qualification/unavailable", json=quality())
    assert result.status_code == 409 and result.json()["code"] == "OBSERVATION_CAPTURE_STALE"
    assert evidence(db) == before and not engine._sessions


def test_unbound_quality_remains_display_only_without_durable_qualification(actual):
    db, app, client, engine, _ = actual
    before = evidence(db)
    result = client.post("/v1/sessions/session/observation/qualification/unavailable", json={"scene_quality": "too_dark", "reason": "public old client quality"})
    assert result.status_code == 200
    value = result.json()
    assert value["status"] == "unavailable" and value["scene_quality"] == "too_dark"
    assert value["analysis_scope"] == "framing_only" and value["person_baseline"] == "not_evaluated"
    assert value["qualified_at"] is None
    assert evidence(db) == before and not engine._sessions


def test_bound_current_quality_is_persisted_without_new_behavior_claim(actual):
    db, app, client, engine, _ = actual
    start(client)
    before = evidence(db)
    result = client.post("/v1/sessions/session/observation/qualification/unavailable", json=quality())
    assert result.status_code == 200 and result.json()["status"] == "unavailable"
    after = evidence(db)
    assert {k:v for k,v in after.items() if k != "observation_qualification"} == {k:v for k,v in before.items() if k != "observation_qualification"}
    assert engine.qualification("session")["scene_quality"] == "too_dark" and not engine._sessions


@pytest.mark.parametrize("change", ["stop", "revision", "generation", "mode"])
def test_second_original_connection_change_after_preflight_rejects_quality(actual, monkeypatch, change):
    db, app, client, engine, _ = actual
    start(client)
    original = app.state.behavior_integration.capture_admission
    calls = []
    def interleave(session_id, capture, *, connection=None, expected=None):
        admission = original(session_id, capture, connection=connection, expected=expected)
        if connection is None and not calls:
            calls.append(True)
            if change == "stop":
                app.state.behavior_integration.stop_observation_stream("session", stream_epoch=EPOCH,
                    sequence=1, reason="disconnected", client_action_id="public-quality-second-c-stop")
            else:
                with db.connect() as other:
                    if change == "revision": other.execute("UPDATE daily_session SET observation_revision=observation_revision+1 WHERE session_id='session'")
                    elif change == "generation": other.execute("UPDATE privacy_storage_generation SET generation='public-quality-new-generation' WHERE singleton=1")
                    else: other.execute("UPDATE family_rule SET observation_mode='shadow' WHERE child_id='child-primary-4'")
                    other.commit()
        return admission
    monkeypatch.setattr(app.state.behavior_integration, "capture_admission", interleave)
    before = evidence(db)
    result = client.post("/v1/sessions/session/observation/qualification/unavailable", json=quality())
    assert calls and result.status_code == 409, result.text
    assert result.json()["code"] == "OBSERVATION_CAPTURE_STALE"
    after = evidence(db)
    assert {k:v for k,v in after.items() if k != "observable_event"} == {k:v for k,v in before.items() if k != "observable_event"}
    assert len(after["observable_event"]) == len(before["observable_event"]) + (1 if change == "stop" else 0)
    assert not engine._sessions


def test_same_epoch_quality_cannot_replace_later_qualified_sequence(actual):
    db, app, client, engine, _ = actual
    start(client)
    response = client.post("/v1/sessions/session/observation-frame", json=body(sequence=3))
    assert response.status_code == 200 and response.json()["qualification"]["status"] == "qualified"
    before = evidence(db)
    stale = client.post("/v1/sessions/session/observation/qualification/unavailable", json=quality(sequence=2))
    assert stale.status_code == 409 and stale.json()["code"] == "OBSERVATION_CAPTURE_STALE"
    assert evidence(db) == before


def test_original_borrowed_quality_transaction_is_not_committed_or_rolled_back(actual):
    db, app, client, engine, _ = actual
    before = evidence(db)
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE child_profile SET nickname='public borrowed quality' WHERE child_id='child-primary-4'")
        result = engine.record_renderer_unavailable("session", "too_dark", "public borrowed quality", connection=connection)
        assert result["status"] == "unavailable" and connection.in_transaction
        assert connection.execute("SELECT nickname FROM child_profile WHERE child_id='child-primary-4'").fetchone()[0] == "public borrowed quality"
        connection.rollback()
    assert evidence(db) == before


def test_failed_quality_write_rolls_back_without_consuming_capture_or_tracker(actual):
    import sqlite3
    db, app, client, engine, _ = actual
    start(client)
    with db.connect() as connection:
        connection.execute("CREATE TRIGGER public_quality_abort BEFORE INSERT ON observation_qualification BEGIN SELECT RAISE(ABORT, 'public quality write stopped'); END")
        connection.commit()
    before = evidence(db)
    with pytest.raises(sqlite3.IntegrityError, match="public quality write stopped"):
        client.post("/v1/sessions/session/observation/qualification/unavailable", json=quality())
    assert evidence(db) == before and not engine._sessions
    with db.connect() as connection:
        connection.execute("DROP TRIGGER public_quality_abort")
        connection.commit()
    retry = client.post("/v1/sessions/session/observation/qualification/unavailable", json=quality())
    assert retry.status_code == 200 and retry.json()["status"] == "unavailable"
