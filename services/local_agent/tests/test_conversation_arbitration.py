from pathlib import Path

import pytest

from test_app import client_for


@pytest.mark.parametrize("owner", ["pet", "exam", "mistake", "story"])
@pytest.mark.parametrize("utterance,intent", [
    ("你听到我的声音了吗？", "audio_check"),
    ("对吧？", "confirmation_followup"),
    ("喂", "attention_check"),
    ("你为什么听不明白我说的话呢", "conversation_repair"),
    ("我想伤害自己", "urgent_safety_support"),
])
def test_activity_cannot_grade_conversation_control(tmp_path: Path, monkeypatch, owner, utterance, intent):
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        sid = client.app.state.repository.start_session("child-primary-4", "arbitration-session")["session_id"]
        orchestrator = client.app.state.agent_orchestrator
        with orchestrator.database.connect() as connection:
            connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            connection.commit()
        if owner == "pet":
            monkeypatch.setattr(orchestrator.pet_vocabulary, "active_prompt", lambda _: {"card_id": "waiting"})
        elif owner == "exam":
            monkeypatch.setattr(orchestrator.exam_simulation, "active_for_session", lambda _: {"status": "active"})
        elif owner == "mistake":
            from home_study_agent.mistake_dialogue import MistakeDialogueService
            monkeypatch.setattr(MistakeDialogueService, "state", lambda *_: {"phase": "awaiting_answer"})
        else:
            original = orchestrator.agent_runtime.assemble_context
            def context(*args, **kwargs):
                result = original(*args, **kwargs)
                result["active_companion_activity"] = {"activity_type": "interactive_story", "remaining_turns": 3}
                return result
            monkeypatch.setattr(orchestrator.agent_runtime, "assemble_context", context)
        response = client.post(f"/v1/sessions/{sid}/agent-turn", json={
            "transcript": utterance, "client_action_id": f"control-{owner}-{intent}",
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["intent"] == intent
        assert body["action"]["type"] == ("find_parent_urgent" if intent == "urgent_safety_support" else "none")
        assert "learning.evaluate_response" not in str(body["action"])


def test_stop_releases_persisted_mistake_focus_across_restart(tmp_path):
    from test_mistake_voice_journey import fixture, begin, focus
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        sid, _ = fixture(client)
        begin(client, sid)
        turn = client.post(f"/v1/sessions/{sid}/agent-turn", json={
            "transcript": "别讲了", "client_action_id": "stop-mistake-voice",
        }).json()
        assert turn["action"]["type"] == "stop_speech"
        finished = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
            "final_response": turn["response_text"], "tool_status": "completed",
            "result": {"execution_receipt": {
                "capability": "speech.stop", "execution_location": "client_device_bridge",
                "action_type": "stop_speech", "status": "completed",
                "completed_at": turn["created_at"], "output": {"speech_stopped": True},
            }},
        })
        assert finished.status_code == 200, finished.text
        assert focus(client, sid)["phase"] == "paused"
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        assert focus(client, sid)["phase"] == "paused"
        following = client.post(f"/v1/sessions/{sid}/agent-turn", json={
            "transcript": "你听到我的声音了吗？", "client_action_id": "after-stop-mistake",
        }).json()
        assert following["intent"] == "audio_check"
        assert focus(client, sid)["phase"] == "paused"


def test_paused_exam_does_not_own_new_conversation(tmp_path, monkeypatch):
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        sid = client.app.state.repository.start_session("child-primary-4", "paused-exam-session")["session_id"]
        orchestrator = client.app.state.agent_orchestrator
        monkeypatch.setattr(orchestrator.exam_simulation, "active_for_session", lambda _: {"status": "paused"})
        turn = client.post(f"/v1/sessions/{sid}/agent-turn", json={
            "transcript": "我喜欢画画", "client_action_id": "paused-exam-chat",
        }).json()
        assert turn["intent"] not in {"submit_exam_answer", "exam_waiting_resume"}
