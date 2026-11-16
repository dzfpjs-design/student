from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.agent_runtime import context_manifest
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.specialized_program import SpecializedProgramService
from home_study_agent.strategy_evolution import StrategyEvolutionService


def _client(data_dir: Path) -> TestClient:
    return TestClient(create_app(Settings(
        data_dir=data_dir,
        database_path=data_dir / "child-model.db",
        data_encryption_key=b"m" * 32,
        maintenance_enabled=False,
    )))


def _parent_headers(client: TestClient) -> dict[str, str]:
    token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
    return {"X-HSC-Parent": token}


def test_longitudinal_model_is_child_isolated_evidence_only_and_versioned(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    with _client(tmp_path) as client:
        headers = _parent_headers(client)
        cold = client.get(
            "/v1/parent/children/child-junior-3/longitudinal-model", headers=headers
        )
        assert cold.status_code == 200
        first = cold.json()
        assert first["model_version"] == "evidence_child_model_v6"
        assert first["payload"]["schema_version"] == "child_state_snapshot_v6"
        assert set(first["payload"]["state_domains"]) == {
            "knowledge", "learning_process", "habits", "daily_state", "interaction",
        }
        assert first["payload"]["state_domains"]["daily_state"]["status"] == "unknown"
        assert first["payload"]["state_domains"]["daily_state"]["evidence_refs"] == []
        assert first["payload"]["evidence_level"] == "cold_start"
        assert first["payload"]["learning"]["evidenced_node_count"] == 0

        database = Database(tmp_path / "child-model.db")
        with database.connect() as connection:
            connection.execute(
                """
                INSERT INTO mastery_state(
                  child_id, knowledge_node_id, mastery_probability, evidence_count,
                  hint_dependency, status_confidence, last_practiced_at,
                  last_independent_correct_at, forgetting_rate, next_review_at,
                  algorithm_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "child-junior-3", "node-j3-math-discriminant", .42, 3, .66, .82,
                    now.isoformat(), None, .2, (now - timedelta(hours=1)).isoformat(),
                    "test-v1", now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO learning_evidence(
                  evidence_id, child_id, session_id, knowledge_node_id, evidence_type,
                  correctness, hint_level, independent, confidence, evidence_summary,
                  occurred_at, client_action_id
                ) VALUES ('evidence-child-model-1', 'child-junior-3', NULL,
                  'node-j3-math-discriminant', 'simulated_diagnostic', .25, 3, 0, .9,
                  '模拟证据：判别式符号判断需要较多提示。', ?, 'child-model-evidence-action-1')
                """,
                (now.isoformat(),),
            )
            connection.commit()

        changed = client.get(
            "/v1/parent/children/child-junior-3/longitudinal-model", headers=headers
        ).json()
        assert changed["snapshot_id"] != first["snapshot_id"]
        math = changed["payload"]["learning"]["subjects"][0]
        assert math["subject"] == "数学"
        assert math["weak_nodes"] == 1
        assert math["due_reviews"] == 1
        assert changed["payload"]["support_strategy"]["explanation_pacing"] == "standard_layered"
        assert changed["payload"]["learning"]["reliable_feedback"]["status"] == "needs_baseline"
        assert changed["payload"]["learning"]["evidenced_node_count"] == 0
        assert "personality" in changed["payload"]["support_strategy"]["do_not_infer"]
        assert "raw_audio" not in str(changed)
        assert "raw_video" not in str(changed)

        unchanged = client.get(
            "/v1/parent/children/child-junior-3/longitudinal-model", headers=headers
        ).json()
        assert unchanged["snapshot_id"] == changed["snapshot_id"]
        primary = client.get(
            "/v1/parent/children/child-primary-4/longitudinal-model", headers=headers
        ).json()
        assert primary["payload"]["learning"]["evidenced_node_count"] == 0
        assert primary["snapshot_id"] != changed["snapshot_id"]

        history = client.get(
            "/v1/parent/children/child-junior-3/longitudinal-model/history?limit=10",
            headers=headers,
        ).json()["items"]
        assert [item["snapshot_id"] for item in history] == [
            changed["snapshot_id"], first["snapshot_id"]
        ]


def test_longitudinal_model_survives_restart_and_runtime_records_snapshot(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        headers = _parent_headers(client)
        snapshot = client.get(
            "/v1/parent/children/child-primary-4/longitudinal-model", headers=headers
        ).json()

    with _client(tmp_path) as restarted:
        token = restarted.post("/v1/parent/unlock", json={"pin": "2468"}).json()["parent_token"]
        persisted = restarted.get(
            "/v1/parent/children/child-primary-4/longitudinal-model/history",
            headers={"X-HSC-Parent": token},
        )
        assert persisted.status_code == 200
        assert persisted.json()["items"][0]["snapshot_id"] == snapshot["snapshot_id"]

        session = restarted.post(
            "/v1/sessions",
            json={"child_id": "child-primary-4", "client_action_id": "child-model-runtime-start"},
        ).json()
        context = restarted.app.state.agent_runtime.assemble_context(session["session_id"], ())
        runtime_snapshot_id = context["longitudinal_child_model"]["snapshot_id"]
        assert runtime_snapshot_id != snapshot["snapshot_id"]
        assert context["longitudinal_child_model"]["snapshot_persisted"] is True
        assert context["longitudinal_child_model"]["evidence_fingerprint"]
        assert context["longitudinal_child_model"]["model_version"] == "evidence_child_model_v6"

        manifest = context_manifest(context)
        assert manifest["child_model_snapshot_id"] == runtime_snapshot_id

        with Database(tmp_path / "child-model.db").connect() as connection:
            current = connection.execute(
                """SELECT snapshot_id,evidence_fingerprint FROM child_model_snapshot
                   WHERE child_id='child-primary-4' AND is_current=1"""
            ).fetchone()
            audit_count = connection.execute(
                """
                SELECT COUNT(*) FROM audit_event
                WHERE child_id = 'child-primary-4' AND action = 'child_model_snapshot_created'
                """
            ).fetchone()[0]
        assert current["snapshot_id"] == runtime_snapshot_id
        assert current["evidence_fingerprint"] == context["longitudinal_child_model"]["evidence_fingerprint"]
        assert audit_count == 2


def test_longitudinal_model_unifies_specialized_and_bounded_evolution_evidence(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        headers = _parent_headers(client)
        database = Database(tmp_path / "child-model.db")
        StrategyEvolutionService(database).ensure_candidate("child-primary-4")
        SpecializedProgramService(database).record_assessment(
            "child-primary-4",
            "competition_math",
            "数学",
            "parent_reported_baseline",
            {"competition_math.logic": .4, "competition_math.pattern": .8},
            "child-model-specialized-1",
            score=1.2,
            max_score=2,
        )
        now = datetime.now(timezone.utc).isoformat()
        with database.connect() as connection:
            connection.execute(
                """INSERT INTO habit_experiment(
                     experiment_id,child_id,target_state,title,child_action,success_signal,
                     duration_days,baseline_json,status,proposed_at,started_at,ends_at,closed_at,outcome_json
                   ) VALUES('habit-model-1','child-primary-4','stall','先写第一步','停住时只写已知条件',
                     '出现书写候选',7,'{}','completed',?,?,?,?,?)""",
                (now, now, now, now, '{"direction":"improving","observed_days":7}'),
            )
            connection.commit()

        model = client.get(
            "/v1/parent/children/child-primary-4/longitudinal-model", headers=headers
        ).json()
        context = client.app.state.agent_runtime.assemble_context(
            client.post(
                "/v1/sessions",
                json={"child_id": "child-primary-4", "client_action_id": "unified-model-session"},
            ).json()["session_id"],
            (),
        )["longitudinal_child_model"]

    assert model["payload"]["specialized_progress"][0]["weak_domain_count"] == 1
    assert model["payload"]["behavior"]["habit_experiments"]["completed_count"] == 1
    # A cached legacy direction without a supported goal and current proofs is not improvement.
    assert model["payload"]["behavior"]["habit_experiments"]["improving_count"] == 0
    assert model["payload"]["support_strategy"]["strategy_evolution"]["status"] == "draft"
    assert context["specialized_progress"][0]["track"] == "competition_math"
    assert "outcome_json" not in str(context)


def test_parent_correction_is_durable_audited_expirable_and_reversible(tmp_path: Path) -> None:
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    payload = {
        "target_key": "task_segment_minutes",
        "value": 15,
        "reason": "长时段学习后容易遗漏中间步骤。",
        "expires_at": expires_at,
        "client_action_id": "parent-model-correction-create-1",
    }
    with _client(tmp_path) as client:
        headers = _parent_headers(client)
        baseline = client.get(
            "/v1/parent/children/child-primary-4/longitudinal-model", headers=headers
        ).json()
        baseline_minutes = baseline["payload"]["support_strategy"]["task_segment_minutes"]
        assert baseline_minutes != 15

        created = client.post(
            "/v1/parent/children/child-primary-4/longitudinal-model/corrections",
            headers=headers,
            json=payload,
        )
        assert created.status_code == 200
        body = created.json()
        correction_id = body["correction"]["correction_id"]
        assert body["model"]["payload"]["support_strategy"]["task_segment_minutes"] == 15
        assert body["model"]["payload"]["support_strategy"]["parent_correction_count"] == 1
        assert body["model"]["payload"]["parent_corrections"][0]["reason"] == payload["reason"]

        replay = client.post(
            "/v1/parent/children/child-primary-4/longitudinal-model/corrections",
            headers=headers,
            json=payload,
        )
        assert replay.status_code == 200
        assert replay.json()["correction"]["correction_id"] == correction_id
        assert replay.json()["correction"]["idempotent"] is True
        conflicting_payload = {**payload, "value": 25}
        assert client.post(
            "/v1/parent/children/child-primary-4/longitudinal-model/corrections",
            headers=headers,
            json=conflicting_payload,
        ).status_code == 409

        agent_context = client.app.state.agent_runtime.assemble_context(
            client.post(
                "/v1/sessions",
                json={"child_id": "child-primary-4", "client_action_id": "correction-agent-session"},
            ).json()["session_id"],
            (),
        )["longitudinal_child_model"]
        assert agent_context["support_strategy"]["task_segment_minutes"] == 15

        exported = client.post(
            "/v1/parent/children/child-primary-4/export", headers=headers
        ).json()
        export_payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        assert export_payload["child_model_corrections"][0]["correction_id"] == correction_id
        assert export_payload["child_model_correction_events"][0]["event_type"] == "created"

    with _client(tmp_path) as restarted:
        token = restarted.post("/v1/parent/unlock", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        listed = restarted.get(
            "/v1/parent/children/child-primary-4/longitudinal-model/corrections",
            headers=headers,
        ).json()["items"]
        assert listed[0]["status"] == "active"
        revoked = restarted.post(
            f"/v1/parent/longitudinal-model/corrections/{correction_id}/revoke",
            headers=headers,
            json={
                "reason": "已观察到孩子能稳定完成更长学习段。",
                "client_action_id": "parent-model-correction-revoke-1",
            },
        )
        assert revoked.status_code == 200
        assert revoked.json()["correction"]["status"] == "revoked"
        assert revoked.json()["model"]["payload"]["support_strategy"]["task_segment_minutes"] == baseline_minutes
        history = restarted.get(
            "/v1/parent/children/child-primary-4/longitudinal-model/history", headers=headers
        ).json()["items"]
        assert len(history) >= 3
        with Database(tmp_path / "child-model.db").connect() as connection:
            actions = {
                row[0] for row in connection.execute(
                    "SELECT action FROM audit_event WHERE child_id = 'child-primary-4'"
                ).fetchall()
            }
        assert "child_model_correction_created" in actions
        assert "child_model_correction_revoked" in actions


def test_parent_correction_expires_without_leaking_into_agent_context(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        headers = _parent_headers(client)
        created = client.post(
            "/v1/parent/children/child-junior-3/longitudinal-model/corrections",
            headers=headers,
            json={
                "target_key": "interaction_tone",
                "value": "brief",
                "reason": "复习时希望提示更简短。",
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
                "client_action_id": "parent-model-correction-expire-1",
            },
        ).json()
        assert created["model"]["payload"]["support_strategy"]["interaction_tone"] == "brief"
        correction_id = created["correction"]["correction_id"]
        with Database(tmp_path / "child-model.db").connect() as connection:
            connection.execute(
                "UPDATE child_model_correction SET expires_at = ? WHERE correction_id = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), correction_id),
            )
            connection.commit()

        refreshed = client.get(
            "/v1/parent/children/child-junior-3/longitudinal-model", headers=headers
        ).json()
        assert refreshed["payload"]["support_strategy"]["interaction_tone"] == "gentle"
        assert refreshed["payload"]["parent_corrections"] == []
        listed = client.get(
            "/v1/parent/children/child-junior-3/longitudinal-model/corrections",
            headers=headers,
        ).json()["items"]
        assert listed[0]["status"] == "expired"
        replacement = client.post(
            "/v1/parent/children/child-junior-3/longitudinal-model/corrections",
            headers=headers,
            json={
                "target_key": "interaction_tone",
                "value": "direct",
                "reason": "新一轮复习试用更明确的提示。",
                "expires_at": None,
                "client_action_id": "parent-model-correction-replace-expired-1",
            },
        )
        assert replacement.status_code == 200
        assert replacement.json()["model"]["payload"]["support_strategy"]["interaction_tone"] == "direct"
