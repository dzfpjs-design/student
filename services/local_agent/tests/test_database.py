from pathlib import Path
import sqlite3

import pytest

from historical_database_fixture import seed_empty_historical_v84

from home_study_agent.database import (
    LATEST_SCHEMA_VERSION,
    Database,
    UnsupportedDatabaseVersionError,
)


def test_paper_revision_upgrade_backs_up_and_restores_failed_v77_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "family-v77.db"
    database = Database(path)
    seed_empty_historical_v84(path)
    with database.connect() as connection:
        connection.execute("DELETE FROM schema_migration WHERE version>=78")
        connection.execute("CREATE TABLE family_upgrade_sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO family_upgrade_sentinel VALUES('preserve me')")
        connection.commit()

    def fail_after_partial_revision_ddl() -> None:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE revision_upgrade_partial(value TEXT)")
            connection.execute("DELETE FROM family_upgrade_sentinel")
            connection.commit()
        raise RuntimeError("synthetic revision upgrade failure")

    monkeypatch.setattr(database, "_initialize_schema", fail_after_partial_revision_ddl)
    with pytest.raises(RuntimeError, match="synthetic revision upgrade failure"):
        database.initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM family_upgrade_sentinel").fetchone()[0] == "preserve me"
        assert connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] == 77
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='revision_upgrade_partial'"
        ).fetchone() is None
    status = database.latest_upgrade_status()
    assert status["source_schema_version"] == 77
    assert status["target_schema_version"] == LATEST_SCHEMA_VERSION
    assert status["status"] == "restored_after_failure"
    assert status["snapshot_available"] is True

    monkeypatch.undo()
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='paper_scoring_decision_revision'"
        ).fetchone() is not None
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_initialize_installs_knowledge_graph_inside_main_upgrade_contract(tmp_path: Path) -> None:
    database = Database(tmp_path / "kg-bootstrap.db")
    database.initialize()
    connection = sqlite3.connect(database.path)
    try:
        assert connection.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] == LATEST_SCHEMA_VERSION
        assert connection.execute("SELECT version FROM kg_schema_migration").fetchone()[0] >= 1
        installed_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "period_report_evidence_reference",
            "period_report_evidence_state",
            "period_report_evidence_action",
        } <= installed_tables
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_initialize_upgrades_existing_transition_table(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE daily_session (
          session_id TEXT PRIMARY KEY,
          child_id TEXT NOT NULL,
          status TEXT NOT NULL,
          state TEXT NOT NULL,
          started_at TEXT NOT NULL,
          ended_at TEXT,
          last_checkpoint_at TEXT NOT NULL,
          client_action_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE session_transition (
          transition_id TEXT PRIMARY KEY,
          session_id TEXT NOT NULL REFERENCES daily_session(session_id),
          from_state TEXT,
          to_state TEXT NOT NULL,
          reason TEXT NOT NULL,
          occurred_at TEXT NOT NULL
        );
        CREATE TABLE model_call (
          call_id TEXT PRIMARY KEY,
          session_id TEXT,
          child_id TEXT,
          purpose TEXT NOT NULL,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          status TEXT NOT NULL,
          latency_ms INTEGER,
          input_chars INTEGER NOT NULL,
          output_chars INTEGER,
          prompt_version TEXT NOT NULL,
          context_manifest_json TEXT NOT NULL,
          prompt_tokens INTEGER,
          completion_tokens INTEGER,
          error_code TEXT,
          created_at TEXT NOT NULL,
          completed_at TEXT
        );
        INSERT INTO model_call(
          call_id, purpose, provider, model, status, input_chars,
          prompt_version, context_manifest_json, created_at
        ) VALUES ('legacy-call-1', 'tutor', 'qwen', 'qwen-plus', 'completed', 20,
                  'legacy_prompt', '{}', datetime('now'));
        """
    )
    connection.close()

    database = Database(path)
    database.initialize()

    upgraded = sqlite3.connect(path)
    columns = {row[1] for row in upgraded.execute("PRAGMA table_info(session_transition)")}
    indexes = {row[1] for row in upgraded.execute("PRAGMA index_list(session_transition)")}
    model_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(model_call)")}
    reflection_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(daily_reflection)")}
    plan_item_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(plan_item)")}
    session_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(daily_session)")}
    family_rule_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(family_rule)")}
    agent_turn_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(agent_turn)")}
    tutor_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(tutor_turn)")}
    plan_revision_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(plan_revision)")}
    backup_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(backup_manifest)")}
    knowledge_node_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(knowledge_node)")}
    knowledge_source_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(knowledge_source)")}
    mastery_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(mastery_state)")}
    recall_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(classroom_recall)")}
    deletion_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(deletion_job)")}
    period_report_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(period_report)")}
    deletion_indexes = {row[1] for row in upgraded.execute("PRAGMA index_list(deletion_job)")}
    tables = {row[0] for row in upgraded.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    request_id = upgraded.execute("SELECT request_id FROM model_call WHERE call_id = 'legacy-call-1'").fetchone()[0]
    version = upgraded.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0]
    upgraded.close()
    assert "client_action_id" in columns
    assert "idx_transition_client_action" in indexes
    assert {"request_id", "risk_level", "provider_preference", "policy_version", "knowledge_version", "verification_status", "safety_status", "fallback_used", "input_price_cny_per_million", "output_price_cny_per_million", "estimated_cost_cny"} <= model_columns
    assert "skipped" in reflection_columns
    assert {"task_type", "completion_checklist_json", "completion_artifact_id"} <= plan_item_columns
    assert "snooze_until" in session_columns
    assert {"recall_declared_empty", "homework_declared_empty"} <= session_columns
    assert {"session_declaration", "session_draft", "device_incident_report", "device_binding", "device_binding_event", "device_qualification_run", "sensor_release_verification", "maintenance_run", "mastery_update_event", "knowledge_edge", "weekly_goal", "strategy_experiment", "strategy_evaluation_event", "strategy_transition_event", "model_pricing", "deferred_task_review", "period_report_refresh_state", "observation_calibration_run", "agent_objective", "agent_plan", "agent_step", "agent_outcome", "agent_commitment"} <= tables
    assert "node_type" in knowledge_node_columns
    assert {"effective_from", "effective_to"} <= knowledge_source_columns
    assert {"last_independent_correct_at", "forgetting_rate"} <= mastery_columns
    assert "reason" in backup_columns
    assert "misclassification_shadow_threshold" in family_rule_columns
    assert {"tool_status", "tool_error_code", "completed_at"} <= agent_turn_columns
    assert {"strategy_experiment_id", "strategy_version", "output_envelope_json"} <= tutor_columns
    assert "previous_ordered_item_ids_json" in plan_revision_columns
    assert request_id == "legacy-call-1"
    assert {"record_kind", "candidate_nodes_json", "textbook_mapping_json", "evidence_sources_json"} <= recall_columns
    assert {"target_type", "target_id", "residual_paths_json", "verification_json", "error_code", "error_message", "client_action_id", "updated_at"} <= deletion_columns
    assert "updated_at" in period_report_columns
    assert "idx_deletion_job_client_action" in deletion_indexes
    assert version == LATEST_SCHEMA_VERSION
    upgrade_status = database.latest_upgrade_status()
    assert upgrade_status is not None
    assert upgrade_status["status"] == "completed"
    assert upgrade_status["source_schema_version"] == 0
    assert upgrade_status["target_schema_version"] == LATEST_SCHEMA_VERSION
    assert upgrade_status["created_at"]
    assert upgrade_status["completed_at"]
    assert upgrade_status["error_type"] is None
    assert upgrade_status["snapshot_available"] is True


def test_initialize_upgrades_v47_deletion_job_before_creating_new_index(tmp_path: Path) -> None:
    data_dir = tmp_path / "family data with spaces"
    data_dir.mkdir()
    path = data_dir / "v47-deletion.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migration(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migration(version, applied_at) VALUES (47, datetime('now'));
        CREATE TABLE deletion_job (
          deletion_id TEXT PRIMARY KEY,
          child_id TEXT NOT NULL,
          scope TEXT NOT NULL,
          status TEXT NOT NULL,
          deleted_counts_json TEXT NOT NULL,
          created_at TEXT NOT NULL,
          completed_at TEXT
        );
        INSERT INTO deletion_job(
          deletion_id, child_id, scope, status, deleted_counts_json, created_at
        ) VALUES ('legacy-delete-001', 'child-primary-4', 'learning', 'completed', '{}', datetime('now'));
        """
    )
    connection.close()

    database = Database(path)
    database.initialize()

    upgraded = sqlite3.connect(path)
    columns = {row[1] for row in upgraded.execute("PRAGMA table_info(deletion_job)")}
    indexes = {row[1] for row in upgraded.execute("PRAGMA index_list(deletion_job)")}
    safety_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(child_safety_event)")}
    legacy = upgraded.execute(
        "SELECT status, updated_at FROM deletion_job WHERE deletion_id = 'legacy-delete-001'"
    ).fetchone()
    version = upgraded.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0]
    upgraded.close()

    assert {"client_action_id", "target_type", "target_id", "updated_at"} <= columns
    assert "idx_deletion_job_client_action" in indexes
    assert {"risk_level", "category", "status", "guidance_json", "acknowledgement_action_id"} <= safety_columns
    assert legacy[0] == "completed"
    assert legacy[1] is not None
    assert version == LATEST_SCHEMA_VERSION


def test_v50_migration_repairs_homework_completed_by_all_plan_segments(tmp_path: Path) -> None:
    path = tmp_path / "v49-homework-status.db"
    database = Database(path)
    seed_empty_historical_v84(path)
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM schema_migration WHERE version = ?", (LATEST_SCHEMA_VERSION,))
    connection.execute(
        """
        INSERT INTO homework_item(
          homework_id, child_id, session_id, subject, task_type, title,
          expected_minutes, confirmation_status, status, created_at, client_action_id
        ) VALUES ('homework-completed', 'child-junior-3', 'session-old', '数学', 'written',
                  '判别式练习', 20, 'child_confirmed', 'pending', datetime('now'), 'homework-old-1')
        """
    )
    connection.execute(
        """
        INSERT INTO plan_item(
          plan_item_id, plan_id, homework_id, subject, title, expected_minutes,
          actual_minutes, priority, status, position, source_type
        ) VALUES ('segment-completed', 'plan-old', 'homework-completed', '数学', '判别式练习',
                  20, 20, 1, 'completed', 1, 'school_homework')
        """
    )
    connection.execute(
        """
        INSERT INTO homework_item(
          homework_id, child_id, session_id, subject, task_type, title,
          expected_minutes, confirmation_status, status, created_at, client_action_id
        ) VALUES ('homework-partial', 'child-junior-3', 'session-old', '数学', 'written',
                  '分段练习', 40, 'child_confirmed', 'pending', datetime('now'), 'homework-old-2')
        """
    )
    connection.executemany(
        """
        INSERT INTO plan_item(
          plan_item_id, plan_id, homework_id, subject, title, expected_minutes,
          actual_minutes, priority, status, position, source_type
        ) VALUES (?, 'plan-old', 'homework-partial', '数学', ?, 20, ?, ?, ?, ?, 'school_homework')
        """,
        [
            ("segment-partial-completed", "分段练习", 20, 2, "completed", 2),
            ("segment-partial-rescheduled", "分段练习（续2）", None, 3, "rescheduled", 3),
        ],
    )
    connection.commit()
    connection.close()

    Database(path).initialize()

    repaired = sqlite3.connect(path)
    statuses = dict(repaired.execute("SELECT homework_id, status FROM homework_item").fetchall())
    version = repaired.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0]
    repaired.close()
    assert statuses["homework-completed"] == "completed"
    assert statuses["homework-partial"] == "pending"
    assert version == LATEST_SCHEMA_VERSION


def test_initialize_restores_pre_upgrade_snapshot_after_committed_migration_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "upgrade-failure.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migration(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migration(version, applied_at) VALUES (1, datetime('now'));
        CREATE TABLE family_sentinel(value TEXT NOT NULL);
        INSERT INTO family_sentinel(value) VALUES ('必须保留');
        """
    )
    connection.close()
    database = Database(path)

    def fail_after_committed_change() -> None:
        changed = sqlite3.connect(path)
        changed.execute("DELETE FROM family_sentinel")
        changed.execute(
            "INSERT INTO schema_migration(version, applied_at) VALUES (?, datetime('now'))",
            (LATEST_SCHEMA_VERSION,),
        )
        changed.commit()
        changed.close()
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(database, "_initialize_schema", fail_after_committed_change)
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        database.initialize()

    restored = sqlite3.connect(path)
    assert restored.execute("SELECT value FROM family_sentinel").fetchone()[0] == "必须保留"
    assert restored.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] == 1
    restored.close()
    status = database.latest_upgrade_status()
    assert status is not None
    assert status["status"] == "restored_after_failure"
    assert status["source_schema_version"] == 1
    assert status["target_schema_version"] == LATEST_SCHEMA_VERSION
    assert status["error_type"] == "RuntimeError"
    assert status["snapshot_available"] is True


def test_v61_to_v66_kg_bootstrap_has_a_preupgrade_backup_and_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v61-kg-upgrade.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_migration(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migration(version, applied_at) VALUES (61, datetime('now'));
        CREATE TABLE family_sentinel(value TEXT NOT NULL);
        INSERT INTO family_sentinel(value) VALUES ('v61-safe');
    """)
    connection.close(); database = Database(path)
    def interrupted() -> None:
        changed = sqlite3.connect(path)
        changed.execute("DELETE FROM family_sentinel")
        changed.execute("CREATE TABLE kg_partial_upgrade(value TEXT)")
        changed.commit(); changed.close()
        raise RuntimeError("kg v66 migration interrupted")
    monkeypatch.setattr(database, "_initialize_schema", interrupted)
    with pytest.raises(RuntimeError, match="kg v66 migration interrupted"):
        database.initialize()
    restored = sqlite3.connect(path)
    try:
        assert restored.execute("SELECT value FROM family_sentinel").fetchone()[0] == "v61-safe"
        assert restored.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] == 61
        assert restored.execute("SELECT 1 FROM sqlite_master WHERE name='kg_partial_upgrade'").fetchone() is None
    finally:
        restored.close()
    assert database.latest_upgrade_status()["target_schema_version"] == LATEST_SCHEMA_VERSION


def test_initialize_rejects_newer_database_without_modifying_it(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    future_version = LATEST_SCHEMA_VERSION + 1
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE schema_migration(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        INSERT INTO schema_migration(version, applied_at) VALUES ({future_version}, datetime('now'));
        CREATE TABLE family_sentinel(value TEXT NOT NULL);
        INSERT INTO family_sentinel(value) VALUES ('未来版本数据');
        """
    )
    connection.close()
    before = path.read_bytes()

    with pytest.raises(UnsupportedDatabaseVersionError, match="拒绝降级打开"):
        Database(path).initialize()

    assert path.read_bytes() == before
    assert not (tmp_path / "upgrade-backups").exists()


def test_v63_voice_calibration_upgrade_after_behavior_v62_preserves_each_child_and_creates_v64_binding(tmp_path: Path) -> None:
    path = tmp_path / "v63-voice-calibration.db"
    database = Database(path)
    seed_empty_historical_v84(path)
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM voice_calibration_save_action")
    connection.execute("DROP TABLE voice_calibration_save_action")
    connection.execute("ALTER TABLE child_voice_calibration DROP COLUMN microphone_json")
    connection.execute("INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (62, datetime('now'))")
    connection.execute("DELETE FROM schema_migration WHERE version >= 64")
    connection.execute("INSERT INTO child_profile(child_id, nickname, grade, curriculum_region, status, created_at, updated_at) VALUES ('child-primary-4', '小四', '四年级', '江苏', 'active', datetime('now'), datetime('now'))")
    connection.execute("INSERT INTO child_profile(child_id, nickname, grade, curriculum_region, status, created_at, updated_at) VALUES ('child-junior-3', '小九', '初三', '江苏', 'active', datetime('now'), datetime('now'))")
    connection.execute("INSERT INTO child_voice_calibration(child_id, consented_at, profile_json, vocabulary_json, status, updated_at) VALUES (?, datetime('now'), ?, ?, 'ready', datetime('now'))", ("child-primary-4", '{"childId":"child-primary-4","calibrationCount":3}', '["奥数"]'))
    connection.execute("INSERT INTO child_voice_calibration(child_id, consented_at, profile_json, vocabulary_json, status, updated_at) VALUES (?, datetime('now'), ?, ?, 'ready', datetime('now'))", ("child-junior-3", '{"childId":"child-junior-3","calibrationCount":3}', '["化学"]'))
    connection.commit()
    connection.close()

    Database(path).initialize()

    upgraded = sqlite3.connect(path)
    version = upgraded.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0]
    calibration_columns = {row[1] for row in upgraded.execute("PRAGMA table_info(child_voice_calibration)")}
    rows = upgraded.execute("SELECT child_id, vocabulary_json, microphone_json FROM child_voice_calibration ORDER BY child_id").fetchall()
    action_table = upgraded.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='voice_calibration_save_action'").fetchone()
    foreign_key_errors = upgraded.execute("PRAGMA foreign_key_check").fetchall()
    behavior_v62_marker = upgraded.execute("SELECT 1 FROM schema_migration WHERE version = 62").fetchone()
    upgraded.close()
    assert version == LATEST_SCHEMA_VERSION
    assert "microphone_json" in calibration_columns
    assert behavior_v62_marker is not None
    assert rows == [("child-junior-3", '["化学"]', None), ("child-primary-4", '["奥数"]', None)]
    assert action_table is not None
    assert foreign_key_errors == []
    upgrade_status = Database(path).latest_upgrade_status()
    assert upgrade_status is not None and upgrade_status["source_schema_version"] == 63 and upgrade_status["target_schema_version"] == LATEST_SCHEMA_VERSION
    assert upgrade_status["snapshot_available"] is True


def test_v63_voice_calibration_upgrade_failure_restores_two_child_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "v63-voice-calibration-failure.db"
    database = Database(path)
    database.initialize()
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM voice_calibration_save_action")
    connection.execute("DROP TABLE voice_calibration_save_action")
    connection.execute("ALTER TABLE child_voice_calibration DROP COLUMN microphone_json")
    connection.execute("INSERT OR IGNORE INTO schema_migration(version, applied_at) VALUES (62, datetime('now'))")
    connection.execute("DELETE FROM schema_migration WHERE version >= 64")
    connection.execute("INSERT INTO child_profile(child_id, nickname, grade, curriculum_region, status, created_at, updated_at) VALUES ('child-a', '甲', '四年级', '江苏', 'active', datetime('now'), datetime('now'))")
    connection.execute("INSERT INTO child_profile(child_id, nickname, grade, curriculum_region, status, created_at, updated_at) VALUES ('child-b', '乙', '初三', '江苏', 'active', datetime('now'), datetime('now'))")
    connection.execute("INSERT INTO child_voice_calibration(child_id, consented_at, profile_json, vocabulary_json, status, updated_at) VALUES ('child-a', datetime('now'), '{\"childId\":\"child-a\"}', '[]', 'ready', datetime('now'))")
    connection.execute("INSERT INTO child_voice_calibration(child_id, consented_at, profile_json, vocabulary_json, status, updated_at) VALUES ('child-b', datetime('now'), '{\"childId\":\"child-b\"}', '[]', 'ready', datetime('now'))")
    connection.commit(); connection.close()

    def fail_after_v63_change() -> None:
        changed = sqlite3.connect(path)
        changed.execute("DELETE FROM child_voice_calibration WHERE child_id = 'child-a'")
        changed.commit(); changed.close()
        raise RuntimeError("synthetic v63 migration failure")

    monkeypatch.setattr(database, "_initialize_schema", fail_after_v63_change)
    with pytest.raises(RuntimeError, match="synthetic v63 migration failure"):
        database.initialize()
    restored = sqlite3.connect(path)
    assert restored.execute("SELECT MAX(version) FROM schema_migration").fetchone()[0] == 63
    assert restored.execute("SELECT child_id FROM child_voice_calibration ORDER BY child_id").fetchall() == [("child-a",), ("child-b",)]
    assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
    restored.close()
    status = database.latest_upgrade_status()
    assert status is not None and status["status"] == "restored_after_failure" and status["source_schema_version"] == 63 and status["snapshot_available"] is True
