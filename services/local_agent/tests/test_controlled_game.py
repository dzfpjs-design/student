import json
from pathlib import Path

import pytest

from home_study_agent.controlled_game import ControlledGameError, ControlledGameService
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService


def seed(database: Database, executable: Path) -> tuple[str, str]:
    executable.write_bytes(b"synthetic-windows-executable")
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            VALUES ('child-game','小明','四年级','江苏南京','active',datetime('now'),datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('session-game','child-game','active','study',datetime('now'),datetime('now'),'session-game-action')
            """
        )
        connection.execute(
            """
            INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('plan-game','child-game','session-game',date('now'),'active',30,'test',1,datetime('now'),'plan-game-action')
            """
        )
        connection.execute(
            """
            INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type)
            VALUES ('item-game','plan-game','数学','计算练习',30,1,'paused',1,'homework','homework-game','written')
            """
        )
        connection.commit()
    return "session-game", "item-game"


def test_allowlist_budget_process_binding_and_restart_recovery(tmp_path: Path) -> None:
    database = Database(tmp_path / "game.db")
    database.initialize()
    executable = tmp_path / "MathChallenge.exe"
    session_id, plan_item_id = seed(database, executable)
    service = ControlledGameService(database)

    with pytest.raises(ControlledGameError, match="尚未启用"):
        service.start(session_id, "数学", "game-disabled")

    service.update_rule("child-game", enabled=True, max_daily_minutes=20, max_item_minutes=8)
    approval = service.approve("child-game", "数学挑战", str(executable), 6, "学习完成后使用")
    game = service.start(session_id, "数学", "game-start-action", plan_item_id)
    assert game["status"] == "active"
    assert game["allowed_minutes"] == 6
    assert game["executable_path"] == str(executable.resolve())
    assert service.start(session_id, "数学", "game-start-action", plan_item_id)["game_session_id"] == game["game_session_id"]
    assert service.bind_process(game["game_session_id"], 4242)["os_process_id"] == 4242
    with pytest.raises(ControlledGameError, match="正在运行"):
        service.revoke("child-game", approval["approval_id"])

    recovered = ControlledGameService(Database(database.path))
    assert recovered.recover_interrupted() == 1
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id='item-game'").fetchone()[0] == "paused"
        assert connection.execute("SELECT status FROM controlled_game_session").fetchone()[0] == "interrupted"
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action='controlled_game_interrupted'").fetchone()[0] == 1
    balance = ChildModelService(database).snapshot("child-game")["payload"]["companion_balance"]
    assert balance["game_sessions"] == 1
    from home_study_agent.companion_activity_outcome import CompanionActivityOutcomeService
    outcome = CompanionActivityOutcomeService(database).list('child-game')['items'][0]
    assert outcome['resume']['status'] == 'pending'
    assert not outcome['habit_evidence_eligible']

    private_data = tmp_path / "private-data"
    private_data.mkdir()
    privacy = PrivacyLifecycleService(database, private_data)
    health = privacy.system_health()
    assert health["active_controlled_games"] == []
    assert health["recent_controlled_game_failures"][0]["title"] == "数学挑战"
    assert health["recent_controlled_game_failures"][0]["status"] == "interrupted"
    assert health["recent_controlled_game_failures"][0]["child_nickname"] == "小明"
    exported = privacy.export_child("child-game")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert len(payload["controlled_game_rules"]) == 1
    assert len(payload["controlled_game_approvals"]) == 1
    assert len(payload["controlled_game_sessions"]) == 1
    assert privacy.deletion_preview("child-game")["controlled_game_configuration"] == 2
    privacy.delete_child_scope("child-game", "media", "确认删除", "delete-game-media")
    assert privacy.deletion_preview("child-game")["controlled_game_sessions"] == 0
    privacy.delete_child_scope("child-game", "all_history", "确认删除", "delete-game-all")
    assert privacy.deletion_preview("child-game")["controlled_game_configuration"] == 0


def test_changed_or_unapproved_executable_never_starts(tmp_path: Path) -> None:
    database = Database(tmp_path / "game.db")
    database.initialize()
    executable = tmp_path / "WordGame.exe"
    session_id, _ = seed(database, executable)
    service = ControlledGameService(database)
    service.update_rule("child-game", enabled=True, max_daily_minutes=20, max_item_minutes=8)

    with pytest.raises(ControlledGameError, match="许可目录"):
        service.start(session_id, "单词", "game-unapproved")

    approval = service.approve("child-game", "单词挑战", str(executable), 5)
    executable.write_bytes(b"changed-after-parent-approved")
    with pytest.raises(ControlledGameError, match="重新许可"):
        service.start(session_id, "单词", "game-changed")

    service.approve("child-game", "单词挑战", str(executable), 5)
    game = service.start(session_id, "单词", "game-approved")
    ended = service.end(session_id, "time_limit", 300, game_session_id=game['game_session_id'])
    assert ended and ended["status"] == "expired"
    assert ended["actual_seconds"] == 300
    service.revoke("child-game", approval["approval_id"])
    assert service.approvals("child-game", include_revoked=False) == []


def test_only_real_exe_files_can_be_approved(tmp_path: Path) -> None:
    database = Database(tmp_path / "game.db")
    database.initialize()
    executable = tmp_path / "Allowed.exe"
    seed(database, executable)
    service = ControlledGameService(database)
    script = tmp_path / "not-a-game.cmd"
    script.write_text("exit 0", encoding="utf-8")
    with pytest.raises(ControlledGameError, match="Windows .exe"):
        service.approve("child-game", "脚本", str(script), 5)
