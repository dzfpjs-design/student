from pathlib import Path
import sqlite3

from home_study_agent.controlled_content import ControlledContentError, ControlledContentService
from home_study_agent.database import Database
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService


def seed(database: Database, video_root: Path) -> tuple[str, int]:
    video_root.mkdir(parents=True)
    (video_root / "lesson.mp4").write_bytes(b"synthetic-local-video")
    source_database = video_root.parent.resolve(strict=True) / "synthetic-source.db"
    transcript_root = video_root.parent.resolve(strict=True) / "transcripts"
    transcript_root.mkdir()
    with source_database.open("xb"):
        pass
    with sqlite3.connect(source_database) as source:
        source.execute("CREATE TABLE fixture_metadata(value TEXT NOT NULL)")
        source.execute("INSERT INTO fixture_metadata VALUES (?)", ("public-synthetic",))
        source.commit()
    with database.connect() as connection:
        connection.execute(
            """
            INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            VALUES ('child-media','小明','四年级','江苏南京','active',datetime('now'),datetime('now'))
            """
        )
        connection.execute(
            """
            INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('session-media','child-media','active','study',datetime('now'),datetime('now'),'session-media-action')
            """
        )
        connection.execute(
            """
            INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('plan-media','child-media','session-media',date('now'),'active',30,'test',1,datetime('now'),'plan-media-action')
            """
        )
        connection.execute(
            """
            INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type)
            VALUES ('item-media','plan-media','数学','分数练习',30,1,'active',1,'homework','homework-media','written')
            """
        )
        connection.execute(
            """
            INSERT INTO curriculum_corpus(
              corpus_id,title,corpus_type,source_database_path,transcript_root_path,video_root_path,
              source_ref,license_status,review_status,status,resource_count,transcript_count,
              local_video_count,imported_at,updated_at
            ) VALUES (
              'corpus-media','本地课程','seyk',?,?,?,
              'local-test','public_personal_use','approved_baseline','ready',1,1,1,datetime('now'),datetime('now')
            )
            """,
            (str(source_database), str(transcript_root), str(video_root)),
        )
        cursor = connection.execute(
            """
            INSERT INTO curriculum_resource(
              corpus_id,external_resource_id,title,description,stage_title,grade_title,subject_title,
              semester,unit_path,transcript_text,video_relative_path,video_duration,
              local_video_available,import_job_id
            ) VALUES (
              'corpus-media','101','分数的意义','四年级分数课程','小学','四年级','数学',
              '上册','第五单元/分数','认识分数','lesson.mp4',600,1,'job-media'
            )
            """
        )
        connection.commit()
    return "session-media", int(cursor.lastrowid)


def test_parent_approval_voice_playback_limit_and_task_resume_survive_restart(tmp_path: Path) -> None:
    database = Database(tmp_path / "media.db")
    database.initialize()
    session_id, row_id = seed(database, tmp_path / "videos")
    service = ControlledContentService(database)

    assert service.candidates("child-media", "分数")[0]["approval_status"] == "not_approved"
    service.update_rule("child-media", enabled=True, max_daily_minutes=20, max_item_minutes=8)
    service.approve("child-media", row_id, 6, "只用于学习休息")

    with database.connect() as connection:
        connection.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id='item-media'")
        connection.commit()
    playback = service.start(
        session_id, "分数", "controlled-content-start-1", "item-media"
    )
    assert playback["status"] == "active"
    assert playback["allowed_minutes"] == 6
    assert Path(playback["local_path"]).is_file()
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id='item-media'").fetchone()[0] == "paused"

    try:
        service.start(session_id, "分数", "controlled-content-start-2", "item-media")
        raise AssertionError("second active playback unexpectedly replaced the first one")
    except ControlledContentError as error:
        assert "正在播放" in str(error)

    recovered = ControlledContentService(Database(database.path))
    assert recovered.recover_interrupted() == 1
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id='item-media'").fetchone()[0] == "paused"
        assert connection.execute("SELECT status FROM controlled_content_session").fetchone()[0] == "interrupted"
        assert connection.execute("SELECT actual_seconds FROM controlled_content_session").fetchone()[0] >= 0
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action='controlled_content_interrupted'").fetchone()[0] == 1

    privacy = PrivacyLifecycleService(database, tmp_path / "private-data", b"0" * 32)
    exported = privacy.export_child("child-media")
    import json
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert len(payload["controlled_content_rules"]) == 1
    assert len(payload["controlled_content_approvals"]) == 1
    assert len(payload["controlled_content_sessions"]) == 1
    preview = privacy.deletion_preview("child-media")
    assert preview["controlled_content_sessions"] == 1
    assert preview["controlled_content_configuration"] == 2
    media_deleted = privacy.delete_child_scope(
        "child-media", "media", "确认删除", "delete-controlled-content-media"
    )
    assert media_deleted["status"] == "completed"
    assert privacy.deletion_preview("child-media")["controlled_content_sessions"] == 0
    assert privacy.deletion_preview("child-media")["controlled_content_configuration"] == 2
    all_deleted = privacy.delete_child_scope(
        "child-media", "all_history", "确认删除", "delete-controlled-content-all"
    )
    assert all_deleted["status"] == "completed"
    assert privacy.deletion_preview("child-media")["controlled_content_configuration"] == 0


def test_unapproved_or_disabled_content_never_starts(tmp_path: Path) -> None:
    database = Database(tmp_path / "media.db")
    database.initialize()
    session_id, row_id = seed(database, tmp_path / "videos")
    service = ControlledContentService(database)

    try:
        service.start(session_id, "分数", "controlled-content-disabled")
        raise AssertionError("disabled content unexpectedly started")
    except ControlledContentError as error:
        assert "尚未启用" in str(error)

    service.update_rule("child-media", enabled=True, max_daily_minutes=20, max_item_minutes=8)
    try:
        service.start(session_id, "分数", "controlled-content-unapproved")
        raise AssertionError("unapproved content unexpectedly started")
    except ControlledContentError as error:
        assert "许可目录" in str(error)

    service.approve("child-media", row_id, 6)
    with database.connect() as connection:
        connection.execute("UPDATE plan_item SET status='paused' WHERE plan_item_id='item-media'")
        connection.commit()
    playback = service.start(
        session_id, "分数", "controlled-content-approved", "item-media"
    )
    ended = service.end(session_id, "child_requested", 25, content_session_id=playback['content_session_id'])
    assert ended and ended["status"] == "stopped"
    assert ended["actual_seconds"] >= 25
    assert playback["content_session_id"] == ended["content_session_id"]
