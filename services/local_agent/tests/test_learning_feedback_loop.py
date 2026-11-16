from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest

from home_study_agent import learning_state, repository
from home_study_agent.child_model import ChildModelService
from home_study_agent.database import Database
from home_study_agent.learning_feedback import build_learning_feedback
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository, TutorFeedbackConflictError, ReportNotReadyError
from home_study_agent.reports import build_daily_report

CHILD = "child-primary-4"
OTHER = "child-junior-3"
ITEM = "diag-p4-arithmetic-closed-1"
SECOND = "diag-p4-arithmetic-closed-2"
NODE = "node-p4-math-arithmetic"


@pytest.fixture
def flow(tmp_path, monkeypatch):
    db = Database(tmp_path / "feedback.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    learning = LearningStateService(db)
    learning.seed_catalog()
    clock = [datetime.now(timezone.utc) - timedelta(days=8)]
    monkeypatch.setattr(repository, "utc_now", lambda: clock[0].isoformat())
    monkeypatch.setattr(learning_state, "utc_now", lambda: clock[0].isoformat())
    session = repo.start_session(CHILD, str(uuid4()))["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        c.execute("UPDATE family_rule SET latest_end_time=NULL WHERE child_id=?", (CHILD,))
        c.commit()
    return db, repo, learning, clock, session


def feedback(db, child=CHILD):
    with db.connect() as c:
        return build_learning_feedback(c, child)


def test_multiday_feedback_changes_model_and_plan_without_claiming_mastery(flow):
    db, repo, learning, clock, sid = flow
    assert feedback(db)["status"] == "needs_baseline"
    first = learning.submit_diagnostic(sid, ITEM, "12", "first")
    node = feedback(db)["nodes"][0]
    assert node["status"] == "recheck_needed"
    snap = ChildModelService(db).snapshot(CHILD)
    assert snap["payload"]["learning"]["reliable_feedback"]["nodes"][0]["next_action"] == "clarify_then_recheck"
    assert feedback(db, OTHER)["nodes"] == []
    assert learning.submit_diagnostic(sid, ITEM, "12", "first") == first
    assert feedback(db)["nodes"][0]["attempt_count"] == 1
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
        c.commit()
    clock[0] += timedelta(days=2)
    sid2 = repo.start_session(CHILD, "next-day-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='homework_capture',homework_declared_empty=1,recall_declared_empty=1 WHERE session_id=?", (sid2,))
        c.commit()
    workspace = repo.generate_plan(sid2, "plan-from-feedback")
    review_item = next(p for p in workspace["plan_items"] if p["source_type"] == "due_review")
    assert "最近一次有效作答" in review_item["completion_standard"]
    with db.connect() as c:
        decision = c.execute("SELECT metadata_json FROM plan_constraint_decision WHERE obligation_type='learning_feedback'").fetchone()
        assert json.loads(decision[0])["nodes"][0]["evidence_refs"] == [first["evidence_id"]]
        review = c.execute("SELECT * FROM review_task WHERE review_task_id=?", (review_item["source_ref"],)).fetchone()
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid2,))
        c.commit()
    learning.submit_diagnostic(sid2, review["diagnostic_item_id"], "42", "delayed-answer", review["review_task_id"])
    improved = feedback(Database(db.path))
    assert improved["nodes"][0]["status"] == "independent_observed"
    assert improved["nodes"][0]["retention_success_count"] == 0
    assert improved["nodes"][0]["mastery_claim_allowed"] is False
    changed = ChildModelService(Database(db.path)).snapshot(CHILD)
    assert changed["snapshot_id"] != snap["snapshot_id"]
    assert ChildModelService(Database(db.path)).snapshot(CHILD)["snapshot_id"] == changed["snapshot_id"]
    clock[0] += timedelta(days=1)
    learning.submit_diagnostic(sid2, ITEM, "12", "relapse")
    assert feedback(db)["nodes"][0]["status"] == "recheck_needed"
    assert feedback(db, OTHER)["nodes"] == []


def test_hint_dependency_and_repeated_item_are_not_independent_transfer(flow):
    db, _, learning, _, sid = flow
    first = learning.submit_diagnostic(sid, ITEM, "90", "hinted")
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET hint_level=2,independent=0 WHERE evidence_id=?", (first["evidence_id"],))
        c.commit()
    assert feedback(db)["nodes"][0]["status"] == "supported_only"
    for n in range(3):
        learning.submit_diagnostic(sid, ITEM, "90", f"repeat-{n}")
    node = feedback(db)["nodes"][0]
    assert node["distinct_items"] == 1
    assert node["independent_distinct_items"] == 0
    assert node["status"] == "supported_only"
    assert node["retention_success_count"] == 0


def test_catalog_aliases_cannot_inflate_independent_question_diversity(flow, monkeypatch):
    db, _, learning, clock, sid = flow
    original = next(row for row in learning_state.ITEMS if row[0] == ITEM)
    alias_id = "diag-p4-arithmetic-alias"
    # Two catalog entries, one actual question; keep the real scoring and receipt path.
    monkeypatch.setattr(learning_state, "ITEMS", [*learning_state.ITEMS, (alias_id, *original[1:])])
    learning.seed_catalog()
    learning.submit_diagnostic(sid, ITEM, "90", "original-question")
    clock[0] += timedelta(days=2)
    learning.submit_diagnostic(sid, alias_id, "90", "alias-later")
    learning.submit_diagnostic(sid, SECOND, "42", "different-question")
    node = feedback(Database(db.path))["nodes"][0]
    assert node["attempt_count"] == 3
    assert node["distinct_items"] == node["independent_distinct_items"] == 2
    assert node["retention_success_count"] == 0  # No bound delayed-review receipt.
    assert node["mastery_claim_allowed"] is False
    assert feedback(db, OTHER)["nodes"] == []


def test_parent_daily_report_revalidates_learning_sources_and_survives_restart(flow):
    db, repo, learning, _, sid = flow
    learning.submit_diagnostic(sid, ITEM, "90", "daily-evidence")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    repo.complete_with_reflection(sid, "", "", "okay", "", True, "finish-daily")
    source = repo.report_evidence(sid)
    assert len(source["knowledge_evidence"]) == 1
    assert "mastery_probability" not in source["knowledge_evidence"][0]
    summary, coaching = build_daily_report(source)
    saved = repo.save_daily_report(sid, summary, coaching)
    old_fingerprint = saved["evidence"]["reliable_learning_fingerprint"]
    repo.save_parent_report_feedback(saved["report_id"], True, 4, 1, "今天沟通有帮助")
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
    updated = StudyRepository(Database(db.path)).get_daily_report(saved["report_id"])
    assert updated["evidence_detail"]["knowledge_evidence"] == []
    assert updated["evidence"]["knowledge_evidence_count"] == 0
    assert updated["evidence"]["reliable_learning_fingerprint"] != old_fingerprint
    assert not any("1条已核验作答" in text for text in updated["summary"]["facts"])
    assert any("未计入可靠学习判断" in text for text in updated["summary"]["facts"])
    assert updated["feedback"]["notes_summary"] == "今天沟通有帮助"
    assert StudyRepository(Database(db.path)).get_daily_report(saved["report_id"])["summary"] == updated["summary"]
    with db.connect() as c:
        audits = c.execute("SELECT summary_json FROM audit_event WHERE action='daily_report_evidence_revalidated'").fetchall()
    assert len(audits) == 1
    assert json.loads(audits[0][0])["previous_summary"] == summary


def test_report_built_before_source_withdrawal_cannot_save_stale_claim_as_current(flow):
    db, repo, learning, _, sid = flow
    learning.submit_diagnostic(sid, ITEM, "90", "late-report-evidence")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    repo.complete_with_reflection(sid, "", "", "okay", "", True, "finish-late-report")
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    old_fingerprint = summary["reliable_learning_fingerprint"]
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
    saved = repo.save_daily_report(sid, summary, coaching, source="cloud_result")
    assert saved["summary"]["reliable_learning_fingerprint"] != old_fingerprint
    assert saved["evidence"]["knowledge_evidence_count"] == 0
    assert saved["source"] == "local_rules_revalidated_v3"
    assert not any("1条已核验作答" in text for text in saved["summary"]["facts"])


def test_prior_day_withdrawal_invalidates_today_coaching_dependency_without_recursion(flow):
    db, repo, learning, clock, sid = flow

    def finish_report(session_id, key):
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (session_id,))
            c.commit()
        repo.complete_with_reflection(session_id, "", "", "okay", "", True, key)
        summary, coaching = build_daily_report(repo.report_evidence(session_id))
        return repo.save_daily_report(session_id, summary, coaching)

    learning.submit_diagnostic(sid, ITEM, "90", "yesterday-proof")
    finish_report(sid, "close-yesterday-proof")
    clock[0] += timedelta(days=1)
    today_id = repo.start_session(CHILD, "today-coaching")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (today_id,))
        c.commit()
    learning.submit_diagnostic(today_id, SECOND, "42", "today-proof")
    report = finish_report(today_id, "close-today-proof")
    assert report["evidence"]["reliable_learning_dependencies"][0]["session_id"] == sid
    assert any("近期确认记录" in text for text in report["parent_coaching"]["evidence_basis"])
    own_fingerprint = report["evidence"]["reliable_learning_fingerprint"]
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
    updated = StudyRepository(Database(db.path)).get_daily_report(report["report_id"])
    assert updated["evidence"]["reliable_learning_fingerprint"] == own_fingerprint
    assert updated["evidence"]["reliable_learning_dependencies"] == []
    assert not any("近期确认记录" in text for text in updated["parent_coaching"]["evidence_basis"])
    assert len(updated["evidence_detail"]["knowledge_evidence"]) == 1


def test_report_commit_race_preserves_answer_and_retries_without_stale_report(flow, monkeypatch):
    db, repo, learning, _, sid = flow
    learning.submit_diagnostic(sid, ITEM, "90", "commit-race-answer")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        c.commit()
    repo.complete_with_reflection(sid, "", "", "okay", "", True, "commit-race-finish")
    summary, coaching = build_daily_report(repo.report_evidence(sid))
    original = repo.report_evidence

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        with db.connect() as c:
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
            c.commit()
        return result

    monkeypatch.setattr(repo, "report_evidence", race)
    with pytest.raises(ReportNotReadyError):
        repo.save_daily_report(sid, summary, coaching)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM daily_report").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
    monkeypatch.setattr(repo, "report_evidence", original)
    saved = repo.save_daily_report(sid, summary, coaching)
    assert saved["evidence"]["knowledge_evidence_count"] == 0


def test_invalidated_legacy_mastery_cannot_drive_child_model_support(flow):
    db, _, learning, _, sid = flow
    learning.submit_diagnostic(sid, ITEM, "90", "legacy-estimate")
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        c.execute("UPDATE mastery_state SET hint_dependency=1,mastery_probability=.99 WHERE child_id=?", (CHILD,))
        c.commit()
    payload = ChildModelService(db).snapshot(CHILD)["payload"]
    assert payload["learning"]["evidenced_node_count"] == 0
    assert payload["evidence_level"] == "cold_start"
    assert payload["support_strategy"]["explanation_pacing"] == "standard_layered"


def test_recent_retention_keeps_qualified_baseline_outside_statistics_window(flow):
    db, _, learning, clock, sid = flow
    now = datetime.now(timezone.utc)
    clock[0] = now - timedelta(days=91)
    first = learning.submit_diagnostic(sid, ITEM, "90", "old-baseline")
    with db.connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='7d'", (first["evidence_id"],)).fetchone()
    clock[0] = now - timedelta(days=2)
    learning.submit_diagnostic(sid, review["diagnostic_item_id"], "42", "recent-retention", review["review_task_id"])
    node = feedback(db)["nodes"][0]
    assert node["attempt_count"] == 1
    assert node["status"] == "retention_observed"
    assert node["retention_basis_refs"] == [first["evidence_id"]]
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (ITEM,))
        c.commit()
    node = feedback(Database(db.path))["nodes"][0]
    assert node["status"] == "independent_observed"
    assert node["retention_success_count"] == 0


@pytest.mark.parametrize("mutation,reason", [
    ("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", "source_unqualified_or_reference_only"),
    ("UPDATE diagnostic_item SET prompt='tampered' WHERE diagnostic_item_id=?", "source_unqualified_or_reference_only"),
    ("UPDATE learning_evidence SET confidence=.1 WHERE evidence_id=?", "unreliable_score"),
    ("UPDATE learning_evidence SET occurred_at='2099-01-01T00:00:00+00:00' WHERE evidence_id=?", "invalid_or_future_timestamp"),
    ("UPDATE diagnostic_attempt SET child_id='child-junior-3' WHERE evidence_id=?", "ownership_or_conditions_mismatch"),
    ("UPDATE diagnostic_attempt SET submitted_answer='12' WHERE evidence_id=?", "score_receipt_mismatch"),
])
def test_source_corrections_remove_invalid_conclusions(flow, mutation, reason):
    db, _, learning, _, sid = flow
    attempt = learning.submit_diagnostic(sid, ITEM, "90", "source")
    before = ChildModelService(db).snapshot(CHILD)
    with db.connect() as c:
        c.execute(mutation, (ITEM if "diagnostic_item SET" in mutation else attempt["evidence_id"],))
        c.commit()
    after = ChildModelService(Database(db.path)).snapshot(CHILD)
    assert after["snapshot_id"] != before["snapshot_id"]
    assert after["payload"]["learning"]["reliable_feedback"]["nodes"] == []
    assert feedback(db)["excluded"][reason] == 1


def test_legacy_keyword_score_is_not_qualified_learning_evidence(flow):
    db, repo, learning, _, _ = flow
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed'")
        c.commit()
    sid = repo.start_session(OTHER, "other-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    learning.submit_diagnostic(sid, "diag-j3-discriminant-1", "0 一个", "keyword")
    result = feedback(db, OTHER)
    assert result["nodes"] == []
    assert sum(result["excluded"].values()) == 1


def test_feedback_receipt_conflicts_do_not_return_other_child_text(flow):
    db, repo, _, _, sid = flow
    turn = repo.save_tutor_turn(sid, None, "数学", "问题", "提示", "test", "test", "turn-a")
    first = repo.save_tutor_feedback(turn["turn_id"], "孩子A的私有反馈", "still_stuck", "same-action")
    assert repo.save_tutor_feedback(turn["turn_id"], "孩子A的私有反馈", "still_stuck", "same-action") == first
    with pytest.raises(TutorFeedbackConflictError):
        repo.save_tutor_feedback(turn["turn_id"], "不同内容", "understood", "same-action")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
        c.commit()
    other_sid = repo.start_session(OTHER, "other-feedback")["session_id"]
    other = repo.save_tutor_turn(other_sid, None, "数学", "问题B", "提示B", "test", "test", "turn-b")
    with pytest.raises(TutorFeedbackConflictError):
        repo.save_tutor_feedback(other["turn_id"], "不同孩子", "understood", "same-action")
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM tutor_feedback").fetchone()[0] == 1


def test_http_feedback_conflict_never_emits_another_strategy_receipt(flow, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    db, repo, _, _, sid = flow
    a = repo.save_tutor_turn(sid, None, "数学", "问题A", "提示A", "test", "test", "http-a")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
        c.commit()
    sid2 = repo.start_session(OTHER, "http-b-session")["session_id"]
    b = repo.save_tutor_turn(sid2, None, "数学", "问题B", "提示B", "test", "test", "http-b")
    app = create_app(Settings(data_dir=db.path.parent, database_path=db.path,
                              data_encryption_key=b"f" * 32, maintenance_enabled=False))
    calls = []
    monkeypatch.setattr(app.state.strategy_evolution, "record_tutor_feedback", lambda *args: calls.append(args))
    body = {"response_text": "A私有内容", "outcome": "still_stuck", "client_action_id": "shared-key"}
    with TestClient(app) as client:
        assert client.post(f'/v1/tutor-turns/{a["turn_id"]}/feedback', json=body).status_code == 200
        result = client.post(f'/v1/tutor-turns/{b["turn_id"]}/feedback', json=body)
        assert result.status_code == 409
        assert "A私有内容" not in result.text
        assert calls == [(a["turn_id"], "still_stuck")]


def test_http_answer_refreshes_model_and_failure_retries_only_projection(flow, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    db, _, _, _, sid = flow
    app = create_app(Settings(data_dir=db.path.parent, database_path=db.path,
                              data_encryption_key=b"f" * 32, maintenance_enabled=False))
    body = {"diagnostic_item_id": ITEM, "answer": "12", "client_action_id": "http-check"}
    with TestClient(app) as client:
        original = app.state.child_model.snapshot
        def fail(_):
            raise RuntimeError("test_projection_unavailable")
        monkeypatch.setattr(app.state.child_model, "snapshot", fail)
        first = client.post(f"/v1/sessions/{sid}/diagnostics/answer", json=body)
        assert first.status_code == 200, first.text
        assert first.json()["learning_adaptation"]["status"] == "pending_retry"
        monkeypatch.setattr(app.state.child_model, "snapshot", original)
        replay = client.post(f"/v1/sessions/{sid}/diagnostics/answer", json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["learning_adaptation"]["status"] == "refreshed"
        assert replay.json()["attempt_id"] == first.json()["attempt_id"]
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM diagnostic_attempt").fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM audit_event WHERE action='learning_adaptation_pending'").fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM child_model_snapshot WHERE child_id=? AND is_current=1", (CHILD,)).fetchone()[0] == 1


def test_local_feedback_is_available_to_runtime_but_not_cloud_messages(flow):
    from home_study_agent.agent_runtime import CapabilityRegistry, build_planner_messages
    db, _, learning, _, sid = flow
    learning.submit_diagnostic(sid, ITEM, "12", "privacy")
    compact = ChildModelService(db).compact_for_agent(CHILD)
    assert compact["learning_feedback"]["nodes"]
    context = {"session": {"state": "study"}, "longitudinal_child_model": compact}
    messages = build_planner_messages(context, "继续学习", CapabilityRegistry())
    sent = json.loads(messages[1]["content"])["context"]["longitudinal_child_model"]
    assert "learning_feedback" not in sent
    assert sent["learning_priorities"] == []
    assert compact["learning_priorities"][0]["evidence_refs"]


def test_http_reported_hint_changes_support_without_claiming_independence(flow):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    db, _, _, _, sid = flow
    app = create_app(Settings(data_dir=db.path.parent, database_path=db.path,
                              data_encryption_key=b"f" * 32, maintenance_enabled=False))
    with TestClient(app) as client:
        response = client.post(f"/v1/sessions/{sid}/diagnostics/answer", json={
            "diagnostic_item_id": ITEM, "answer": "90", "hint_level": 2,
            "client_action_id": "http-assisted-answer"})
        assert response.status_code == 200, response.text
        node = feedback(db)["nodes"][0]
        assert node["status"] == "supported_only" and node["independent_correct"] == 0
        model = app.state.child_model.snapshot(CHILD)["payload"]
        assert model["support_strategy"]["explanation_pacing"] == "short_step_then_restate"


@pytest.mark.parametrize("answer", ["四个根", "0，一个不同的实数根"])
def test_http_reference_scoring_does_not_fan_out_as_verified_ability(flow, answer):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    db, repo, _, _, previous = flow
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (previous,))
        c.commit()
    sid = repo.start_session(OTHER, "reference-check-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
        c.commit()
    app = create_app(Settings(data_dir=db.path.parent, database_path=db.path,
                              data_encryption_key=b"f" * 32, maintenance_enabled=False))
    with TestClient(app) as client:
        response = client.post(f"/v1/sessions/{sid}/diagnostics/answer", json={
            "diagnostic_item_id": "diag-j3-discriminant-1", "answer": answer,
            "client_action_id": "http-reference-answer"})
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["score_status"] == "needs_review"
        assert saved["mastery_update_allowed"] is False
        assert "核" in saved["feedback"]
        assert saved["mistake"] is None
        assert saved["knowledge_graph"]["status"] == "manual_confirmation_required"
        assert feedback(db, OTHER)["nodes"] == []


def test_parent_correction_drives_real_task_segments_and_revoke_restores_rules(flow):
    db, repo, _, clock, sid = flow
    clock[0] = datetime.now(timezone.utc)
    model = ChildModelService(db)
    correction = model.correct(CHILD, "task_segment_minutes", 10, "每次先做十分钟", None, "segment-correction")
    def plan(current_sid, action):
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='homework_capture',recall_declared_empty=1 WHERE session_id=?", (current_sid,))
            c.execute("UPDATE family_rule SET task_segment_minutes=25 WHERE child_id=?", (CHILD,))
            c.commit()
        repo.add_homework(current_sid, "数学", "今天作业", 40, "written", None, None, None, None, None, action + "homework")
        return repo.generate_plan(current_sid, action)
    current = plan(sid, "corrected-plan")
    school = [p for p in current["plan_items"] if p["source_type"] == "school_homework"]
    assert school and max(p["expected_minutes"] for p in school) == 10
    with db.connect() as c:
        assert c.execute("SELECT source_ref FROM plan_constraint_decision WHERE obligation_type='parent_model_correction'").fetchone()[0] == correction["correction_id"]
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
        c.commit()
    model.revoke(correction["correction_id"], "恢复原分段", "revoke-segment")
    next_sid = repo.start_session(CHILD, "after-revoke")["session_id"]
    changed = plan(next_sid, "restored-plan")
    assert max(p["expected_minutes"] for p in changed["plan_items"] if p["source_type"] == "school_homework") == 25
    # Existing plan is immutable with respect to later policy revisions.
    assert max(p["expected_minutes"] for p in repo.get_workspace(sid)["plan_items"] if p["source_type"] == "school_homework") == 10
