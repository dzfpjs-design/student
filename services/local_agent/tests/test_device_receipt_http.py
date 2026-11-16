from test_app import advance_to_study, client_for


def test_capture_success_without_artifact_is_rejected_and_turn_unchanged(tmp_path):
    with client_for(tmp_path, agent_runtime_enabled=True) as client:
        session, _ = advance_to_study(client)
        turn = client.post(f"/v1/sessions/{session}/agent-turn", json={
            "transcript": "帮我拍题", "client_action_id": "capture-without-proof",
        }).json()
        assert turn["action"]["type"] == "request_capture"
        result = client.put(f"/v1/agent-turns/{turn['agent_turn_id']}/outcome", json={
            "final_response": "已经拍好了", "tool_status": "completed",
        })
        assert result.status_code == 409, result.text
        assert result.json()["code"] == "DEVICE_RECEIPT_REQUIRED"
        with client.app.state.agent_orchestrator.database.connect() as connection:
            saved = connection.execute("SELECT tool_status,completed_at FROM agent_turn WHERE agent_turn_id=?", (turn["agent_turn_id"],)).fetchone()
            assert saved["tool_status"] != "completed"
            assert saved["completed_at"] is None
