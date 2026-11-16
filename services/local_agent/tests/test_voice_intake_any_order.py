import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository, StaleSessionStateError
from test_app import client_for
from test_cloud_first_agent import Planner


def test_new_session_cloud_homework_is_saved_without_stage_commands(tmp_path):
    utterance = "语文读第三课，数学做第十页，先帮我记住"
    gateway = Planner("homework.add_batch", {"items": [
        {"subject": "语文", "title": "读第三课", "expected_minutes": 10},
        {"subject": "数学", "title": "做第十页", "expected_minutes": 20},
    ]}, utterance)
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        sid = client.post("/v1/sessions", json={"child_id": "child-primary-4", "client_action_id": "intake-session"}).json()["session_id"]
        turn = client.post(f"/v1/sessions/{sid}/agent-turn", json={"transcript": utterance, "client_action_id": "intake-voice"}).json()
        assert turn["action"]["type"] == "add_homework_batch"
        for _ in range(2):
            result = client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute", json={})
            assert result.status_code == 200, result.text
        repo = client.app.state.repository
        assert repo.get_session(sid)["state"] == "check_in"
        repo.add_recall(sid, "数学", "今天学了小数", "early-recall")
        with repo.database.connect() as db:
            assert db.execute("SELECT count(*) FROM homework_item WHERE session_id=?", (sid,)).fetchone()[0] == 2
            assert tuple(db.execute("SELECT recall_declared_empty,homework_declared_empty FROM daily_session WHERE session_id=?", (sid,)).fetchone()) == (0, 0)
            recalls = [tuple(row) for row in db.execute("SELECT recall_id,record_kind,confirmation_status,summary FROM classroom_recall WHERE session_id=? ORDER BY recall_id", (sid,))]
            assert sum(row[1] == "child_statement" for row in recalls) == 1
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        assert client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute", json={}).status_code == 200
        with client.app.state.repository.database.connect() as db:
            assert db.execute("SELECT count(*) FROM homework_item WHERE session_id=?", (sid,)).fetchone()[0] == 2
            assert [tuple(row) for row in db.execute("SELECT recall_id,record_kind,confirmation_status,summary FROM classroom_recall WHERE session_id=? ORDER BY recall_id", (sid,))] == recalls
        assert len(gateway.requests) == 1


@pytest.mark.parametrize("state", ["check_in", "classroom_recall", "homework_capture", "plan_review", "study", "completed"])
def test_only_intake_stages_accept_new_inputs_without_replanning(tmp_path, state):
    db = Database(tmp_path / "intake.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    sid = repo.start_session("child-primary-4", "session")["session_id"]
    with db.connect() as conn:
        conn.execute("UPDATE daily_session SET state=? WHERE session_id=?", (state, sid))
        conn.commit()
    def save():
        repo.add_recall(sid, "语文", "修辞", "recall")
        repo.add_homework(sid, "数学", "练习", 15, "written", None, None, None, None, None, "homework")
    if state in {"check_in", "classroom_recall", "homework_capture"}:
        save()
        assert repo.get_session(sid)["state"] == state
    else:
        with pytest.raises(StaleSessionStateError):
            save()
