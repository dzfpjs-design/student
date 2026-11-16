import pytest

from home_study_agent.agent_orchestrator import AgentOrchestrator
from test_app import client_for
from test_companion_memory_integration import RecordingUnavailableGateway, say
from test_daily_learning_closure_audit import learning_clock, learning_day, advance_day
from test_cloud_first_agent import Planner


def set_state(client, sid, state):
    with client.app.state.agent_orchestrator.database.connect() as connection:
        connection.execute("UPDATE daily_session SET state=? WHERE session_id=?", (state, sid))
        connection.commit()


def test_daily_summary_voice_preview_close_recovery_and_child_isolation(tmp_path, learning_clock):
    gateway = RecordingUnavailableGateway()
    child = "child-primary-4"
    day = learning_day(learning_clock)
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        repo = client.app.state.repository
        sid = repo.start_session(child, "summary-session")["session_id"]
        set_state(client, sid, "classroom_recall")
        repo.add_recall(sid, "数学", "今天练习先括号再乘除的运算顺序", "summary-recall")
        set_state(client, sid, "homework_capture")
        repo.add_homework(sid, "数学", "混合运算练习第3题", 15, "written", "第3题", None, None, None, None, "summary-homework")
        set_state(client, sid, "study")
        attempt = client.app.state.learning_state.submit_diagnostic(sid, "diag-p4-arithmetic-closed-1", "12", "summary-wrong")
        assert attempt["correctness"] == 0
        preview = client.get(f"/v1/sessions/{sid}/learning-summary")
        assert preview.status_code == 200, preview.text
        assert preview.json()["cloud_transmission_allowed"] is False
        assert "第3题" in preview.json()["child_response"]
        turn = say(client, sid, "总结今天的学习", "summary-voice")
        assert turn["intent"] == "daily_learning_summary", turn
        assert "第3题" in turn["response_text"]
        assert turn["action"]["local_only"] is True
        assert gateway.requests == []
        assert client.get(f"/v1/children/{child}/daily-learning-summary?learning_date=bad").status_code == 422
        set_state(client, sid, "reflection")
        payload = {"learned_text": "", "difficult_text": "运算顺序还没弄懂", "client_action_id": "summary-finish"}
        completed = client.post(f"/v1/sessions/{sid}/reflection/complete", json=payload)
        assert completed.status_code == 200, completed.text
        retried = client.post(f"/v1/sessions/{sid}/reflection/complete", json=payload)
        assert retried.status_code == 200, retried.text
        daily = client.get(f"/v1/children/{child}/daily-learning-summary", params={"learning_date": day}).json()
        assert daily["status"] == "ready", daily
        assert len(daily["sessions"]) == 1
        assert daily["sessions"][0]["remediation"]
        assert any(row["source_evidence_id"] == attempt["evidence_id"] for row in daily["sessions"][0]["remediation"] if row["source_type"] == "due_review")
        assert client.get(f"/v1/children/child-junior-3/daily-learning-summary").json()["sessions"] == []
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        recovered = client.get(f"/v1/children/{child}/daily-learning-summary", params={"learning_date": day}).json()
        assert recovered["status"] == "ready"
        assert "第3题" in recovered["child_response"]
        assert recovered["sessions"][0]["remediation"] == daily["sessions"][0]["remediation"]
        with client.app.state.agent_orchestrator.database.connect() as connection:
            replay = AgentOrchestrator._get_replayed(connection, turn["agent_turn_id"])
        assert "第3题" not in replay["response_text"]
        assert replay["action"]["cloud_transmission_allowed"] is False


def test_daily_summary_is_not_an_exam_answer_or_unsolicited_lesson():
    for state in ("check_in", "study", "reflection"):
        intent, _, action = AgentOrchestrator._classify(state, "明天怎么复习", 5)
        assert intent == "daily_learning_summary" and action["type"] == "none"
    intent, _, action = AgentOrchestrator._classify("study", "停止监听，总结今天的学习", 5)
    assert intent == "privacy_pause" and action["type"] == "privacy_pause"


@pytest.mark.parametrize(
    "utterance,expected_intent,expected_action",
    [
        ("不知道", "submit_diagnostic_answer", "submit_diagnostic_answer"),
        ("-23", "submit_diagnostic_answer", "submit_diagnostic_answer"),
        ("停止监听", "privacy_pause", "privacy_pause"),
        ("总结今天的学习", "daily_learning_summary", "none"),
    ],
    ids=["abstention", "bare-answer", "privacy-control", "daily-summary"],
)
def test_active_due_review_voice_answers_preserve_conversation_controls(
    tmp_path, learning_clock, utterance, expected_intent, expected_action,
):
    gateway = Planner("diagnostic.submit", {"answer": utterance}, utterance,
                      role="answer", objective_type="learning")
    child = "child-primary-4"
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        repo = client.app.state.repository
        learning = client.app.state.learning_state
        first_sid = repo.start_session(child, "review-voice-first-day")["session_id"]
        set_state(client, first_sid, "study")
        original = learning.submit_diagnostic(first_sid, "diag-p4-arithmetic-closed-1", "12", "review-voice-original")
        assert original["correctness"] == 0
        set_state(client, first_sid, "reflection")
        repo.complete_with_reflection(first_sid, "", "", "", "", False, "review-voice-close")
        closure = learning.close_session_learning(first_sid)
        review = next(item for item in closure["remediation"] if item["source_type"] == "due_review")
        assert review["source_evidence_id"] == original["evidence_id"]
        assert review["diagnostic_item_id"] == "diag-p4-arithmetic-closed-2"

        advance_day(learning_clock)
        sid = repo.start_session(child, "review-voice-next-day")["session_id"]
        with repo.database.connect() as connection:
            connection.execute("""UPDATE daily_session SET state='homework_capture',
                homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?""", (sid,))
            connection.execute("UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id=?", (child,))
            connection.commit()
        workspace = repo.generate_plan(sid, "review-voice-plan")
        item = next(item for item in workspace["plan_items"] if item["source_type"] == "due_review")
        assert item["source_ref"] == review["source_ref"]
        set_state(client, sid, "study")
        started = client.post(f"/v1/plan-items/{item['plan_item_id']}/start", json={"client_action_id": "review-voice-start"})
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "active"
        question = client.get(f"/v1/plan-items/{item['plan_item_id']}/review-question")
        assert question.status_code == 200, question.text
        assert question.json()["review_task_id"] == review["source_ref"]
        assert question.json()["review_status"] == "pending"
        prepared = client.post(f"/v1/sessions/{sid}/diagnostic-prompt/prepare", json={
            "track": "school", "preferred_item_id": question.json()["diagnostic_item_id"],
            "review_task_id": review["source_ref"], "client_action_id": "review-voice-prompt",
        })
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["status"] == "waiting_answer"

        # Inspect orchestration only: say() would claim completion before executing the action.
        response = client.post(f"/v1/sessions/{sid}/agent-turn", json={
            "transcript": utterance, "client_action_id": "review-voice-utterance",
        })
        assert response.status_code == 200, response.text
        turn = response.json()
        actual = (turn["intent"], turn["action"]["type"])
        assert actual == (expected_intent, expected_action), {
            "utterance": utterance, "actual": actual,
            "is_review_answer": AgentOrchestrator._is_review_answer(utterance),
            "stateless_intent": AgentOrchestrator._classify("study", utterance, 5)[0],
        }
        if expected_action == "submit_diagnostic_answer":
            assert turn["action"]["answer"] == utterance
            assert turn["action"]["diagnostic_prompt_id"] == prepared.json()["prompt"]["prompt_id"]
            assert turn["action"]["local_only"] is True
            assert turn["action"]["cloud_transmission_allowed"] is False
            assert not gateway.requests
            answered = client.post(f'/v1/sessions/{sid}/diagnostic-prompt/answer', json={
                'prompt_id': turn['action']['diagnostic_prompt_id'],
                'item_version': turn['action']['diagnostic_item_version'], 'answer': utterance,
                'client_action_id': 'review-real-voice-answer', 'authorized_turn_id': turn['agent_turn_id']})
            assert answered.status_code == 200, answered.text
            assert answered.json()['status'] == 'answered'
            with repo.database.connect() as connection:
                assert connection.execute('SELECT count(*) FROM diagnostic_attempt WHERE session_id=?', (sid,)).fetchone()[0] == 1
        else:
            assert not gateway.requests
            assert "answer" not in turn["action"]
            with repo.database.connect() as connection:
                assert connection.execute("SELECT count(*) FROM diagnostic_attempt WHERE session_id=?", (sid,)).fetchone()[0] == 0
                assert connection.execute("SELECT status FROM review_task WHERE review_task_id=?", (review["source_ref"],)).fetchone()[0] == "pending"
