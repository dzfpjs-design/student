from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database


class OfflineGateway:
    configured = False


def test_restart_recovers_mixed_child_work_without_cross_profile_leakage(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path, database_path=tmp_path / "fault-matrix.db",
        data_encryption_key=b"f" * 32, maintenance_enabled=False,
    )
    executable = tmp_path / "ApprovedLearningGame.exe"
    executable.write_bytes(b"synthetic-windows-executable")

    with TestClient(create_app(settings, model_gateway=OfflineGateway())) as client:
        repository = client.app.state.repository
        database = Database(settings.database_path)
        session = repository.start_session("child-primary-4", "fault-matrix-session")
        session_id = session["session_id"]
        with database.connect() as connection:
            connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session_id,))
            connection.execute(
                """INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
                   VALUES('fault-plan','child-primary-4',?,date('now'),'active',20,'test',1,datetime('now'),'fault-plan-action')""",
                (session_id,),
            )
            connection.execute(
                """INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type)
                   VALUES('fault-item','fault-plan','数学','数学练习',20,1,'active',1,'homework','fault-homework','written')"""
            )
            connection.commit()

        game = client.app.state.controlled_game
        game.update_rule("child-primary-4", enabled=True, max_daily_minutes=20, max_item_minutes=8)
        game.approve("child-primary-4", "学习游戏", str(executable), 5)
        started_game = game.start(session_id, "学习", "fault-game-start", "fault-item")
        game.bind_process(started_game["game_session_id"], 4242)

        document = client.app.state.document_monitoring
        document.start(session_id, "fault-document-start")
        document.record_event(
            session_id, "writing", .84,
            {"motion_score": .2, "paper_ratio": .7, "raw_frame_stored": True},
            "fault-document-event",
        )

        supply = client.app.state.specialized_supply
        queued = supply.enqueue_gaps(
            "child-primary-4", "competition_math", "fault-specialized-supply", 4,
        )
        assert queued["created_count"] > 0
        with database.connect() as connection:
            connection.execute(
                "UPDATE specialized_supply_job SET status='processing' WHERE job_id=?",
                (queued["created_job_ids"][0],),
            )
            connection.commit()

    # A second application process must repair interrupted side effects from
    # durable state without a child clicking a recovery control.
    with TestClient(create_app(settings, model_gateway=OfflineGateway())) as restarted:
        document_status = restarted.app.state.document_monitoring.status(session_id)
        primary_model = restarted.app.state.child_model.snapshot("child-primary-4")
        junior_model = restarted.app.state.child_model.snapshot("child-junior-3")
        health = restarted.app.state.privacy_lifecycle.system_health()

    with Database(settings.database_path).connect() as connection:
        game_row = connection.execute(
            "SELECT status,end_reason FROM controlled_game_session WHERE game_session_id=?",
            (started_game["game_session_id"],),
        ).fetchone()
        item_status = connection.execute(
            "SELECT status FROM plan_item WHERE plan_item_id='fault-item'"
        ).fetchone()[0]
        supply_status = connection.execute(
            "SELECT status,error_code FROM specialized_supply_job WHERE job_id=?",
            (queued["created_job_ids"][0],),
        ).fetchone()

    assert game_row["status"] == "interrupted"
    assert game_row["end_reason"] == "app_restarted"
    assert item_status == "active"
    assert supply_status["status"] == "pending"
    assert supply_status["error_code"] == "APP_RESTART"
    assert document_status["status"] == "active"
    assert document_status["latest_event"]["event_type"] == "writing"
    assert document_status["latest_event"]["metrics"]["raw_frame_stored"] is False
    assert health["pending_content_generation_jobs"] == queued["created_count"]
    assert health["recent_controlled_game_failures"][0]["child_nickname"]
    assert "child-junior-3" not in json.dumps(primary_model["payload"], ensure_ascii=False)
    assert "child-primary-4" not in json.dumps(junior_model["payload"], ensure_ascii=False)
