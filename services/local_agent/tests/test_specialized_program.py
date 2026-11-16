from pathlib import Path
import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_program import SpecializedProgramService


def setup_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    StudyRepository(database).seed_family()
    LearningStateService(database).seed_catalog()
    return database


def test_specialized_roadmaps_cover_pet_competition_and_all_junior_subjects(tmp_path: Path) -> None:
    database = setup_database(tmp_path / "specialized.db")
    service = SpecializedProgramService(database)

    primary = service.roadmaps("child-primary-4")
    junior = service.roadmaps("child-junior-3")

    assert {item["track"] for item in primary} == {"pet", "competition_math"}
    pet = next(item for item in primary if item["track"] == "pet")
    assert pet["total_domain_count"] == 17
    assert pet["catalog_domain_count"] == pet["total_domain_count"]
    assert pet["catalog_gap_domains"] == []
    assert pet["phase"] == "baseline"
    assert pet["forecast"]["status"] == "insufficient_evidence"
    competition = next(item for item in primary if item["track"] == "competition_math")
    assert competition["total_domain_count"] == 10
    assert competition["catalog_domain_count"] == competition["total_domain_count"]
    assert competition["catalog_gap_domains"] == []
    assert len(competition["next_actions"]) == 3

    junior_program = junior[0]
    assert junior_program["track"] == "junior_exam"
    assert {item["subject"] for item in junior_program["domains"]} == {
        "语文", "数学", "英语", "物理", "化学", "道德与法治", "历史"
    }
    assert junior_program["catalog_domain_count"] == junior_program["total_domain_count"]
    assert junior_program["catalog_gap_domains"] == []
    assert all(action["domain_id"] not in junior_program["catalog_gap_domains"] for action in junior_program["next_actions"])
    assert all(action["action_type"] == "baseline" for action in junior_program["next_actions"])

    learning = LearningStateService(database)
    for subject in ("语文", "数学", "英语", "物理", "化学", "道德与法治", "历史"):
        item = learning.next_diagnostic("child-junior-3", "school", subject=subject)
        assert item is not None and item["subject"] == subject


def test_open_specialized_writing_uses_reference_rubric_instead_of_always_failing(tmp_path: Path) -> None:
    database = setup_database(tmp_path / "specialized-writing.db")
    with database.connect() as connection:
        item = connection.execute(
            "SELECT * FROM diagnostic_item WHERE diagnostic_item_id='diag-pet-writing-organization-baseline'"
        ).fetchone()

    score, feedback = LearningStateService._score_answer(
        item,
        "Last Friday our class visited a science museum and made a small robot. "
        "I felt excited because it moved, and then we showed it to our teacher.",
    )

    assert score >= 0.75
    assert "任务点覆盖" in feedback
    assert "参考评估" in feedback


def test_assessment_is_idempotent_drives_next_action_and_survives_restart(tmp_path: Path) -> None:
    database = setup_database(tmp_path / "specialized.db")
    service = SpecializedProgramService(database)
    recorded = service.record_assessment(
        "child-primary-4",
        "competition_math",
        "数学",
        "family_baseline",
        {
            "competition_math.number_sense": .85,
            "competition_math.logic": .35,
            "competition_math.modeling": .55,
            "competition_math.transfer": .25,
            "competition_math.counting": .7,
            "competition_math.geometry": .6,
        },
        "specialized-assessment-1",
        score=33,
        max_score=60,
        source_ref="paper-2026-09-01",
    )
    replay = service.record_assessment(
        "child-primary-4",
        "competition_math",
        "数学",
        "family_baseline",
        {"competition_math.logic": 1.0},
        "specialized-assessment-1",
    )
    assert replay["assessment_id"] == recorded["assessment_id"]
    assert replay["domain_results"]["competition_math.logic"] == .35

    roadmap = service.roadmap("child-primary-4", "competition_math")
    assert roadmap["evidence_coverage"] == .6
    assert roadmap["forecast"]["status"] == "reference_only"
    assert roadmap["next_actions"][0]["domain_id"] in {
        "competition_math.arithmetic_structure",
        "competition_math.number_theory",
        "competition_math.pattern",
        "competition_math.expression",
    }

    restarted = SpecializedProgramService(Database(database.path))
    persisted = restarted.roadmap("child-primary-4", "competition_math")
    assert persisted["reference_readiness"] == roadmap["reference_readiness"]
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM specialized_assessment_event").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM specialized_program_snapshot").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='specialized_assessment_recorded'"
        ).fetchone()[0] == 1

    privacy = PrivacyLifecycleService(database, tmp_path / "private-data")
    exported = privacy.export_child("child-primary-4")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert len(payload["specialized_assessments"]) == 1
    assert len(payload["specialized_program_snapshots"]) == 1
    assert privacy.deletion_preview("child-primary-4")["specialized_programs"] == 2
    deleted = privacy.delete_child_scope(
        "child-primary-4", "learning", "确认删除", "delete-specialized-learning"
    )
    assert deleted["status"] == "completed"
    assert privacy.deletion_preview("child-primary-4")["specialized_programs"] == 0


def test_parent_api_records_junior_exam_evidence_and_returns_updated_roadmap(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "app.db",
        data_encryption_key=b"s" * 32,
        maintenance_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        token = client.post("/v1/parent/setup", json={"pin": "2468"}).json()["parent_token"]
        headers = {"X-HSC-Parent": token}
        baseline = client.get(
            "/v1/parent/children/child-junior-3/specialized-roadmaps", headers=headers
        )
        configured = client.put(
            "/v1/parent/children/child-junior-3/goal-programs/junior_exam_readiness",
            headers=headers,
            json={
                "target_statement": "在目标中考节点前稳定修复七科薄弱点。",
                "target_date": (date.today() + timedelta(days=30)).isoformat(),
                "target_score": 90,
                "target_score_label": "数学百分制目标",
                "weekly_minutes": 90,
                "weekly_sessions": 5,
                "client_action_id": "junior-goal-for-stage-test",
            },
        )
        recorded = client.post(
            "/v1/parent/children/child-junior-3/specialized-assessments",
            headers=headers,
            json={
                "track": "junior_exam",
                "subject": "数学",
                "assessment_type": "monthly_exam_analysis",
                "domain_results": {
                    "junior_exam.数学.数与式": .82,
                    "junior_exam.数学.方程与不等式": .48,
                    "junior_exam.数学.函数": .56,
                },
                "score": 76,
                "max_score": 100,
                "source_ref": "september-math-paper",
                "client_action_id": "junior-specialized-api-1",
            },
        )

    assert baseline.status_code == 200
    assert configured.status_code == 200
    assert baseline.json()["items"][0]["total_domain_count"] >= 30
    assert recorded.status_code == 200
    assert recorded.json()["assessment"]["score"] == 76
    roadmap = recorded.json()["roadmap"]
    equation = next(item for item in roadmap["domains"] if item["domain_id"] == "junior_exam.数学.方程与不等式")
    assert equation["reference_score"] == 48
    assert equation["status"] == "weak"
    assert roadmap["stage_plan"]["stage"] == "round_3_simulation"
    assert roadmap["target"]["target_score"] == 90
    assert roadmap["assessment_history"][-1]["percentage"] == 76.0
    assert roadmap["whole_exam_analysis"]["latest_exam"]["score"] == 76


def test_voice_guided_specialized_assessment_advances_persists_and_aggregates(tmp_path: Path) -> None:
    database = setup_database(tmp_path / "guided-specialized.db")
    repository = StudyRepository(database)
    session = repository.start_session("child-primary-4", "start-guided-session")
    now = "2026-09-02T08:00:00+00:00"
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session["session_id"],))
        connection.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
            VALUES('plan-guided','child-primary-4',?,'2026-09-02','active',15,'test',1,?,'plan-guided-action')""", (session["session_id"], now))
        connection.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,started_at,source_type,source_ref,completion_standard,cognitive_load,task_type)
            VALUES('item-guided','plan-guided','数学','数学思维连续测评',15,1,'active',1,?,'competition_math','competition_math.logic','完成整组并保存逐题证据','medium','oral')""", (now,))
        connection.commit()

    service = SpecializedProgramService(database)
    learning = LearningStateService(database)
    run = service.start_for_plan_item("item-guided", "start-guided-assessment")
    assert run and run["total_items"] == 3 and run["completed_items"] == 0

    while run["next_item_id"]:
        with database.connect() as connection:
            expected = json.loads(connection.execute(
                "SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?",
                (run["next_item_id"],),
            ).fetchone()[0])
        answer = " ".join(str(item) for item in expected.get("keywords", [])[:2]) or "我的思路"
        attempt = learning.submit_diagnostic(
            session["session_id"], run["next_item_id"], answer,
            f"guided-answer-{run['completed_items']}",
        )
        run = service.capture_guided_attempt(session["session_id"], attempt)
        assert run is not None

    assert run["status"] == "completed" and run["completed_items"] == 3
    assert run["domain_results"] == {}
    assert run["reference_domain_results"]
    assert run["qualified_items"] == 0
    assert run["learning_status"] == "needs_verification"
    assert run["completion_scope"] == "execution_only"
    restarted = SpecializedProgramService(Database(database.path)).guided_status(run["run_id"])
    assert restarted["status"] == "completed" and restarted["next_item_id"] is None
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM specialized_assessment_event WHERE source_ref=?", (run["run_id"],)).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM specialized_assessment_run_attempt WHERE run_id=?", (run["run_id"],)).fetchone()[0] == 3

    # A source label without an owned promotion receipt cannot enter a later run.
    with database.connect() as connection:
        first_item = connection.execute(
            "SELECT diagnostic_item_id FROM specialized_assessment_run_attempt WHERE run_id=? ORDER BY created_at LIMIT 1",
            (run["run_id"],),
        ).fetchone()[0]
        seed_node = connection.execute(
            "SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?", (first_item,)
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO diagnostic_item(diagnostic_item_id,knowledge_node_id,prompt,answer_type,expected_answer_json,
                 explanation,assessment_mode,difficulty,source_ref,status)
               VALUES('approved-new-specialized',?,'新的迁移题面，请完整说明判断和理由。','text',
                 '{"keywords":["判断","理由"]}','迁移题解析','objective',2,'parent_approved_specialized:test','active')""",
            (seed_node,),
        )
        connection.execute("UPDATE daily_session SET status='completed',state='closed',ended_at=datetime('now') WHERE session_id=?", (session["session_id"],))
        connection.execute("UPDATE daily_plan SET status='completed' WHERE plan_id='plan-guided'")
        connection.execute("UPDATE plan_item SET status='completed' WHERE plan_item_id='item-guided'")
        connection.execute(
            """INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
               VALUES('session-guided-later','child-primary-4','active','study',datetime('now'),datetime('now'),'session-guided-later-action')"""
        )
        connection.execute(
            """INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
               VALUES('plan-guided-later','child-primary-4','session-guided-later',date('now'),'active',15,'test',1,datetime('now'),'plan-guided-later-action')"""
        )
        connection.execute(
            """INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,task_type)
               VALUES('item-guided-later','plan-guided-later','数学','再次测评',15,1,'active',1,'competition_math','competition_math.logic','oral')"""
        )
        connection.commit()
    later = service.start_for_plan_item("item-guided-later", "start-guided-later")
    assert later is not None
    with database.connect() as connection:
        later_queue = json.loads(connection.execute(
            "SELECT item_queue_json FROM specialized_assessment_run WHERE run_id=?", (later["run_id"],)
        ).fetchone()[0])
    assert all(item["diagnostic_item_id"] != "approved-new-specialized" for item in later_queue)

    privacy = PrivacyLifecycleService(database, tmp_path / "private-guided")
    exported = privacy.export_child("child-primary-4")
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert len(payload["specialized_assessment_runs"]) == 2
    assert len(payload["specialized_assessment_run_attempts"]) == 3


def test_rescheduled_specialized_item_cancels_run_and_does_not_hijack_later_answers(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "cancel.db",
        data_encryption_key=b"c" * 32,
        maintenance_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        session = client.post(
            "/v1/sessions",
            json={"child_id": "child-primary-4", "client_action_id": "cancel-run-session"},
        ).json()
        session_id = session["session_id"]
        repository = StudyRepository(Database(settings.database_path))
        with repository.database.connect() as connection:
            connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session_id,))
            connection.execute(
                """INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,generator,version,created_at,client_action_id)
                   VALUES('cancel-plan','child-primary-4',?,'2026-09-02','active',15,'test',1,'2026-09-02T08:00:00+00:00','cancel-plan-action')""",
                (session_id,),
            )
            connection.execute(
                """INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,position,source_type,source_ref,completion_standard,cognitive_load,task_type)
                   VALUES('cancel-item','cancel-plan','数学','竞赛诊断',15,1,'pending',1,'competition_math','competition_math.logic','完成连续诊断','medium','oral')"""
            )
            connection.commit()
        started = client.post(
            "/v1/plan-items/cancel-item/start", json={"client_action_id": "cancel-item-start"}
        )
        # New competition starts require a typed stage binding. Keep that guard,
        # then exercise cancellation of an actual legacy persisted run.
        assert started.status_code == 422
        with repository.database.connect() as connection:
            assert connection.execute("SELECT status FROM plan_item WHERE plan_item_id='cancel-item'").fetchone()[0] == "pending"
            connection.execute("UPDATE plan_item SET status='active' WHERE plan_item_id='cancel-item'")
            connection.commit()
        specialized = SpecializedProgramService(repository.database)
        legacy = specialized.start_for_plan_item("cancel-item", "persisted-legacy-run")
        assert legacy["status"] == "active"
        client.post("/v1/plan-items/cancel-item/pause", json={"client_action_id": "cancel-item-pause"})
        rescheduled = client.post(
            "/v1/plan-items/cancel-item/reschedule",
            json={"reason": "孩子今天状态不适合", "client_action_id": "cancel-item-reschedule"},
        )
        active = client.get(f"/v1/sessions/{session_id}/specialized-assessment")
        late = LearningStateService(repository.database).submit_diagnostic(
            session_id, legacy["next_item_id"], "不会", "late-after-reschedule")
        assert SpecializedProgramService(Database(settings.database_path)).capture_guided_attempt(session_id, late) is None

    assert rescheduled.status_code == 200
    assert active.status_code == 200
    assert active.json()["assessment"] is None
    with Database(settings.database_path).connect() as connection:
        assert connection.execute(
            "SELECT status FROM specialized_assessment_run WHERE plan_item_id='cancel-item'"
        ).fetchone()[0] == "cancelled"
        assert connection.execute("SELECT count(*) FROM specialized_assessment_run_attempt").fetchone()[0] == 0
