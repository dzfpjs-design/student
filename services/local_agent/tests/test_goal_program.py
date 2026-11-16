from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from home_study_agent import learning_feedback
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.family_clock import FAMILY_ZONE, family_today
from home_study_agent.goal_program import GoalProgramService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import StudyRepository
from home_study_agent.specialized_supply import SpecializedDiagnosticSupplyService


def _client(path: Path) -> TestClient:
    return TestClient(create_app(Settings(
        data_dir=path,
        database_path=path / "goals.db",
        data_encryption_key=b"g" * 32,
        maintenance_enabled=False,
    )))


def _headers(client: TestClient, unlock: bool = False) -> dict[str, str]:
    endpoint = "/v1/parent/unlock" if unlock else "/v1/parent/setup"
    token = client.post(endpoint, json={"pin": "1357"}).json()["parent_token"]
    return {"X-HSC-Parent": token}


def _advance_to_plan(client: TestClient, child_id: str) -> dict:
    session = client.post(
        "/v1/sessions",
        json={"child_id": child_id, "client_action_id": f"goal-session-{child_id}"},
    ).json()
    session_id = session["session_id"]
    client.post(f"/v1/sessions/{session_id}/transition", json={
        "expected_state": "check_in", "target_state": "classroom_recall", "reason": "ready",
        "client_action_id": f"goal-transition-recall-{child_id}",
    })
    client.post(f"/v1/sessions/{session_id}/recalls", json={
        "subject": "数学", "summary": "今天复习了二次方程。",
        "client_action_id": f"goal-recall-{child_id}",
    })
    client.post(f"/v1/sessions/{session_id}/transition", json={
        "expected_state": "classroom_recall", "target_state": "homework_capture", "reason": "done",
        "client_action_id": f"goal-transition-homework-{child_id}",
    })
    client.post(f"/v1/sessions/{session_id}/homework", json={
        "subject": "数学", "title": "校内作业", "expected_minutes": 20,
        "client_action_id": f"goal-homework-{child_id}",
    })
    workspace = client.post(f"/v1/sessions/{session_id}/plan/generate", json={
        "client_action_id": f"goal-generate-{child_id}",
    }).json()
    return {"session_id": session_id, "workspace": workspace}


def test_default_programs_cover_both_children_and_express_final_targets(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        headers = _headers(client)
        primary = client.get(
            "/v1/parent/children/child-primary-4/goal-programs", headers=headers
        )
        junior = client.get(
            "/v1/parent/children/child-junior-3/goal-programs", headers=headers
        )

        assert primary.status_code == junior.status_code == 200
        primary_items = primary.json()["items"]
        assert {item["track"] for item in primary_items} == {
            "pet_path", "competition_math_path"
        }
        pet = next(item for item in primary_items if item["track"] == "pet_path")
        assert pet["target_horizon"] == {
            "type": "school_stage", "grade": "六年级", "term": "上学期"
        }
        assert "PET" in pet["target_statement"]

        junior_items = junior.json()["items"]
        assert [item["track"] for item in junior_items] == ["junior_exam_readiness"]
        assert "中考" in junior_items[0]["target_statement"]
        action = junior.json()['next_daily_action']
        assert action and action['knowledge_node_id'] == 'node-j3-math-discriminant'
        assert action['mastery_claim_allowed'] is False


def test_junior_program_enters_daily_plan_and_progress_survives_restart(tmp_path: Path) -> None:
    cycle_id = None
    with _client(tmp_path) as client:
        headers = _headers(client)
        database = Database(tmp_path / 'goals.db')
        exact = _qualified_goal_fixture(database)
        with database.connect() as connection:
            node = connection.execute('SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?', (exact[0],)).fetchone()[0]
            connection.execute("UPDATE knowledge_node SET status='inactive' WHERE grade_band LIKE '初三%' AND knowledge_node_id!=?", (node,))
            connection.execute('DELETE FROM knowledge_edge WHERE target_node_id=?', (node,))
            connection.execute("UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id='child-junior-3'")
            connection.commit()
        cycle_id = client.get('/v1/parent/children/child-junior-3/goal-programs', headers=headers).json()['items'][0]['cycle_id']
        planned = _advance_to_plan(client, "child-junior-3")
        session_id = planned["session_id"]
        goal_item = next(
            item for item in planned["workspace"]["plan_items"]
            if item["source_type"] == "junior_exam_goal"
        )
        assert goal_item["expected_minutes"] == 15
        from home_study_agent.goal_roadmap import GoalRoadmapService
        original = GoalRoadmapService(database).resolve_execution(session_id, goal_item['plan_item_id'])
        assert original['binding']['knowledge_node_id'] == node
        assert original['diagnostic_item_id'] == exact[0]

        client.post(f"/v1/sessions/{session_id}/transition", json={
            "expected_state": "plan_review", "target_state": "study", "reason": "confirmed",
            "client_action_id": "goal-enter-study-junior",
        })
        for pending in planned['workspace']['plan_items']:
            if pending['plan_item_id'] != goal_item['plan_item_id']:
                deferred = client.post(f"/v1/plan-items/{pending['plan_item_id']}/reschedule",json={
                    'reason':'本次先完成目标检查，其他任务另行安排','client_action_id':'defer-'+pending['plan_item_id']})
                assert deferred.status_code == 200,deferred.text
        heard = client.post(f'/v1/sessions/{session_id}/agent-turn',json={
            'transcript':'开始任务','client_action_id':'goal-start-item-junior'}).json()
        started = client.post(f"/v1/agent-turns/{heard['agent_turn_id']}/execute")
        assert started.status_code == 200
        prompt = started.json()['execution']['result']['diagnostic_prompt']['prompt']
        saved = client.post(f'/v1/sessions/{session_id}/diagnostic-prompt/answer',json={
            'prompt_id':prompt['prompt_id'],'item_version':prompt['item_version'],
            'answer':exact[1],'client_action_id':'goal-real-answer'})
        assert saved.status_code == 200,saved.text
        answered = saved.json()['attempt']
        assert answered['correctness'] == 1 and answered['score_status'] == 'verified_local'
        diagnostic = client.get(f'/v1/sessions/{session_id}/diagnostics/next?track=school&subject={goal_item["subject"]}').json()['item']
        for index in range(30):
            if diagnostic is None:
                break
            with database.connect() as connection:
                expected = json.loads(connection.execute('SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?',
                    (diagnostic['diagnostic_item_id'],)).fetchone()[0])
            answers = expected.get('exact_answers') or []
            response = client.post(f'/v1/sessions/{session_id}/diagnostics/answer', json={
                'diagnostic_item_id': diagnostic['diagnostic_item_id'], 'answer': str(answers[0]) if answers else '不知道，需要讲解',
                'client_action_id': f'goal-assessment-answer-{index}'})
            assert response.status_code == 200, response.json()
            diagnostic = response.json().get('next_specialized_item')
        assert diagnostic is None
        completed = client.post(f"/v1/plan-items/{goal_item['plan_item_id']}/complete", json={
            "actual_minutes": 15,
            "completion_evidence_type": "child_checked",
            "completion_note": "模拟独立诊断已完成并记录证据。",
            "completion_checklist": ["materials_reviewed", "uncertainty_marked"],
            "client_action_id": "goal-complete-item-junior",
        })
        assert completed.status_code == 200, completed.json()
        current = client.get(
            "/v1/parent/children/child-junior-3/goal-programs", headers=headers
        ).json()["items"][0]
        assert current["cycle_id"] == cycle_id
        assert current["completed_minutes"] == 15
        assert current["completed_sessions"] == 1

    with _client(tmp_path) as restarted:
        headers = _headers(restarted, unlock=True)
        persisted = restarted.get(
            "/v1/parent/children/child-junior-3/goal-programs", headers=headers
        ).json()["items"][0]
        assert persisted["cycle_id"] == cycle_id
        assert persisted["completed_minutes"] == 15
        assert persisted["evidence_watermark"]["evidence_items"] == 1


def test_parent_configures_goal_deadline_score_and_weekly_pace_durably(tmp_path: Path) -> None:
    action_id = "configure-pet-goal-001"
    payload = {
        "target_statement": "在目标日期前通过 PET，并能在日常场景中稳定使用英语。",
        "target_date": "2027-06-30",
        "target_score": 145,
        "target_score_label": "PET 参考目标分",
        "weekly_minutes": 120,
        "weekly_sessions": 6,
        "client_action_id": action_id,
    }
    with _client(tmp_path) as client:
        headers = _headers(client)
        configured = client.put(
            "/v1/parent/children/child-primary-4/goal-programs/pet_path",
            headers=headers,
            json=payload,
        )
        replayed = client.put(
            "/v1/parent/children/child-primary-4/goal-programs/pet_path",
            headers=headers,
            json=payload,
        )

        assert configured.status_code == replayed.status_code == 200
        assert configured.json()["program"]["origin"] == "family_configured"
        pet = next(item for item in configured.json()["items"] if item["track"] == "pet_path")
        assert pet["target_minutes"] == 120
        assert pet["target_sessions"] == 6
        assert pet["target_horizon"]["target_score"] == 145
        assert pet["deadline"]["target_date"] == "2027-06-30"
        assert pet["deadline"]["status"] == "active"
        assert replayed.json()["program"]["updated_at"] == configured.json()["program"]["updated_at"]
        weekly = client.get(
            "/v1/children/child-primary-4/weekly-goals", headers=headers
        ).json()["items"]
        pet_week = next(item for item in weekly if item["track"] == "pet")
        assert pet_week["target_minutes"] == 120
        assert pet_week["target_sessions"] == 6

        conflict = client.put(
            "/v1/parent/children/child-primary-4/goal-programs/pet_path",
            headers=headers,
            json={**payload, "weekly_minutes": 90},
        )
        assert conflict.status_code == 422

    with _client(tmp_path) as restarted:
        headers = _headers(restarted, unlock=True)
        persisted = restarted.get(
            "/v1/parent/children/child-primary-4/goal-programs", headers=headers
        ).json()["items"]
        pet = next(item for item in persisted if item["track"] == "pet_path")
        assert pet["target_minutes"] == 120
        assert pet["target_horizon"]["target_score_label"] == "PET 参考目标分"


def test_competition_goal_requires_and_persists_a_versioned_real_event(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        headers = _headers(client)
        incomplete = client.put(
            "/v1/parent/children/child-primary-4/goal-programs/competition_math_path",
            headers=headers,
            json={
                "target_statement": "参加适龄数学赛事并检验迁移能力。",
                "target_date": "2027-05-16",
                "event_name": "家庭确认的数学赛事",
                "weekly_minutes": 90,
                "weekly_sessions": 4,
                "client_action_id": "competition-event-incomplete",
            },
        )
        assert incomplete.status_code == 422
        assert "赛事名称、规则链接、规则版本和比赛日期" in incomplete.json()["message"]

        configured = client.put(
            "/v1/parent/children/child-primary-4/goal-programs/competition_math_path",
            headers=headers,
            json={
                "target_statement": "参加适龄数学赛事并以真实赛果检验迁移能力。",
                "target_date": "2027-05-16",
                "event_name": "家庭确认的数学赛事",
                "event_rules_url": "https://example.org/official-rules-2027",
                "event_version": "2027四年级组",
                "award_target": "进入复赛",
                "registration_deadline": "2027-04-01",
                "weekly_minutes": 90,
                "weekly_sessions": 4,
                "client_action_id": "competition-event-complete",
            },
        )
        assert configured.status_code == 200
        cycle = next(
            item for item in configured.json()["items"]
            if item["track"] == "competition_math_path"
        )
        assert cycle["target_horizon"]["event_name"] == "家庭确认的数学赛事"
        assert cycle["target_horizon"]["event_version"] == "2027四年级组"
        assert cycle["target_horizon"]["award_target"] == "进入复赛"
        assert cycle["target_horizon"]["registration_deadline"] == "2027-04-01"

    with _client(tmp_path) as restarted:
        headers = _headers(restarted, unlock=True)
        cycle = next(
            item for item in restarted.get(
                "/v1/parent/children/child-primary-4/goal-programs", headers=headers,
            ).json()["items"]
            if item["track"] == "competition_math_path"
        )
        assert cycle["target_horizon"]["event_rules_url"] == "https://example.org/official-rules-2027"


@pytest.fixture
def goal_db(tmp_path):
    database = Database(tmp_path / "isolated-goals.db")
    database.initialize()
    StudyRepository(database).seed_family()
    LearningStateService(database).seed_catalog()
    return database


def _feedback_node(node_id, track="school", status="recheck_needed"):
    return {
        "knowledge_node_id": node_id, "track": track, "subject": "数学",
        "title": node_id, "status": status, "next_action": "clarify_then_recheck",
        "reason": "以当前有效回执核对，不能由活动时长判断掌握。",
        "evidence_refs": [f"receipt-{node_id}"], "independent_correct": 0,
        "attempt_count": 1, "retention_success_count": 0, "distinct_items": 1,
    }


@pytest.fixture
def exact_goal_item(goal_db):
    return _qualified_goal_fixture(goal_db)


def _qualified_goal_fixture(goal_db):
    from test_school_oral_catalog import without_authored_tasks
    # These cases exercise exact-answer scoring and exhaustion, not oral feedback.
    without_authored_tasks(goal_db, 'node-j3-math-discriminant')
    supply = SpecializedDiagnosticSupplyService(goal_db)
    supply.enqueue_local("diag-j3-discriminant-1", "child-junior-3", "goal-exact-fixture")
    supply.process_local_pending()
    candidate = supply.list("child-junior-3")["items"][0]
    qualified = supply.qualify_local(candidate["candidate_id"], "child-junior-3", "goal-exact-qualify")
    with goal_db.connect() as connection:
        item = dict(connection.execute("SELECT * FROM diagnostic_item WHERE diagnostic_item_id=?",
            (qualified["diagnostic_item_id"],)).fetchone())
    return item["diagnostic_item_id"], json.loads(item["expected_answer_json"])["exact_answers"][0]


def _feedback_stub(monkeypatch, nodes):
    payload = {
        "version": "feedback-contract-v1", "evidence_fingerprint": "first",
        "window_days": 90, "nodes": nodes,
    }
    calls = []

    def build(connection, child_id, now=None):
        assert isinstance(now, datetime)
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        calls.append(child_id)
        return payload

    monkeypatch.setattr(learning_feedback, "build_learning_feedback", build)
    return payload, calls


def _completed_goal_activity(database, child, minutes):
    session = StudyRepository(database).start_session(child, str(uuid4()))["session_id"]
    with database.connect() as connection:
        connection.execute("""INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,
            status,total_minutes,generator,version,created_at,client_action_id)
            VALUES ('quota-plan',?,?,?,'active',?,'test',1,?,'quota-plan')""",
            (child, session, family_today().isoformat(), minutes, datetime.now(timezone.utc).isoformat()))
        connection.execute("""INSERT INTO plan_item(plan_item_id,plan_id,subject,title,
            expected_minutes,actual_minutes,priority,status,position,source_type,completed_at)
            VALUES ('quota-item','quota-plan','数学','Activity only',?,?,1,'completed',1,
            'junior_exam_goal',?)""", (minutes, minutes, datetime.now(timezone.utc).isoformat()))
        connection.commit()


def test_activity_quota_is_not_a_learning_outcome_and_explanation_persists(goal_db):
    service = GoalProgramService(goal_db)
    service.seed_defaults()
    child = "child-junior-3"
    with goal_db.connect() as connection:
        connection.execute("""UPDATE goal_program SET default_weekly_minutes=15,
            default_weekly_sessions=1 WHERE child_id=?""", (child,))
        connection.commit()
    _completed_goal_activity(goal_db, child, 15)
    cycle = service.cycles(child)[0]
    assert cycle["status"] == "completed"
    assert cycle["participation_status"] == "quota_met"
    assert cycle["status_scope"] == "participation_only"
    assert cycle["learning_outcome"]["status"] == "insufficient_evidence"
    assert cycle["learning_outcome"]["next_action"] == "needs_baseline"
    assert cycle["learning_outcome"]["goal_attainment"] == "not_established"
    assert cycle["focus"]["status"] == "needs_baseline"
    assert cycle["focus"]["cloud_transmission_allowed"] is False
    assert cycle["learning_outcome"]["cloud_transmission_allowed"] is False
    assert cycle["evidence_watermark"]["evidence_items"] == 1
    with Database(goal_db.path).connect() as connection:
        stored = connection.execute("SELECT * FROM goal_cycle WHERE cycle_id=?", (cycle["cycle_id"],)).fetchone()
        watermark = json.loads(stored["evidence_watermark_json"])
        assert watermark["participation_status"] == "quota_met"
        assert watermark["learning_outcome"] == cycle["learning_outcome"]
    restarted = GoalProgramService(Database(goal_db.path)).cycles(child)[0]
    assert restarted["learning_outcome"] == cycle["learning_outcome"]


def test_same_week_focus_and_json_refresh_from_current_feedback(goal_db, monkeypatch):
    monkeypatch.setattr('home_study_agent.goal_program._now',lambda:'2026-09-08T10:00:00+00:00')
    payload, calls = _feedback_stub(monkeypatch, [_feedback_node("first")])
    service = GoalProgramService(goal_db)
    monday = date(2026, 9, 7)
    first = service.cycles("child-junior-3", monday)[0]
    payload["nodes"] = [
        _feedback_node("first", status="independent_observed"),
        _feedback_node("second"),
    ]
    payload["evidence_fingerprint"] = "corrected-in-place"
    refreshed = service.cycles("child-junior-3", monday + timedelta(days=1))[0]
    assert refreshed["cycle_id"] == first["cycle_id"]
    assert refreshed["focus"]["knowledge_node_id"] == "second"
    assert refreshed["focus"]["cloud_transmission_allowed"] is False
    assert refreshed["learning_outcome"]["cloud_transmission_allowed"] is False
    assert refreshed["focus"]["evidence_refs"] == ["receipt-second"]
    outcome = refreshed["learning_outcome"]
    assert outcome["evidence_fingerprint"] == "corrected-in-place"
    assert outcome["evidence_version"] == payload["version"]
    assert outcome["window_days"] == 90
    assert outcome["evidence_scope"] == "current_valid_evidence"
    assert outcome["status"] == "recheck_needed"
    assert len(calls) == 2
    with Database(goal_db.path).connect() as connection:
        stored = connection.execute("SELECT * FROM goal_cycle WHERE cycle_id=?", (first["cycle_id"],)).fetchone()
        assert json.loads(stored["focus_json"]) == refreshed["focus"]
        assert json.loads(stored["evidence_watermark_json"])["learning_outcome"] == outcome
    restarted = GoalProgramService(Database(goal_db.path)).cycles("child-junior-3", monday)[0]
    assert restarted["focus"] == refreshed["focus"]
    assert restarted["learning_outcome"] == outcome


def test_tracks_never_borrow_school_evidence_and_refresh_once_per_child(goal_db, monkeypatch):
    payload, calls = _feedback_stub(monkeypatch, [_feedback_node("school-only")])
    service = GoalProgramService(goal_db)
    first = service.cycles("child-primary-4")
    assert len(calls) == 1
    for cycle in first:
        assert cycle["focus"]["status"] == "needs_baseline"
        assert cycle["learning_outcome"]["nodes"] == []
        assert cycle["learning_outcome"]["next_action"] == "needs_baseline"
    payload["nodes"] += [
        _feedback_node("pet-only", "pet", "supported_only"),
        _feedback_node("competition-only", "competition_math", "independent_observed"),
    ]
    second = service.cycles("child-primary-4")
    assert len(calls) == 2
    for cycle in second:
        expected = "pet-only" if cycle["track"] == "pet_path" else "competition-only"
        assert cycle["focus"]["knowledge_node_id"] == expected
        assert [n["knowledge_node_id"] for n in cycle["learning_outcome"]["nodes"]] == [expected]
    school = service.cycles("child-junior-3")[0]
    assert [n["knowledge_node_id"] for n in school["learning_outcome"]["nodes"]] == ["school-only"]


@pytest.mark.parametrize("mutation", [
    "unreceipted", "withdrawn", "wrong_child", "unapproved", "open_rubric", "future",
    "expired", "low_confidence", "rescoring_mismatch",
])
def test_invalidated_real_receipt_clears_focus_and_persisted_outcome(goal_db, exact_goal_item, mutation):
    child = "child-junior-3"
    session = StudyRepository(goal_db).start_session(child, str(uuid4()))["session_id"]
    with goal_db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        connection.commit()
    attempt = LearningStateService(goal_db).submit_diagnostic(session, exact_goal_item[0], "不会", str(uuid4()))
    if mutation == "withdrawn":
        assert MistakeBookService(goal_db).capture_from_attempt(attempt)
    service = GoalProgramService(goal_db)
    first = service.cycles(child)[0]
    assert first["focus"]["knowledge_node_id"] == "node-j3-math-discriminant"
    assert first["learning_outcome"]["status"] == "recheck_needed"
    with goal_db.connect() as connection:
        if mutation == "unreceipted":
            connection.execute("DELETE FROM diagnostic_attempt")
        elif mutation == "withdrawn":
            connection.execute("UPDATE mistake_case SET status='excluded'")
            assert connection.execute("SELECT COUNT(*) FROM mistake_case").fetchone()[0] > 0
        elif mutation == "wrong_child":
            connection.execute("UPDATE diagnostic_attempt SET child_id='child-primary-4'")
        elif mutation == "unapproved":
            connection.execute("UPDATE diagnostic_item SET status='quarantined'")
        elif mutation == "open_rubric":
            connection.execute("UPDATE diagnostic_item SET assessment_mode='pet_reference_rubric'")
        elif mutation in {"future", "expired"}:
            connection.execute("UPDATE learning_evidence SET occurred_at=?",
                ((datetime.now(timezone.utc) + timedelta(days=1 if mutation == "future" else -91)).isoformat(),))
        elif mutation == "rescoring_mismatch":
            connection.execute("UPDATE diagnostic_attempt SET correctness=1")
            connection.execute("UPDATE learning_evidence SET correctness=1")
        else:
            connection.execute("UPDATE learning_evidence SET confidence=0.5")
        connection.commit()
    refreshed = GoalProgramService(Database(goal_db.path)).cycles(child)[0]
    assert refreshed["cycle_id"] == first["cycle_id"]
    assert refreshed["focus"]["status"] == "needs_baseline"
    assert "knowledge_node_id" not in refreshed["focus"]
    assert refreshed["learning_outcome"]["nodes"] == []
    assert refreshed["learning_outcome"]["evidence_fingerprint"] != first["learning_outcome"]["evidence_fingerprint"]
    with goal_db.connect() as connection:
        stored = connection.execute("SELECT * FROM goal_cycle WHERE cycle_id=?", (first["cycle_id"],)).fetchone()
        assert json.loads(stored["focus_json"]) == refreshed["focus"]
        assert json.loads(stored["evidence_watermark_json"])["learning_outcome"] == refreshed["learning_outcome"]


def test_repeated_correct_item_is_practice_not_goal_attainment(goal_db, exact_goal_item):
    child = "child-junior-3"
    session = StudyRepository(goal_db).start_session(child, str(uuid4()))["session_id"]
    with goal_db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        connection.commit()
    attempts = [
        LearningStateService(goal_db).submit_diagnostic(session, exact_goal_item[0], exact_goal_item[1], str(uuid4()))
        for _ in range(2)
    ]
    cycle = GoalProgramService(goal_db).cycles(child)[0]
    outcome = cycle["learning_outcome"]
    assert outcome["status"] == "independent_observed"
    assert outcome["next_action"] == "schedule_delayed_check"
    assert outcome["goal_attainment"] == "not_established"
    assert outcome["mastery_claim_allowed"] is False
    assert outcome["nodes"][0]["attempt_count"] == 1
    assert outcome["nodes"][0]["evidence_refs"] == [attempts[0]["evidence_id"]]
    assert outcome["nodes"][0]["distinct_items"] == 1
    assert outcome["nodes"][0]["independent_correct"] == 1
    assert outcome["nodes"][0]["independent_distinct_items"] == 1
    assert outcome["nodes"][0]["retention_success_count"] == 0
    assert outcome["nodes"][0]["retention_basis_refs"] == []
    assert "不能由同题重复判断迁移" in outcome["reason"]
    with goal_db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM diagnostic_attempt WHERE session_id=?", (session,)).fetchone()[0] == 2
        feedback = learning_feedback.build_learning_feedback(connection, child)
        assert feedback["excluded"]["same_item_within_24h"] == 1


def test_legacy_keyword_score_and_mastery_state_do_not_supply_learning_outcome(goal_db):
    child = "child-junior-3"
    session = StudyRepository(goal_db).start_session(child, str(uuid4()))["session_id"]
    with goal_db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        connection.commit()
    LearningStateService(goal_db).submit_diagnostic(session, "diag-j3-discriminant-1", "不会", str(uuid4()))
    with goal_db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mastery_state WHERE child_id=?", (child,)).fetchone()[0] == 0
        feedback = learning_feedback.build_learning_feedback(connection, child)
        assert sum(feedback["excluded"].values()) == 1
        assert set(feedback["excluded"]) <= {"unreliable_score", "reference_rubric_only"}
    cycle = GoalProgramService(goal_db).cycles(child)[0]
    assert cycle["focus"]["status"] == "needs_baseline"
    assert cycle["learning_outcome"]["status"] == "insufficient_evidence"


@pytest.mark.parametrize("minutes,target_sessions,expected_status,action_minutes", [
    (15, 1, "ready", 15), (39, 1, "ready", None),
    (45, 3, "budget_exhausted", None), (60, 3, "budget_exhausted", None),
    (45, 1, "quota_met", None),
])
def test_daily_action_distinguishes_session_target_from_minute_budget(
    goal_db, exact_goal_item, monkeypatch, minutes, target_sessions, expected_status, action_minutes,
):
    service = GoalProgramService(goal_db)
    service.seed_defaults()
    child = "child-junior-3"
    with goal_db.connect() as connection:
        node = connection.execute('SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?', (exact_goal_item[0],)).fetchone()[0]
        connection.execute("UPDATE knowledge_node SET status='inactive' WHERE grade_band LIKE '初三%' AND knowledge_node_id!=?", (node,))
        connection.execute('DELETE FROM knowledge_edge WHERE target_node_id=?', (node,))
        connection.execute("""UPDATE goal_program SET default_weekly_minutes=45,
            default_weekly_sessions=? WHERE child_id=?""", (target_sessions, child))
        connection.commit()
    _completed_goal_activity(goal_db, child, minutes)

    def specialized(*args):
        assert expected_status == "ready", "No specialized work when the budget prevents scheduling"
        return None

    monkeypatch.setattr("home_study_agent.goal_program.SpecializedProgramService.next_agent_action", specialized)
    cycle = service.cycles(child)[0]
    assert cycle["scheduling_status"] == expected_status
    assert cycle["scheduling_reason"]
    assert cycle["budget_status"] == ("available" if minutes < 45 else "exhausted")
    assert cycle["learning_outcome"]["status"] == "insufficient_evidence"
    action = service.next_daily_action(child)
    if action_minutes is None:
        assert action is None
    else:
        assert action["minutes"] == action_minutes
        assert action["minutes"] <= cycle["remaining_minutes"]
        assert action["scheduling_status"] == "ready"
        assert action["learning_outcome"] == cycle["learning_outcome"]
    if expected_status == "budget_exhausted":
        assert cycle["status"] != "completed"
        assert cycle["participation_status"] != "quota_met"
        assert cycle["remaining_sessions"] == 2
        assert "不自动加时" in cycle["scheduling_reason"]
    with Database(goal_db.path).connect() as connection:
        stored = connection.execute("SELECT evidence_watermark_json FROM goal_cycle WHERE cycle_id=?",
            (cycle["cycle_id"],)).fetchone()[0]
        watermark = json.loads(stored)
        assert watermark["scheduling_status"] == expected_status
        assert watermark["scheduling_reason"] == cycle["scheduling_reason"]


def test_retention_observation_does_not_certify_long_term_goal(goal_db, monkeypatch):
    node = _feedback_node("retained", status="retention_observed")
    node.update({"next_action": "check_transfer", "retention_success_count": 1,
        "reason": "观察到延迟保持，仍需检查其他题目，不能判断全部掌握。"})
    _feedback_stub(monkeypatch, [node])
    outcome = GoalProgramService(goal_db).cycles("child-junior-3")[0]["learning_outcome"]
    assert outcome["status"] == "retention_observed"
    assert outcome["next_action"] == "check_transfer"
    assert outcome["goal_attainment"] == "not_established"
    assert outcome["mastery_claim_allowed"] is False
    assert outcome["nodes"] == [node]


def test_failed_refresh_does_not_persist_half_a_feedback_snapshot(goal_db, monkeypatch):
    payload, _ = _feedback_stub(monkeypatch, [_feedback_node("pet-before", "pet")])
    service = GoalProgramService(goal_db)
    first = service.cycles("child-primary-4")
    payload["nodes"] = [_feedback_node("pet-after", "pet")]
    payload["evidence_fingerprint"] = "after"
    refresh = service._refresh_progress

    def fail_second(connection, cycle, today, outcome):
        if cycle["track"] == "competition_math_path":
            raise RuntimeError("injected refresh failure")
        return refresh(connection, cycle, today, outcome)

    monkeypatch.setattr(service, "_refresh_progress", fail_second)
    with pytest.raises(RuntimeError, match="injected refresh failure"):
        service.cycles("child-primary-4")
    with Database(goal_db.path).connect() as connection:
        for cycle in first:
            stored = connection.execute("SELECT * FROM goal_cycle WHERE cycle_id=?", (cycle["cycle_id"],)).fetchone()
            assert json.loads(stored["focus_json"]) == cycle["focus"]
            assert json.loads(stored["evidence_watermark_json"]) == cycle["evidence_watermark"]


def test_future_receipts_do_not_rewrite_historical_week_but_withdrawals_do(goal_db, exact_goal_item, monkeypatch):
    from home_study_agent import learning_state as learning_module
    from home_study_agent import repository as repository_module

    child = "child-junior-3"
    previous_monday = family_today() - timedelta(days=family_today().weekday() + 7)
    clock = [datetime.combine(previous_monday, datetime.min.time(), tzinfo=timezone.utc).replace(hour=12).isoformat()]
    monkeypatch.setattr(learning_module, "utc_now", lambda: clock[0])
    monkeypatch.setattr(repository_module, "utc_now", lambda: clock[0])
    repo = StudyRepository(goal_db)
    session = repo.start_session(child, str(uuid4()))["session_id"]
    with goal_db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        connection.commit()
    learning = LearningStateService(goal_db)
    old_attempt = learning.submit_diagnostic(session, exact_goal_item[0], "不会", str(uuid4()))
    mistake = MistakeBookService(goal_db).capture_from_attempt(old_attempt)
    service = GoalProgramService(goal_db)
    historical = service.cycles(child, previous_monday)[0]
    assert historical["learning_outcome"]["status"] == "recheck_needed"
    cutoff = historical["learning_outcome"]["as_of"]
    assert cutoff == datetime.combine(previous_monday + timedelta(days=6), datetime.max.time(), tzinfo=FAMILY_ZONE).astimezone(timezone.utc).isoformat()
    assert historical["learning_outcome"]["evidence_scope"] == "as_of_week_end"
    clock[0] = datetime.now(timezone.utc).isoformat()
    with goal_db.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (session,))
        connection.commit()
    session = repo.start_session(child, str(uuid4()))["session_id"]
    with goal_db.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        connection.commit()
    learning.submit_diagnostic(session, exact_goal_item[0], exact_goal_item[1], str(uuid4()))
    assert service.cycles(child)[0]["learning_outcome"]["status"] == "independent_observed"
    refreshed = GoalProgramService(Database(goal_db.path)).cycles(child, previous_monday)[0]
    assert refreshed["focus"] == historical["focus"]
    assert refreshed["learning_outcome"] == historical["learning_outcome"]
    with goal_db.connect() as connection:
        connection.execute("UPDATE mistake_case SET status='excluded' WHERE mistake_id=?", (mistake["mistake_id"],))
        connection.commit()
    revised = service.cycles(child, previous_monday)[0]
    assert revised["focus"]["status"] == "needs_baseline"
    assert revised["learning_outcome"]["as_of"] == cutoff
    assert revised["learning_outcome"]["nodes"] == []
    assert revised["learning_outcome"]["cloud_transmission_allowed"] is False
    with Database(goal_db.path).connect() as connection:
        stored = connection.execute("SELECT * FROM goal_cycle WHERE cycle_id=?", (historical["cycle_id"],)).fetchone()
        assert json.loads(stored["focus_json"]) == revised["focus"]
        assert json.loads(stored["evidence_watermark_json"])["learning_outcome"] == revised["learning_outcome"]


def _weak_goal_session(database, exact_item):
    repo = StudyRepository(database)
    session = repo.start_session("child-junior-3", str(uuid4()))["session_id"]
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        connection.commit()
    attempt = LearningStateService(database).submit_diagnostic(session, exact_item[0], "不会", str(uuid4()))
    return repo, session, attempt


def _qualify_fresh_goal_items(database):
    service = SpecializedDiagnosticSupplyService(database)
    return [service.qualify_local(item["candidate_id"], "child-junior-3", "goal-fresh:" + item["candidate_id"])["diagnostic_item_id"]
            for item in service.list("child-junior-3")["items"] if item["status"] == "needs_review"]


def test_reliable_goal_rebinds_existing_review_and_executes_the_exact_question(goal_db, exact_goal_item, monkeypatch):
    repo, session, original = _weak_goal_session(goal_db, exact_goal_item)
    fresh = _qualify_fresh_goal_items(goal_db)
    assert fresh
    with goal_db.connect() as connection:
        reviews = [dict(row) for row in connection.execute("SELECT * FROM review_task WHERE child_id='child-junior-3'")]
        immediate = next(row for row in reviews if row["interval_label"] == "immediate")
        assert immediate["diagnostic_item_id"] not in fresh
        connection.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1,homework_declared_empty=1 WHERE session_id=?", (session,))
        connection.execute("UPDATE family_rule SET latest_end_time=NULL,max_learning_minutes=90 WHERE child_id='child-junior-3'")
        connection.commit()

    def no_generic(*args):
        raise AssertionError("Reliable recheck must not fall back to a generic specialized domain")

    monkeypatch.setattr("home_study_agent.goal_program.SpecializedProgramService.next_agent_action", no_generic)
    goals = GoalProgramService(goal_db)
    assert goals.prepare_daily_action("child-junior-3") is None
    cycle = goals.cycles("child-junior-3")[0]
    assert cycle["execution_status"] == "review_scheduled"
    assert cycle["execution"]["source_ref"] == immediate["review_task_id"]
    assert cycle["execution"]["diagnostic_item_id"] in fresh
    assert cycle["execution"]["source_evidence_id"] == original["evidence_id"]
    assert cycle["execution"]["cloud_transmission_allowed"] is False
    plan = repo.generate_plan(session, str(uuid4()))
    bound_items = [item for item in plan["plan_items"] if item["source_type"] == "due_review"]
    assert len(bound_items) == 1
    assert not any(item["source_type"] == "junior_exam_goal" for item in plan["plan_items"])
    bound = bound_items[0]
    assert bound["source_ref"] == immediate["review_task_id"]
    with goal_db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_task WHERE child_id='child-junior-3'").fetchone()[0] == len(reviews)
        audits = connection.execute("SELECT summary_json FROM audit_event WHERE action='goal_review_bound'").fetchall()
        assert len(audits) == 1
        audit = json.loads(audits[0][0])
        assert audit["previous_diagnostic_item_id"] == immediate["diagnostic_item_id"]
        assert audit["diagnostic_item_id"] == cycle["execution"]["diagnostic_item_id"]
        assert audit["cloud_transmission_allowed"] is False
        connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        connection.commit()
    repo.start_plan_item(bound["plan_item_id"], str(uuid4()))
    question = repo.plan_review_question(bound["plan_item_id"])
    assert question["diagnostic_item_id"] == cycle["execution"]["diagnostic_item_id"]
    assert question["knowledge_node_id"] == "node-j3-math-discriminant"
    assert goals.next_daily_action("child-junior-3") is None
    assert goals.cycles("child-junior-3")[0]["execution_status"] == "review_in_progress"
    with goal_db.connect() as connection:
        connection.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (question["diagnostic_item_id"],))
        connection.commit()
    assert goals.next_daily_action("child-junior-3") is None
    assert goals.cycles("child-junior-3")[0]["execution_status"] == "supply_gap"
    with goal_db.connect() as connection:
        assert connection.execute("SELECT diagnostic_item_id FROM review_task WHERE review_task_id=?", (question["review_task_id"],)).fetchone()[0] == question["diagnostic_item_id"]
        connection.execute("UPDATE diagnostic_item SET status='active' WHERE diagnostic_item_id=?", (question["diagnostic_item_id"],))
        connection.commit()
        rubric = connection.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?", (question["diagnostic_item_id"],)).fetchone()[0]
    result = LearningStateService(goal_db).submit_diagnostic(session, question["diagnostic_item_id"],
        json.loads(rubric)["exact_answers"][0], str(uuid4()), question["review_task_id"])
    restarted = StudyRepository(Database(goal_db.path))
    receipt = restarted.plan_review_question(bound["plan_item_id"])
    assert receipt["attempt"]["attempt_id"] == result["attempt_id"]
    assert restarted.complete_plan_item(bound["plan_item_id"], 10, str(uuid4()), "child_checked")["status"] == "completed"


def test_reliable_goal_creates_one_real_review_when_no_source_task_exists(goal_db, exact_goal_item):
    from concurrent.futures import ThreadPoolExecutor

    _, _, original = _weak_goal_session(goal_db, exact_goal_item)
    fresh = _qualify_fresh_goal_items(goal_db)
    with goal_db.connect() as connection:
        connection.execute("DELETE FROM review_task")
        connection.commit()
    service = GoalProgramService(goal_db)
    readonly = service.cycles("child-junior-3")[0]
    assert readonly["execution_status"] == "review_ready"
    with goal_db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_task").fetchone()[0] == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: service.prepare_daily_action("child-junior-3"), range(2))) == [None, None]
    with Database(goal_db.path).connect() as connection:
        reviews = connection.execute("SELECT * FROM review_task").fetchall()
        assert len(reviews) == 1
        assert reviews[0]["source_evidence_id"] == original["evidence_id"]
        assert reviews[0]["diagnostic_item_id"] in fresh
        assert reviews[0]["child_id"] == "child-junior-3"
        stored = json.loads(connection.execute("SELECT evidence_watermark_json FROM goal_cycle WHERE cycle_id=?", (readonly["cycle_id"],)).fetchone()[0])
        assert stored["execution"]["source_ref"] == reviews[0]["review_task_id"]


def test_reliable_goal_missing_fresh_qualified_item_is_supply_gap_not_generic_task(goal_db, exact_goal_item, monkeypatch):
    _weak_goal_session(goal_db, exact_goal_item)
    with goal_db.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM review_task ORDER BY review_task_id")]
    monkeypatch.setattr("home_study_agent.goal_program.SpecializedProgramService.next_agent_action",
        lambda *args: pytest.fail("Do not hide a supply gap behind another subject or generic action"))
    assert GoalProgramService(goal_db).next_daily_action("child-junior-3") is None
    cycle = GoalProgramService(Database(goal_db.path)).cycles("child-junior-3")[0]
    assert cycle["execution_status"] == "supply_gap"
    assert cycle["execution"]["diagnostic_item_id"] is None
    assert "暂无同知识点" in cycle["scheduling_reason"]
    with goal_db.connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM review_task ORDER BY review_task_id")] == before


def test_reliable_goal_keeps_future_reviews_and_does_not_add_minutes(goal_db, exact_goal_item):
    _, session, _ = _weak_goal_session(goal_db, exact_goal_item)
    _qualify_fresh_goal_items(goal_db)
    with goal_db.connect() as connection:
        connection.execute("UPDATE review_task SET due_at=?", ((datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),))
        before = [tuple(row) for row in connection.execute("SELECT * FROM review_task ORDER BY review_task_id")]
        connection.commit()
    goals = GoalProgramService(goal_db)
    assert goals.next_daily_action("child-junior-3") is None
    assert goals.cycles("child-junior-3")[0]["execution_status"] == "review_waiting"
    with goal_db.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (session,))
        connection.execute("UPDATE goal_program SET default_weekly_minutes=45 WHERE child_id='child-junior-3'")
        connection.commit()
    _completed_goal_activity(goal_db, "child-junior-3", 40)
    assert goals.next_daily_action("child-junior-3") is None
    assert goals.cycles("child-junior-3")[0]["execution_status"] == "budget_limited"
    with goal_db.connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM review_task ORDER BY review_task_id")] == before


@pytest.mark.parametrize("invalidity", ["wrong_child", "not_promoted", "recent_exposure"])
def test_reliable_goal_never_uses_unqualified_or_recent_alternative(goal_db, exact_goal_item, invalidity):
    _, session, _ = _weak_goal_session(goal_db, exact_goal_item)
    fresh = _qualify_fresh_goal_items(goal_db)
    if invalidity == "recent_exposure":
        for item_id in fresh:
            LearningStateService(goal_db).submit_diagnostic(session, item_id, "不会", str(uuid4()))
    else:
        with goal_db.connect() as connection:
            for item_id in fresh:
                if invalidity == "wrong_child":
                    connection.execute("UPDATE specialized_supply_candidate SET child_id='child-primary-4' WHERE diagnostic_item_id=?", (item_id,))
                else:
                    connection.execute("UPDATE specialized_supply_candidate SET status='needs_review' WHERE diagnostic_item_id=?", (item_id,))
            connection.commit()
    with goal_db.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM review_task ORDER BY review_task_id")]
    service = GoalProgramService(goal_db)
    assert service.next_daily_action("child-junior-3") is None
    assert service.cycles("child-junior-3")[0]["execution_status"] == "supply_gap"
    with goal_db.connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM review_task ORDER BY review_task_id")] == before


def test_reliable_goal_binding_failure_rolls_back_review_and_audit(goal_db, exact_goal_item, monkeypatch):
    _weak_goal_session(goal_db, exact_goal_item)
    _qualify_fresh_goal_items(goal_db)
    service = GoalProgramService(goal_db)
    first = service.cycles("child-junior-3")[0]
    with goal_db.connect() as connection:
        before = [tuple(row) for row in connection.execute("SELECT * FROM review_task ORDER BY review_task_id")]
    bind = service._feedback_execution

    def crash(*args, **kwargs):
        result = bind(*args, **kwargs)
        if kwargs.get("prepare"):
            raise RuntimeError("after review binding")
        return result

    monkeypatch.setattr(service, "_feedback_execution", crash)
    with pytest.raises(RuntimeError, match="after review binding"):
        service.prepare_daily_action("child-junior-3")
    with Database(goal_db.path).connect() as connection:
        assert [tuple(row) for row in connection.execute("SELECT * FROM review_task ORDER BY review_task_id")] == before
        assert connection.execute("SELECT COUNT(*) FROM audit_event WHERE action='goal_review_bound'").fetchone()[0] == 0
        stored = json.loads(connection.execute("SELECT evidence_watermark_json FROM goal_cycle WHERE cycle_id=?", (first["cycle_id"],)).fetchone()[0])
        assert stored == first["evidence_watermark"]
