from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from test_agent_runtime import seed_session
from test_app import client_for
from test_learning_outcome_evaluator import EvaluationGateway, persist_evaluation_turn


def test_parent_can_retract_restore_evaluation_and_restart_keeps_projection(tmp_path):
    gateway = EvaluationGateway()
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        database = Database(tmp_path / "test.db")
        sid = seed_session(database)
        persist_evaluation_turn(database, sid, "review-eval", "review-eval-action", "new", "diagnose")
        response = client.post("/v1/agent-turns/review-eval/execute")
        assert response.status_code == 200, response.text
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        snapshot_url = "/v1/parent/children/child-runtime/longitudinal-model"
        original = client.get(snapshot_url, headers=headers).json()
        projection = original["payload"]["tutor_outcomes"]
        assert projection['candidate_count'] == 1 and projection['unmapped_count'] == 0
        assert projection['level_counts'] == {}
        assert projection['recent_outcomes'][0]['mastery_gate']['missing'] == ['reliable_response_receipt_missing']
        oid = projection["recent_outcomes"][0]["outcome_id"]
        path = f"/v1/parent/children/child-runtime/tutor-outcomes/{oid}/review"
        payload = {"decision": "revoke", "reason": "本次回答是跟读，不是独立作答", "client_action_id": "review-revoke-once"}
        assert client.post(path, json=payload).status_code == 401
        revoked = client.post(path, headers=headers, json=payload)
        assert revoked.status_code == 200, revoked.text
        model = revoked.json()["model"]
        assert model["evidence_fingerprint"] != original["evidence_fingerprint"]
        assert model["payload"]["tutor_outcomes"]["level_counts"] == {}
        assert model["payload"]["tutor_outcomes"]["recent_outcomes"][0]["evidence_status"] == "revoked"
        assert client.post(path.replace("child-runtime", "child-primary-4"), headers=headers, json={**payload, "client_action_id": "cross-child-review"}).status_code == 409
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        token = client.post("/v1/parent/unlock", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        assert client.get(snapshot_url, headers=headers).json()["payload"]["tutor_outcomes"]["level_counts"] == {}
        restored = client.post(path, headers=headers, json={"decision": "restore", "reason": "复核原回答后恢复模型评价，不等同掌握", "client_action_id": "review-restore-once"})
        assert restored.status_code == 200, restored.text
        restored_projection = restored.json()['model']['payload']['tutor_outcomes']
        assert restored_projection['level_counts'] == {}
        assert restored_projection['candidate_count'] == 1
        assert restored_projection['recent_outcomes'][0]['certification_status'] == 'candidate'
        assert restored_projection['recent_outcomes'][0]['mastery_gate']['missing'] == ['reliable_response_receipt_missing']
        client.app.state.privacy_lifecycle.delete_child_scope("child-runtime", "tutor", "确认删除", "delete-eval-memory")
        with Database(tmp_path / "test.db").connect() as connection:
            for table in ("agent_outcome", "learning_outcome_review_event", "learning_evaluation_execution", "agent_turn"):
                assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
            # Snapshots are shared immutable audit inputs for plans and reports;
            # tutor-scope deletion removes the source rows but does not rewrite
            # those historical receipts. The current projection must resolve
            # to a source-free snapshot after deletion.
            assert connection.execute(
                "SELECT COUNT(*) FROM child_model_snapshot WHERE child_id=?", ("child-runtime",)
            ).fetchone()[0] > 0
        clean = ChildModelService(Database(tmp_path / "test.db")).snapshot("child-runtime")
        outcomes = clean["payload"]["tutor_outcomes"]
        assert outcomes["outcome_count"] == outcomes["candidate_count"] == 0
        assert outcomes["recent_outcomes"] == []
    assert len(gateway.requests) == 1


def test_saved_model_response_recovers_on_restart_without_another_model_call(tmp_path, monkeypatch):
    gateway = EvaluationGateway()
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        database = Database(tmp_path / "test.db")
        sid = seed_session(database)
        persist_evaluation_turn(database, sid, "recover-eval", "recover-eval-action", "new", "diagnose")
        def fail_local_persist(*args, **kwargs):
            raise RuntimeError("synthetic disk interruption")
        monkeypatch.setattr(client.app.state.learning_outcome_evaluator, "_persist", fail_local_persist)
        response = client.post("/v1/agent-turns/recover-eval/execute")
        assert response.status_code == 409, response.text
        assert client.get("/v1/agent-turns/recover-eval/execution-status").json()["status"] == "in_progress"
    with client_for(tmp_path, model_gateway=gateway, agent_runtime_enabled=True) as client:
        result = client.get("/v1/agent-turns/recover-eval/execution-status")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "completed"
        assert result.json()["execution"]["turn"]["tool_status"] == "completed"
    assert len(gateway.requests) == 1
