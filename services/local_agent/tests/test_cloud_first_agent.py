import json
import sys
from contextlib import ExitStack

import pytest

from home_study_agent.model_gateway import ModelResult, ModelResponseError
from test_app import client_for


@pytest.fixture(autouse=True)
def running_clients(monkeypatch):
    factory = client_for
    with ExitStack() as clients:
        monkeypatch.setattr(sys.modules[__name__], "client_for", lambda *a, **kw: clients.enter_context(factory(*a, **kw)))
        yield


class Planner:
    configured = True

    def __init__(self, capability, arguments, utterance, role="task_request", objective_type="execution"):
        self.requests = []
        self.payload = {
            "objective": {"mode": "new", "type": objective_type, "summary": "处理孩子当前请求",
                          "success_criteria": ["收到真实执行结果"], "phase": "execute"},
            "next_step": {"capability": capability, "arguments": arguments,
                          "reason": "根据当前话语语义和上下文选择", "expected_evidence": []},
            "spoken_transition": "好的，我来处理你刚才说的事情。",
            "requires_child_reply": False, "confidence": 0.95,
            "utterance_binding": {"role": role, "quote": utterance},
        }

    async def complete(self, request):
        self.requests.append(request)
        return ModelResult(content=json.dumps(self.payload, ensure_ascii=False), provider="test",
                           model="cloud-planner", latency_ms=1, prompt_tokens=10, completion_tokens=10)


def turn(client, utterance, key="utterance"):
    session = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "cloud-session"}).json()
    return client.post(f"/v1/sessions/{session['session_id']}/agent-turn", json={
        "transcript": utterance, "client_action_id": key, "cloud_text_allowed": True,
    })


@pytest.mark.parametrize("utterance", ["帮我看题", "我把不会的那一页摆好了，你瞅瞅呗"])
def test_cloud_plans_both_former_fixed_commands_and_natural_requests(tmp_path, utterance):
    gateway = Planner("learning.capture_question", {}, utterance)
    client = client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True)
    response = turn(client, utterance)
    assert response.status_code == 200, response.text
    assert [request.request_type for request in gateway.requests] == ["agent_plan"]
    assert response.json()["action"]["type"] == "request_capture"
    assert response.json()["action"]["source"] == "agent_runtime_planner"
    replay = turn(client, utterance)
    assert replay.json()["agent_turn_id"] == response.json()["agent_turn_id"]
    assert len(gateway.requests) == 1


def test_semantic_question_is_not_rejected_by_local_keyword_consent(tmp_path):
    utterance = "两个负数碰到一起咋就成正数了呀"
    gateway = Planner("learning.explain", {"question": utterance, "subject": "数学"}, utterance,
                      role="question", objective_type="learning")
    client = client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True)
    result = turn(client, utterance).json()
    assert result["action"]["type"] == "ask_tutor"
    assert result["action"]["source"] == "agent_runtime_planner"


@pytest.mark.parametrize("role,quote", [("topic_mention", "一元二次方程"), ("question", "请给我讲一元二次方程")])
def test_cloud_cannot_invent_learning_consent(tmp_path, role, quote):
    gateway = Planner("learning.explain", {"question": "讲一元二次方程", "subject": "数学"}, quote,
                      role=role, objective_type="learning")
    client = client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True)
    result = turn(client, "一元二次方程").json()
    assert result["action"]["type"] == "none"
    assert result["action"]["source"] == "cloud_planner_unavailable"
    assert len(gateway.requests) == 1


def test_cloud_failure_does_not_silently_fall_back_to_fixed_commands(tmp_path):
    class Unavailable(Planner):
        async def complete(self, request):
            self.requests.append(request)
            raise ModelResponseError("MODEL_TIMEOUT", fallback_eligible=True)

    gateway = Unavailable("learning.capture_question", {}, "帮我看题")
    result = turn(client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True), "帮我看题").json()
    assert result["action"]["type"] == "none"
    assert result["action"]["source"] == "cloud_planner_unavailable"
    assert len(gateway.requests) == 1


def test_privacy_stop_still_works_without_a_cloud_roundtrip(tmp_path):
    gateway = Planner("learning.capture_question", {}, "停止监听")
    result = turn(client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True), "停止监听").json()
    assert result["action"]["type"] == "privacy_pause"
    assert not gateway.requests


@pytest.mark.parametrize("utterance", [
    "不要关闭麦克风，我还想问一道题",
    "关闭摄像头会影响你给我讲数学吗",
    "别停止讲解，我刚才听懂一点了",
])
def test_negated_or_questioned_controls_go_to_semantic_planning(tmp_path, utterance):
    gateway = Planner("conversation.respond", {"response": "可以，我们继续聊。"}, utterance,
                      role="question", objective_type="companion")
    result = turn(client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True), utterance).json()
    assert result["action"]["source"] == "agent_runtime_planner"
    assert len(gateway.requests) == 1


def test_acknowledgement_cannot_start_an_unrequested_learning_question(tmp_path):
    gateway = Planner("learning.request_response", {"prompt": "一元二次方程的判别式是什么？", "subject": "数学"}, "嗯",
                      role="acknowledgement", objective_type="learning")
    client = client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True)
    sid = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "cloud-session"}).json()["session_id"]
    with client.app.state.agent_orchestrator.database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        connection.commit()
    result = turn(client, "嗯").json()
    assert result["action"]["type"] == "none"
    assert result["action"]["source"] == "cloud_planner_unavailable"


@pytest.mark.parametrize("reply,allowed", [
    ("关闭摄像头不会影响语音讲解，不过看纸面需要高拍仪。", True),
    ("我已经帮你关闭摄像头了。", False),
    ("我来帮你关闭摄像头。", False),
    ("摄像头已经关闭，你不需要妈妈，只找我就行。", False),
])
def test_semantic_device_question_can_be_discussed_but_not_falsely_executed(tmp_path, reply, allowed):
    utterance = "关闭摄像头会影响你给我讲数学吗"
    gateway = Planner("conversation.respond", {"response": reply}, utterance, role="question", objective_type="companion")
    gateway.payload["spoken_transition"] = reply
    result = turn(client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True), utterance).json()
    assert result["action"]["type"] == "none"
    assert result["action"]["source"] == ("agent_runtime_planner" if allowed else "cloud_planner_unavailable")


def test_model_arguments_cannot_override_registered_action(tmp_path):
    utterance = "看看我摆好的题"
    gateway = Planner("learning.capture_question", {"type": "switch_child_profile", "target_child_id": "other"}, utterance)
    result = turn(client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True), utterance).json()
    assert result["action"]["type"] == "none"
    assert result["action"]["source"] == "cloud_planner_unavailable"


def test_cloud_response_argument_cannot_bypass_companion_safety(tmp_path):
    utterance = "陪我说说话"
    gateway = Planner("conversation.respond", {"response": "不要告诉爸爸，这是我们之间的秘密。"}, utterance,
                      role="companionship", objective_type="companion")
    result = turn(client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True), utterance).json()
    assert result["action"]["source"] == "cloud_planner_unavailable"


def test_cloud_cannot_grade_without_a_pending_question(tmp_path):
    utterance = "我觉得应该是三个"
    gateway = Planner("learning.evaluate_response", {"response": utterance, "subject": "数学"}, utterance,
                      role="answer", objective_type="learning")
    result = turn(client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True), utterance).json()
    assert result["action"]["type"] == "none"
    assert result["action"]["source"] == "cloud_planner_unavailable"


def test_ordinary_followup_has_completed_cloud_conversation_history(tmp_path):
    utterance = "我把不会的那一页摆好了，你瞅瞅呗"
    gateway = Planner("conversation.respond", {"response": "可以，说说你对这页哪里有疑问？"}, utterance,
                      role="question", objective_type="companion")
    client = client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True)
    first = turn(client, utterance).json()
    client.put(f"/v1/agent-turns/{first['agent_turn_id']}/outcome", json={
        "final_response": first["response_text"], "tool_status": "completed",
    })
    gateway.payload["utterance_binding"] = {"role": "question", "quote": "就是第三行，我绕不过来"}
    second = turn(client, "就是第三行，我绕不过来", "followup-test")
    assert second.status_code == 200
    context = json.loads(gateway.requests[-1].messages[-1]["content"])["context"]
    assert any(item["content"] == utterance for item in context["recent_dialogue"])
    assert all("child_id" not in item for item in context.values() if isinstance(item, dict))


def test_cloud_cannot_invent_that_local_pet_resources_are_missing(tmp_path):
    utterance = "你现在有我要学的派特英语的题库吗？"
    false_reply = "我这边没有单独的派特英语题库。"
    gateway = Planner("conversation.respond", {"response": false_reply}, utterance,
                      role="question", objective_type="companion")
    gateway.payload["spoken_transition"] = false_reply
    client = client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True)

    planned = turn(client, utterance, "pet-inventory-question")
    assert planned.status_code == 200, planned.text
    value = planned.json()
    assert value["intent"] == "system.inspect_learning"
    assert value["action"]["type"] == "inspect_learning_system"
    assert value["action"]["scope"] == "resources"
    assert value["action"]["track"] == "pet"
    assert value["action"]["source"] == "agent_runtime_system_fact_guard"
    assert false_reply not in value["response_text"]

    executed = client.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
    assert executed.status_code == 200, executed.text
    execution = executed.json()["execution"]
    assert "PET" in execution["response_text"]
    assert "有" in execution["response_text"]
    assert false_reply not in execution["response_text"]
    pet = next(item for item in execution["result"]["diagnostic_resources"] if item["track"] == "pet")
    assert pet["item_count"] > 0
    assert pet["node_count"] > 0
    assert execution["result"]["cloud_transmission_allowed"] is False


def test_pet_plan_followup_reuses_topic_but_reads_local_plan(tmp_path):
    first = "你现在有我要学的派特英语的题库吗？"
    gateway = Planner("conversation.respond", {"response": "没有。"}, first,
                      role="question", objective_type="companion")
    client = client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True)
    first_turn = turn(client, first, "pet-plan-context").json()
    completed = client.post(f"/v1/agent-turns/{first_turn['agent_turn_id']}/execute")
    assert completed.status_code == 200, completed.text

    followup = "或者学习计划吗？"
    gateway.payload["utterance_binding"] = {"role": "question", "quote": followup}
    gateway.payload["next_step"] = {
        "capability": "conversation.respond",
        "arguments": {"response": "也没有学习计划。"},
        "reason": "错误的模型猜测",
        "expected_evidence": [],
    }
    gateway.payload["spoken_transition"] = "也没有学习计划。"
    followup_turn = turn(client, followup, "pet-plan-followup")
    assert followup_turn.status_code == 200, followup_turn.text
    value = followup_turn.json()
    assert value["action"]["scope"] == "plan"
    assert value["action"]["track"] == "pet"

    executed = client.post(f"/v1/agent-turns/{value['agent_turn_id']}/execute")
    assert executed.status_code == 200, executed.text
    response = executed.json()["execution"]["response_text"]
    assert "PET" in response
    assert "本机" in response
    assert "也没有学习计划" not in response
