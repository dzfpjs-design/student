import asyncio

import pytest

from home_study_agent.pet_media_evidence import PetMediaEvidenceError, PetMediaEvidenceService
from test_pet_media_evidence import media, begin, review, control, record, complete, change
from test_pet_media_http import mounted
from test_agent_continuation import orchestrator_for


def receipt(media, row, key):
    return media[1].playback_action_receipt(media[2], media_id=row["media_id"], client_action_id=key)


def test_completed_action_receipt_survives_new_generation_without_replay(media):
    row = complete(media)
    original = receipt(media, row, "end")
    assert original["replay_decision"] == "reuse_completed"
    assert original["receipt"]["historical_result"]["status"] == "playback_finished"
    assert original["receipt"]["request"]["monotonic_ms"] == 2000
    row = control(media, row, key="new-generation")
    recovered = receipt(media, row, "end")
    assert recovered["receipt"] == original["receipt"]
    assert recovered["current"]["status"] == "playing"
    assert recovered["replay_decision"] == "reuse_completed" and not recovered["automatic_playback_allowed"]


def test_started_without_ended_is_recovery_not_silent_restart(media):
    row = record(media, control(media, review(media, begin(media))))
    result = receipt(media, row, "start")
    assert result["replay_decision"] == "await_recovery"
    assert not result["receipt"]["completion_accepted"]
    assert receipt(media, row, "missing")["receipt"] is None


def test_pending_then_settled_original_action_query_does_not_settle_on_get(media):
    row = record(media, control(media, review(media, begin(media))))
    media[5][0] += 850
    pending = record(media, row, "ended", 16000, 2000, "end")
    assert receipt(media, pending, "end")["replay_decision"] == "retry_pending_receipt"
    media[5][0] += 150
    assert receipt(media, pending, "end")["replay_decision"] == "retry_pending_receipt"
    finished = record(media, row, "ended", 16000, 2000, "end")
    result = receipt(media, finished, "end")
    assert result["receipt"]["settled_event_id"] and result["replay_decision"] == "reuse_completed"
    assert result["receipt"]["historical_result"]["status"] == "pending_receipt"


def test_http_exact_receipt_read_is_owned_read_only_and_restart_safe(media):
    row = complete(media)
    client, _ = mounted(media, restarted=True)
    url = f"/v1/sessions/{media[2]}/pet-media/{row['media_id']}/action-receipt?client_action_id=end"
    assert client.get(url).status_code == 401
    client.headers["X-HSC-Session"] = "local-test-token"
    with media[0].connect() as c:
        before = c.execute("SELECT count(*) FROM pet_media_event").fetchone()[0]
    assert client.get(url).json()["replay_decision"] == "reuse_completed"
    with media[0].connect() as c:
        assert c.execute("SELECT count(*) FROM pet_media_event").fetchone()[0] == before
    assert client.get(url.replace(media[2], "foreign-session")).status_code == 404
    change(media[0], "UPDATE capture_artifact SET status='deleted' WHERE artifact_id='audio'")
    result = client.get(url).json()
    assert result["receipt"]["completion_accepted"] and result["replay_decision"] == "current_source_invalid"


@pytest.mark.parametrize("action_key", ["end", "start", "resume"])
def test_withdrawn_parent_review_keeps_history_but_denies_replay_after_restart(media, action_key):
    row = complete(media)
    original = receipt(media, row, action_key)["receipt"]
    assert original is not None
    review(media, row, "retract", "withdraw-original-source")
    client, _ = mounted(media, restarted=True)
    client.headers["X-HSC-Session"] = "local-test-token"
    url = f"/v1/sessions/{media[2]}/pet-media/{row['media_id']}/action-receipt"
    with media[0].connect() as c:
        before = list(c.iterdump())
    response = client.get(url, params={"client_action_id": action_key})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["receipt"] == original
    assert result["current"]["source_reviewed"] is False
    assert result["current"]["qualification"]["listening_delivery_qualified"] is False
    assert result["replay_decision"] == "current_source_invalid"
    assert result["automatic_playback_allowed"] is False
    with media[0].connect() as c:
        assert list(c.iterdump()) == before


def test_real_agent_control_transaction_and_lost_receipt(media, tmp_path, monkeypatch):
    review(media, begin(media))
    agent, gateway = orchestrator_for(media[0], tmp_path, {})
    turn = asyncio.run(agent.interpret(media[2], "继续听题", "voice", True))
    from home_study_agent.agent_runtime import AgentRuntimeStore
    inspect = AgentRuntimeStore.inspect_pet_listening_control
    def checked(c, turn_id, **kwargs):
        assert c.in_transaction
        return inspect(c, turn_id, **kwargs)
    monkeypatch.setattr(AgentRuntimeStore, "inspect_pet_listening_control", staticmethod(checked))
    client, _ = mounted(media)
    client.headers["X-HSC-Session"] = "local-test-token"
    path = f"/v1/sessions/{media[2]}/pet-media/listening/agent-control"
    payload = {"agent_turn_id": turn["agent_turn_id"]}
    assert client.post(path, json={**payload, "command": "restart"}).status_code == 422
    result = client.post(path, json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["media"]["status"] == "playing"
    assert not result.json()["media"]["qualification"]["task_delivery_qualified"]
    retry = client.post(path, json=payload).json()
    assert retry["replayed"] and retry["control_action_id"] == turn["agent_turn_id"] + ":pet:control"
    assert gateway.requests == []


def test_cancelled_agent_plan_cannot_control_media(media, tmp_path):
    row = review(media, begin(media))
    agent, _ = orchestrator_for(media[0], tmp_path, {})
    turn = asyncio.run(agent.interpret(media[2], "继续听题", "voice", True))
    change(media[0], "UPDATE agent_plan SET status='cancelled' WHERE agent_turn_id=?", (turn["agent_turn_id"],))
    with pytest.raises(PetMediaEvidenceError):
        media[1].control_from_agent(media[2], agent_turn_id=turn["agent_turn_id"])
    assert media[1].read(media[2], media_id=row["media_id"])["generation"] == row["generation"]
