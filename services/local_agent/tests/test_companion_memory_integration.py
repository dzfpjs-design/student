import json

from home_study_agent.agent_orchestrator import AgentOrchestrator
from home_study_agent.model_gateway import ModelResponseError, ModelResult
from test_app import client_for


class RecordingUnavailableGateway:
    configured = True

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if request.request_type in {"voice_agent_intent", "companion_dialogue"}:
            return ModelResult(content='{"intent":"supportive_chat","confidence":0.98}' if request.request_type == "voice_agent_intent" else "好，你今天有什么想分享的吗？",
                               provider="test", model="recording", latency_ms=1, prompt_tokens=1, completion_tokens=1)
        raise ModelResponseError("MODEL_TIMEOUT", fallback_eligible=False)


def say(client, sid, text, action):
    response = client.post(f"/v1/sessions/{sid}/agent-turn", json={"transcript": text, "client_action_id": action})
    assert response.status_code == 200, response.text
    turn = response.json()
    completed = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
        "final_response": turn["response_text"], "tool_status": "completed",
    })
    assert completed.status_code == 200, completed.text
    return completed.json()


def test_voice_memory_restart_isolation_parent_forget_and_no_cloud_history(tmp_path):
    gateway = RecordingUnavailableGateway()
    url = "/v1/parent/children/child-primary-4/companion-memory"
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid = client.app.state.repository.start_session("child-primary-4", "memory-session")["session_id"]
        with client.app.state.agent_orchestrator.database.connect() as connection:
            connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            connection.commit()
        remembered = say(client, sid, "记住我喜欢画图讲解", "remember-visual")
        assert remembered["intent"] == "memory_command"
        assert gateway.requests == []
        assert client.app.state.companion_memory.teaching_preferences("child-primary-4") == {"explanation_style": "visual"}
        assert client.app.state.companion_memory.recall("child-junior-3", "")["memories"] == []
        assert client.get(url).status_code == 401
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        listing = client.get(url, headers=headers).json()
        assert listing["cloud_transmission_allowed"] is False
        assert len(listing["items"]) == 1
        mid = listing["items"][0]["memory_id"]
        assert listing["items"][0]["source_text"]
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        token = client.post("/v1/parent/unlock", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        assert client.get(url, headers=headers).json()["items"][0]["memory_id"] == mid
        recalled = say(client, sid, "你记得我喜欢什么吗", "recall-next-boot")
        assert "画图" in recalled["response_text"]
        assert recalled["action"]["local_only"] is True
        tts = client.post(f"/v1/sessions/{sid}/voice/synthesize/stream", json={
            "text": recalled["response_text"], "client_action_id": "unsafe-memory-tts",
            "source_agent_turn_id": recalled["agent_turn_id"],
        })
        assert tts.status_code == 409, tts.text
        assert "LOCAL_ONLY_SPEECH_REQUIRED" in tts.text
        for index, text in enumerate(("再说一次：" + recalled["response_text"], "画图讲解是我喜欢的方式")):
            legacy = client.post(f"/v1/sessions/{sid}/voice/synthesize/stream", json={
                "text": text, "client_action_id": f"legacy-memory-{index}",
            })
            assert legacy.status_code == 409, legacy.text
        assert gateway.requests == []
        say(client, sid, "今天我们随便聊聊吧", "chat-after-recall")
        assert gateway.requests
        planner_payloads = [json.loads(request.messages[-1]["content"]) for request in gateway.requests if request.request_type == "agent_plan"]
        assert any(payload["context"]["teaching_preferences"].get("explanation_style") == "visual" for payload in planner_payloads)
        for request in gateway.requests:
            messages = json.dumps(list(request), ensure_ascii=False)
            assert "记住我喜欢画图讲解" not in messages
            assert recalled["response_text"] not in messages
        deleted = client.request("DELETE", f"{url}/{mid}", headers=headers, json={"client_action_id": "parent-forget"})
        assert deleted.status_code == 200, deleted.text
        assert client.get(url, headers=headers).json()["items"] == []
        replayed = say(client, sid, "你记得我喜欢什么吗", "recall-next-boot")
        assert "画图" not in replayed["response_text"]
        client.app.state.companion_memory.process_pending(50)
        assert client.app.state.companion_memory.teaching_preferences("child-primary-4") == {}
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        assert client.app.state.companion_memory.recall("child-primary-4", "")["memories"] == []


def test_child_history_deletion_covers_memory_and_cannot_recover_other_child(tmp_path):
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        memory = client.app.state.companion_memory
        repository = client.app.state.repository
        sid = repository.start_session("child-primary-4", "delete-memory-session")["session_id"]
        say(client, sid, "记住我喜欢画图讲解", "delete-memory-entry")
        say(client, sid, "忘记我喜欢画图讲解", "delete-memory-forget")
        with client.app.state.agent_orchestrator.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM companion_memory_suppression WHERE child_id=?", ("child-primary-4",)).fetchone()[0] > 0
        privacy = client.app.state.privacy_lifecycle
        assert privacy.deletion_preview("child-primary-4")["companion_memory"] > 0
        privacy.delete_child_scope("child-primary-4", "all_history", "确认删除", "delete-all-memory")
        assert privacy.deletion_preview("child-primary-4")["companion_memory"] == 0
        with client.app.state.agent_orchestrator.database.connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM companion_memory_suppression WHERE child_id=?", ("child-primary-4",)).fetchone()[0] == 0
        memory.process_pending(50)
        assert memory.recall("child-primary-4", "")["memories"] == []


def test_real_repetition_and_chinese_number_are_not_discarded():
    spoken = "正弦是对边除以斜边，余弦是邻边除以斜边。"
    mixed = spoken + "但是我还没有弄懂为什么。"
    assert AgentOrchestrator._strip_recent_assistant_echo(mixed, (spoken,)) == (mixed, 0)
    assert AgentOrchestrator._should_bind_learning_reply("unknown", "四", {"type": "none"})
    repeated = "正弦是对边除以斜边"
    assert AgentOrchestrator._strip_recent_assistant_echo(repeated, (spoken,)) == (repeated, 0)
    assert not AgentOrchestrator._should_bind_learning_reply("unknown", repeated, {"type": "none"}, previous_response=spoken)
    assert AgentOrchestrator._should_bind_learning_reply("unknown", "我的答案是" + repeated, {"type": "none"}, previous_response=spoken)


def test_personal_memory_does_not_steal_working_dialogue_or_question_recall():
    from home_study_agent.companion_memory import CompanionMemory
    for text in ("你还记得它吗", "你记得刚才我卡在哪道错题吗", "你记得这题吗"):
        assert not CompanionMemory.is_command(text)
    for text in ("你记得我喜欢什么吗", "记住我喜欢画图讲解", "忘记我刚才的错题经历"):
        assert CompanionMemory.is_command(text)
