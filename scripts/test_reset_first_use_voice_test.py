import sqlite3

import pytest

from reset_first_use_voice_test import reset


@pytest.fixture()
def profile(tmp_path):
    root = tmp_path / "profile"
    data = root / "family-data"
    data.mkdir(parents=True)
    connection = sqlite3.connect(data / "home-study.db")
    connection.executescript(
        """
        CREATE TABLE daily_session(session_id TEXT PRIMARY KEY, child_id TEXT, status TEXT, state TEXT,
          started_at TEXT, ended_at TEXT, last_checkpoint_at TEXT, observation_status TEXT);
        CREATE TABLE daily_plan(plan_id TEXT PRIMARY KEY, session_id TEXT);
        CREATE TABLE plan_item(plan_id TEXT, status TEXT, active_segment_started_at TEXT);
        CREATE TABLE document_monitor_session(session_id TEXT, status TEXT, updated_at TEXT);
        CREATE TABLE session_transition(transition_id TEXT, session_id TEXT, from_state TEXT,
          to_state TEXT, reason TEXT, occurred_at TEXT);
        CREATE TABLE audit_event(audit_id TEXT, actor TEXT, action TEXT, child_id TEXT,
          summary_json TEXT, occurred_at TEXT);
        INSERT INTO daily_session VALUES('session-1','child-1','active','study','now',NULL,'now','enabled');
        INSERT INTO daily_plan VALUES('plan-1','session-1');
        INSERT INTO plan_item VALUES('plan-1','active','now');
        INSERT INTO document_monitor_session VALUES('session-1','active','now');
        """
    )
    connection.commit()
    connection.close()
    (root / "companion-wake-words.json").write_text("[]", encoding="utf-8")
    onboarding = root / "voice-onboarding"
    onboarding.mkdir()
    (onboarding / "child-1.json").write_text("{}", encoding="utf-8")
    return root


def test_reset_archives_voice_state_and_preserves_history(profile, tmp_path):
    result = reset(profile, tmp_path / "resets")
    assert result["status"] == "first_use_voice_test_ready"
    assert result["cancelled_active_sessions"] == 1
    assert result["archived_voice_state_files"] == 2
    assert result["learning_history_deleted"] is False
    assert not (profile / "companion-wake-words.json").exists()
    assert not (profile / "voice-onboarding").exists()
    with sqlite3.connect(profile / "family-data" / "home-study.db") as connection:
        assert connection.execute("SELECT status, state, observation_status FROM daily_session").fetchone() == (
            "cancelled", "cancelled", "paused"
        )
        assert connection.execute("SELECT status FROM plan_item").fetchone()[0] == "paused"
        assert connection.execute("SELECT status FROM document_monitor_session").fetchone()[0] == "paused"
        assert connection.execute("SELECT action FROM audit_event").fetchone()[0] == "first_use_voice_test_reset"
    backup = next((tmp_path / "resets").iterdir())
    assert (backup / "home-study-before-first-use-reset.db").is_file()
    assert (backup / "companion-wake-words.json").is_file()
    assert (backup / "voice-onboarding" / "child-1.json").is_file()
