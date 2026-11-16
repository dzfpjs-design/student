"""Evidence-sensitive roadmaps, using synthetic local goals and actual scoring."""

from datetime import date, datetime, timedelta, timezone
import json
import os
import subprocess
import sys

import pytest

from home_study_agent.database import Database
from home_study_agent.family_clock import family_today
from home_study_agent.goal_program import GoalProgramError, GoalProgramService
from home_study_agent.goal_roadmap import GoalRoadmapService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.repository import StudyRepository
from test_goal_program import goal_db, exact_goal_item, _weak_goal_session, _qualify_fresh_goal_items


CHILD = "child-primary-4"
OTHER = "child-junior-3"
TRACK = "competition_math_path"
ROOT = "node-p4-math-arithmetic"
TARGET = "node-oly-arithmetic-structure"


@pytest.fixture
def flow(tmp_path):
    db = Database(tmp_path / "roadmap.db")
    db.initialize()
    StudyRepository(db).seed_family()
    LearningStateService(db).seed_catalog()
    service = GoalProgramService(db)
    service.seed_defaults()
    with db.connect() as c:
        c.execute("UPDATE knowledge_node SET status='inactive' WHERE track='competition_math' AND knowledge_node_id!=?", (TARGET,))
        c.execute("DELETE FROM knowledge_edge WHERE target_node_id=?", (TARGET,))
        c.execute("""INSERT INTO knowledge_edge VALUES('roadmap-prerequisite',?,?,'prerequisite_of',
            'Synthetic prerequisite for tests','synthetic-catalog','v1','active')""", (ROOT, TARGET))
        c.commit()
    configure(service, target_date=(family_today() + timedelta(days=90)).isoformat())
    return db, service, GoalRoadmapService(db)


def configure(service, *, action="configure-roadmap", child=CHILD, track=TRACK, **kwargs):
    payload = dict(target_statement=None, target_date=None, target_score=None, target_score_label=None,
        event_name=None, event_rules_url=None, event_version=None, award_target=None, registration_deadline=None,
        weekly_minutes=60, weekly_sessions=3)
    return service.configure_program(child, track, client_action_id=action, **{**payload, **kwargs})


def rows(db, table):
    with db.connect() as c:
        return [dict(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def change(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def read(flow):
    return flow[2].read(CHILD, TRACK)


def root_step(roadmap):
    return next(r for r in roadmap["prerequisite_path"] if r["knowledge_node_id"] == ROOT)


def score(flow, monkeypatch, *, item="diag-p4-arithmetic-closed-1", answer="90", days_ago=1, review=None):
    db = flow[0]
    with db.connect() as c:
        row = c.execute("SELECT session_id FROM daily_session WHERE child_id=? AND status='active'", (CHILD,)).fetchone()
    sid = row[0] if row else StudyRepository(db).start_session(CHILD, "roadmap-learning")["session_id"]
    change(db, "UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()
    with monkeypatch.context() as patch:
        patch.setattr("home_study_agent.learning_state.utc_now", lambda: at)
        patch.setattr("home_study_agent.repository.utc_now", lambda: at)
        return LearningStateService(db).submit_diagnostic(sid, item, answer,
            f"roadmap-answer-{item}-{days_ago}-{answer}", review)


def test_same_goal_different_real_baseline_changes_path_effort_and_daily_action(flow, monkeypatch):
    before = read(flow)
    assert root_step(before)["baseline_status"] == "needs_baseline"
    assert before["next_daily_action"]["knowledge_node_id"] == ROOT
    score(flow, monkeypatch)
    after = read(flow)
    assert root_step(after)["baseline_status"] == "independent_observed"
    assert after["burden"]["estimated_minutes"]["max"] < before["burden"]["estimated_minutes"]["max"]
    assert after["source_version"] != before["source_version"]
    assert after["next_daily_action"]["action"] != before["next_daily_action"]["action"]
    assert after["goal_attainment"] == "not_established"


def test_real_retention_allows_dependent_path_but_does_not_complete_goal(flow, monkeypatch):
    first = score(flow, monkeypatch, days_ago=5)
    score(flow, monkeypatch, item="diag-p4-arithmetic-closed-2", answer="42", days_ago=4)
    with flow[0].connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'",
                           (first["evidence_id"],)).fetchone()
        rubric = c.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?",
                           (review["diagnostic_item_id"],)).fetchone()[0]
    score(flow, monkeypatch, item=review["diagnostic_item_id"], answer=json.loads(rubric)["exact_answers"][0],
          days_ago=2, review=review["review_task_id"])
    roadmap = read(flow)
    assert ROOT not in [n["knowledge_node_id"] for n in roadmap["prerequisite_path"]]
    assert roadmap["next_daily_action"]["knowledge_node_id"] == TARGET
    assert roadmap["goal_attainment"] == "not_established" and not roadmap["mastery_claim_allowed"]


def test_deadline_and_budget_change_backward_windows_without_increasing_pressure(flow):
    early = read(flow)
    configure(flow[1], action="short-deadline", target_date=(family_today() + timedelta(days=1)).isoformat(), weekly_minutes=15, weekly_sessions=1)
    tight = read(flow)
    assert early["milestones"] != tight["milestones"]
    assert tight["burden"]["feasibility"] == "estimated_over_capacity"
    assert tight["burden"]["weekly_minutes"] == 15 and not tight["burden"]["auto_increase_budget"]
    assert tight["burden"]["unallocated_minutes"] > 0
    assert tight["next_daily_action"] is None or tight["next_daily_action"]["minutes"] <= 15
    assert tight["negotiation_options"] and "increase_budget" not in tight["negotiation_options"]
    configure(flow[1], action="later-deadline", target_date=(family_today() + timedelta(days=180)).isoformat(), weekly_minutes=90, weekly_sessions=3)
    later = read(flow)
    assert later["burden"]["available_minutes"] > tight["burden"]["available_minutes"]
    assert later["burden"]["unallocated_minutes"] == 0
    assert later["milestones"]["monthly"] and later["milestones"]["weekly"]
    assert all(c["planned_minutes"] <= c["budget_minutes"] for c in later["cycles"])


def test_missing_date_stays_clarification_and_never_guesses_school_or_exam_date(flow):
    pet = flow[2].read(CHILD, "pet_path")
    assert pet["status"] == "needs_clarification" and pet["target_date"] is None
    assert pet["clarifications"] and pet["milestones"] == {"monthly": [], "weekly": []}
    assert pet["cycles"] == [] and pet["burden"]["feasibility"] == "unknown"
    assert pet["goal_attainment"] == "not_established"


def test_reads_do_not_reserve_questions_create_attempts_or_claim_goal_completion(flow):
    tables = ("goal_program", "goal_program_revision", "goal_cycle", "diagnostic_attempt", "review_task", "learning_evidence", "mastery_state")
    before = {t: rows(flow[0], t) for t in tables}
    one = read(flow)
    assert read(flow) == one
    constrained = flow[1].roadmap(CHILD, TRACK)
    assert flow[1].roadmap(CHILD, TRACK) == constrained
    for key in ("source_version", "prerequisite_path", "goal_attainment", "target_date"):
        assert constrained[key] == one[key]
    assert constrained["burden"]["estimated_minutes"] == one["burden"]["estimated_minutes"]
    assert constrained["daily_decomposition"]["base_roadmap_source_version"] == one["source_version"]
    assert not constrained["daily_decomposition"]["actual_assignments_created"]
    assert {t: rows(flow[0], t) for t in tables} == before
    assert all(c["status"] == "planned" for c in one["cycles"])
    assert one["local_only"] and not one["cloud_transmission_allowed"]


def test_staleness_and_source_withdrawal_restore_baseline_work(flow, monkeypatch):
    score(flow, monkeypatch, days_ago=45)
    stale = read(flow)
    assert root_step(stale)["baseline_status"] == "stale_recheck"
    assert any(r["code"] == "stale_evidence" for r in stale["risks"])
    change(flow[0], "UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id='diag-p4-arithmetic-closed-1'")
    withdrawn = read(flow)
    assert root_step(withdrawn)["baseline_status"] == "needs_baseline"
    assert withdrawn["source_version"] != stale["source_version"]


def test_expired_and_future_evidence_does_not_improve_baseline(flow, monkeypatch):
    score(flow, monkeypatch, days_ago=100)
    old = read(flow)
    assert root_step(old)["baseline_status"] == "needs_baseline"
    score(flow, monkeypatch, item="diag-p4-arithmetic-closed-2", answer="42", days_ago=-2)
    future = read(flow)
    assert root_step(future)["baseline_status"] == "needs_baseline"
    assert future["burden"]["estimated_minutes"] == old["burden"]["estimated_minutes"]


def test_new_failure_overrides_old_success_without_diagnosing_cause(flow, monkeypatch):
    score(flow, monkeypatch, days_ago=5)
    success = read(flow)
    score(flow, monkeypatch, item="diag-p4-arithmetic-closed-2", answer="41", days_ago=1)
    failed = read(flow)
    assert root_step(failed)["baseline_status"] == "recheck_needed"
    assert failed["burden"]["estimated_minutes"]["max"] > success["burden"]["estimated_minutes"]["max"]
    assert failed["next_daily_action"]["action"] == "clarify_then_recheck"
    assert failed["burden"]["weekly_minutes"] == success["burden"]["weekly_minutes"]


def test_cross_child_evidence_configuration_and_restart_isolation(flow, monkeypatch):
    other_before = flow[2].read(OTHER, "junior_exam_readiness")
    score(flow, monkeypatch)
    assert flow[2].read(OTHER, "junior_exam_readiness") == other_before
    with pytest.raises(KeyError):
        flow[2].read(OTHER, TRACK)
    with pytest.raises(GoalProgramError):
        configure(flow[1], child=OTHER, track="junior_exam_readiness", target_date=(family_today() + timedelta(days=90)).isoformat())
    current = read(flow)
    code = """import json,sys
from home_study_agent.database import Database
from home_study_agent.goal_roadmap import GoalRoadmapService
print(json.dumps(GoalRoadmapService(Database(sys.argv[1])).read(sys.argv[2], sys.argv[3])))
"""
    result = subprocess.run([sys.executable, "-c", code, str(flow[0].path), CHILD, TRACK],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr[-1500:]
    assert json.loads(result.stdout) == current


def test_same_request_replay_cannot_retarget_another_program(flow):
    payload = dict(target_date=(family_today() + timedelta(days=90)).isoformat())
    original = configure(flow[1], **payload)
    assert configure(flow[1], **payload) == original
    before = rows(flow[0], "goal_program_revision")
    with pytest.raises(GoalProgramError):
        configure(flow[1], track="pet_path", **payload)
    with pytest.raises(GoalProgramError):
        configure(flow[1], **{**payload, "weekly_minutes": 90})
    assert rows(flow[0], "goal_program_revision") == before


@pytest.mark.parametrize("field,value", [("weekly_minutes", True), ("weekly_minutes", -1),
    ("weekly_minutes", 10.5), ("weekly_sessions", 0), ("weekly_sessions", 8), ("target_score", float("nan"))])
def test_invalid_budget_or_score_config_is_rejected_without_revision(flow, field, value):
    before = rows(flow[0], "goal_program_revision")
    with pytest.raises(GoalProgramError):
        configure(flow[1], action="invalid-config", **{field: value})
    assert rows(flow[0], "goal_program_revision") == before


def test_cyclic_or_missing_prerequisite_does_not_fake_a_ready_path(flow):
    change(flow[0], "INSERT INTO knowledge_edge VALUES('reverse-roadmap',?,?,'prerequisite_of','Synthetic cycle','test','v1','active')", (TARGET, ROOT))
    cyclic = read(flow)
    assert any(r["code"] == "prerequisite_cycle" for r in cyclic["risks"])
    assert cyclic["next_daily_action"] is None
    change(flow[0], "DELETE FROM knowledge_edge WHERE edge_id='reverse-roadmap'")
    change(flow[0], "UPDATE knowledge_node SET status='inactive' WHERE knowledge_node_id=?", (ROOT,))
    missing = read(flow)
    assert any(r["code"] == "prerequisite_source_unavailable" for r in missing["risks"])
    assert missing["next_daily_action"] is None


def test_cycles_attach_roadmap_and_default_daily_action_does_not_prepare_reviews(flow, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("GET must not prepare a question or reconcile assessments")
    monkeypatch.setattr("home_study_agent.goal_program.SpecializedProgramService.next_agent_action", forbidden)
    cycles = flow[1].cycles(CHILD)
    cycle = next(c for c in cycles if c["track"] == TRACK)
    assert cycle["roadmap"]["program_id"] == cycle["program_id"]
    before = rows(flow[0], "review_task"), rows(flow[0], "diagnostic_attempt")
    flow[1].next_daily_action(OTHER)
    assert before == (rows(flow[0], "review_task"), rows(flow[0], "diagnostic_attempt"))


def test_used_budget_limits_roadmap_action_but_never_counts_as_learning(flow):
    from test_goal_program import _completed_goal_activity
    _completed_goal_activity(flow[0], CHILD, 60)
    change(flow[0], "UPDATE plan_item SET source_type='competition_math'")
    result = read(flow)
    assert result["burden"]["remaining_weekly_minutes"] == 0
    assert result["next_daily_action"] is None
    assert root_step(result)["baseline_status"] == "needs_baseline"
    assert result["goal_attainment"] == "not_established"


def test_only_explicit_prepare_command_may_bind_one_real_review(goal_db, exact_goal_item):
    from concurrent.futures import ThreadPoolExecutor
    _, _, attempt = _weak_goal_session(goal_db, exact_goal_item)
    fresh = _qualify_fresh_goal_items(goal_db)
    change(goal_db, "DELETE FROM review_task")
    service = GoalProgramService(goal_db)
    assert service.next_daily_action(OTHER) is None
    assert rows(goal_db, "review_task") == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: service.prepare_daily_action(OTHER), range(2))) == [None, None]
    reviews = rows(goal_db, "review_task")
    assert len(reviews) == 1 and reviews[0]["source_evidence_id"] == attempt["evidence_id"]
    assert reviews[0]["diagnostic_item_id"] in fresh
    assert GoalProgramService(Database(goal_db.path)).next_daily_action(OTHER) is None
    assert rows(goal_db, "review_task") == reviews


def test_explicit_prepare_crash_rolls_back_binding_and_cycle_snapshot(goal_db, exact_goal_item, monkeypatch):
    _weak_goal_session(goal_db, exact_goal_item)
    _qualify_fresh_goal_items(goal_db)
    service = GoalProgramService(goal_db)
    service.cycles(OTHER)
    before = rows(goal_db, "review_task"), rows(goal_db, "goal_cycle"), rows(goal_db, "audit_event")
    original = service._feedback_execution
    def fail(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("prepare"):
            raise RuntimeError("after explicit review preparation")
        return result
    monkeypatch.setattr(service, "_feedback_execution", fail)
    with pytest.raises(RuntimeError, match="after explicit review"):
        service.prepare_daily_action(OTHER)
    assert before == (rows(goal_db, "review_task"), rows(goal_db, "goal_cycle"), rows(goal_db, "audit_event"))


@pytest.mark.parametrize("state", ["paused", "completed"])
def test_inactive_goal_never_suggests_automatic_action(flow, state):
    change(flow[0], "UPDATE goal_program SET status=? WHERE child_id=? AND track=?", (state, CHILD, TRACK))
    result = read(flow)
    assert result["next_daily_action"] is None
    assert result["goal_attainment"] == "not_established"


def test_past_deadline_and_missing_catalog_are_not_fake_feasible(flow):
    horizon = {"target_date": (family_today() - timedelta(days=1)).isoformat()}
    change(flow[0], "UPDATE goal_program SET target_horizon_json=? WHERE child_id=? AND track=?", (json.dumps(horizon), CHILD, TRACK))
    past = read(flow)
    assert past["burden"]["feasibility"] == "deadline_passed" and past["next_daily_action"] is None
    change(flow[0], "UPDATE knowledge_node SET status='inactive' WHERE track='competition_math'")
    missing = read(flow)
    assert missing["burden"]["feasibility"] == "unknown" and not missing["burden"]["estimate_complete"]


def test_minute_surplus_does_not_make_six_minutes_a_completed_diagnostic(flow):
    from test_goal_program import _completed_goal_activity
    _completed_goal_activity(flow[0], CHILD, 54)
    change(flow[0], "UPDATE plan_item SET source_type='competition_math'")
    result = read(flow)
    assert result["burden"]["remaining_weekly_minutes"] == 6
    assert result["next_daily_action"] is None
    assert any(r["code"] == "remaining_budget_below_check_minimum" for r in result["risks"])


def test_small_budget_never_schedules_transfer_ahead_of_unfunded_prerequisites(flow):
    configure(flow[1], action="limited-prefix", target_date=(family_today() + timedelta(days=14)).isoformat(),
              weekly_minutes=15, weekly_sessions=1)
    result = read(flow)
    work = [step["work_id"] for node in result["prerequisite_path"] for step in node["steps"]]
    scheduled = list(dict.fromkeys(a["work_id"] for week in result["cycles"] for a in week["allocations"]))
    assert scheduled == work[:len(scheduled)]
    assert result["burden"]["unallocated_minutes"] > 0


def test_foreign_owned_catalog_source_cannot_supply_this_childs_prerequisite(flow):
    change(flow[0], """INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
        source_ref,license_status,review_status,content_hash,imported_at)
        VALUES('other-source',?,'test','Other private material','math','test','other-owned','local','approved','test','2026-01-01')""", (OTHER,))
    change(flow[0], "UPDATE knowledge_node SET source_ref='other-owned' WHERE knowledge_node_id=?", (ROOT,))
    result = read(flow)
    assert ROOT not in [node["knowledge_node_id"] for node in result["baseline"]]
    assert result["status"] == "blocked" and result["next_daily_action"] is None


def test_withdrawn_real_attempt_recalculates_roadmap_instead_of_retaining_old_risk(flow, monkeypatch):
    from home_study_agent.mistake_book import MistakeBookService
    attempt = score(flow, monkeypatch, answer="89")
    mistake = MistakeBookService(flow[0]).capture_from_attempt(attempt)
    assert mistake
    before = read(flow)
    assert root_step(before)["baseline_status"] == "recheck_needed"
    change(flow[0], "UPDATE mistake_case SET status='excluded' WHERE mistake_id=?", (mistake["mistake_id"],))
    after = read(flow)
    assert root_step(after)["baseline_status"] == "needs_baseline"
    assert after["source_version"] != before["source_version"]
    assert after["goal_attainment"] == "not_established"


def test_original_configuration_retry_survives_deadline_passing(flow, monkeypatch):
    import home_study_agent.goal_program as module
    target = (family_today() + timedelta(days=90)).isoformat()
    original = configure(flow[1], target_date=target)
    later = datetime.now(timezone.utc) + timedelta(days=365)
    monkeypatch.setattr(module, "_now", lambda: later.isoformat())
    assert configure(flow[1], target_date=target) == original
    with pytest.raises(GoalProgramError):
        configure(flow[1], action="new-expired-date", target_date=target)


def test_concurrent_same_config_request_has_one_revision_and_same_roadmap(flow):
    from concurrent.futures import ThreadPoolExecutor
    def request(_):
        return configure(flow[1], action="concurrent-goal-request", weekly_minutes=75,
                         target_date=(family_today() + timedelta(days=120)).isoformat())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, range(2)))
    assert results[0] == results[1]
    assert len([r for r in rows(flow[0], "goal_program_revision") if r["client_action_id"] == "concurrent-goal-request"]) == 1
    assert read(flow) == read(flow)


def test_aging_failure_does_not_make_goal_look_easier_without_new_evidence(flow, monkeypatch):
    attempt = score(flow, monkeypatch, answer="89")
    fresh = read(flow)
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    change(flow[0], "UPDATE diagnostic_attempt SET created_at=? WHERE attempt_id=?", (old, attempt["attempt_id"]))
    change(flow[0], "UPDATE learning_evidence SET occurred_at=? WHERE evidence_id=?", (old, attempt["evidence_id"]))
    stale = read(flow)
    assert root_step(stale)["baseline_status"] == "stale_recheck"
    assert stale["burden"]["estimated_minutes"]["max"] >= fresh["burden"]["estimated_minutes"]["max"]


@pytest.fixture
def junior_route(flow):
    db, service, _ = flow
    change(db, "UPDATE goal_program SET track='junior_exam_readiness',source_type='junior_exam_goal' WHERE child_id=? AND track=?", (CHILD, TRACK))
    change(db, "UPDATE knowledge_node SET status='inactive' WHERE track='school' AND knowledge_node_id!=?", (ROOT,))
    change(db, "UPDATE knowledge_node SET track='school' WHERE knowledge_node_id=?", (TARGET,))
    return db, service, GoalRoadmapService(db)


def test_prepare_baseline_uses_same_roadmap_without_specialized_fallback(junior_route, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy specialized selection must not replace roadmap")
    monkeypatch.setattr("home_study_agent.goal_program.SpecializedProgramService.next_agent_action", forbidden)
    db, service, _ = junior_route
    expected = service.next_daily_action(CHILD)
    assert expected["knowledge_node_id"] == ROOT
    before = rows(db, "review_task"), rows(db, "diagnostic_attempt")
    actual = service.prepare_daily_action(CHILD)
    assert actual == expected
    assert before == (rows(db, "review_task"), rows(db, "diagnostic_attempt"))


@pytest.mark.parametrize("block", ["unknown_prerequisite", "missing_question", "deadline", "budget"])
def test_prepare_never_binds_weak_descendant_or_bypasses_roadmap(junior_route, monkeypatch, block):
    db, service, _ = junior_route
    score(junior_route, monkeypatch, answer="wrong", days_ago=2)
    change(db, "DELETE FROM review_task")
    if block == "unknown_prerequisite":
        with db.connect() as c:
            node = dict(c.execute("SELECT * FROM knowledge_node WHERE knowledge_node_id=?", (ROOT,)).fetchone())
            node.update(knowledge_node_id="new-prerequisite", code="synthetic-prerequisite", title="Prerequisite baseline")
            c.execute(f"INSERT INTO knowledge_node({','.join(node)}) VALUES ({','.join('?' for _ in node)})", tuple(node.values()))
            c.execute("INSERT INTO knowledge_edge VALUES('new-edge','new-prerequisite',?,'prerequisite_of','Test','synthetic-catalog','v1','active')", (ROOT,))
            c.commit()
    elif block == "missing_question":
        change(db, "UPDATE diagnostic_item SET status='inactive' WHERE knowledge_node_id=?", (ROOT,))
    elif block == "deadline":
        change(db, "UPDATE goal_program SET target_horizon_json=? WHERE child_id=? AND track='junior_exam_readiness'",
               (json.dumps({"target_date": (family_today() - timedelta(days=1)).isoformat()}), CHILD))
    else:
        from test_goal_program import _completed_goal_activity
        change(db, "UPDATE daily_session SET status='completed' WHERE child_id=?", (CHILD,))
        _completed_goal_activity(db, CHILD, 54)
    roadmap = service.roadmap(CHILD, "junior_exam_readiness")
    assert not roadmap["next_daily_action"] or not roadmap["next_daily_action"]["dispatchable"]
    prepared = service.prepare_daily_action(CHILD)
    if block in {"unknown_prerequisite", "missing_question"}:
        assert prepared["dispatchable"] is False
        assert prepared["status"] == "qualified_source_needed"
        assert prepared["knowledge_node_id"] == roadmap["next_daily_action"]["knowledge_node_id"]
        assert prepared["knowledge_node_id"] == ("new-prerequisite" if block == "unknown_prerequisite" else ROOT)
        assert prepared["mastery_claim_allowed"] is False
        assert service.next_daily_action(CHILD) is None
        assert GoalProgramService(Database(db.path)).prepare_daily_action(CHILD) == prepared
    else:
        assert prepared is None
    assert rows(db, "review_task") == []
    assert not [r for r in rows(db, "audit_event") if r["action"] == "goal_review_bound"]


def test_prepare_recomputes_new_prerequisite_after_get_and_preserves_old_review(junior_route, monkeypatch):
    db, service, _ = junior_route
    score(junior_route, monkeypatch, answer="wrong", days_ago=2)
    before = service.roadmap(CHILD, "junior_exam_readiness")
    assert before["next_daily_action"]["knowledge_node_id"] == ROOT
    reviews = rows(db, "review_task")
    prerequisite = "node-p4-math-fractions-decimals"
    with db.connect() as c:
        c.execute("UPDATE knowledge_node SET status='active' WHERE knowledge_node_id=?", (prerequisite,))
        c.execute("DELETE FROM knowledge_edge WHERE target_node_id=?", (prerequisite,))
        c.execute("INSERT INTO knowledge_edge VALUES('revised-edge',?,?,'prerequisite_of','Revised test','synthetic-catalog','v2','active')", (prerequisite, ROOT))
        c.commit()
    action = service.prepare_daily_action(CHILD)
    assert action["knowledge_node_id"] == prerequisite
    assert action["action"] == "establish_baseline"
    assert action["roadmap_source_version"] != before["source_version"]
    assert rows(db, "review_task") == reviews
    restarted = GoalProgramService(Database(db.path))
    assert restarted.prepare_daily_action(CHILD) == action


def retain_root(flow, monkeypatch):
    first = score(flow, monkeypatch, days_ago=5)
    score(flow, monkeypatch, item="diag-p4-arithmetic-closed-2", answer="42", days_ago=4)
    with flow[0].connect() as c:
        review = c.execute("SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='48h'", (first["evidence_id"],)).fetchone()
        rubric = c.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?", (review["diagnostic_item_id"],)).fetchone()[0]
    score(flow, monkeypatch, item=review["diagnostic_item_id"], answer=json.loads(rubric)["exact_answers"][0], days_ago=2, review=review["review_task_id"])


def test_retained_parent_cannot_hide_a_missing_upstream_prerequisite(flow, monkeypatch):
    retain_root(flow, monkeypatch)
    assert read(flow)["next_daily_action"]["knowledge_node_id"] == TARGET
    with flow[0].connect() as c:
        foreign = c.execute("SELECT knowledge_node_id FROM knowledge_node WHERE grade_band LIKE '初三%' LIMIT 1").fetchone()[0]
        c.execute("INSERT INTO knowledge_edge VALUES('unavailable-ancestor',?,?,'prerequisite_of','Test','synthetic-catalog','v1','active')", (foreign, ROOT))
        c.commit()
    result = read(flow)
    assert result["status"] == "blocked"
    assert result["next_daily_action"] is None


def test_retained_target_does_not_starve_a_newly_unlocked_dependent_baseline(junior_route, monkeypatch):
    db, service, _ = junior_route
    retain_root(junior_route, monkeypatch)
    dependent = "node-p4-math-fractions-decimals"
    change(db, "UPDATE knowledge_node SET status='inactive' WHERE knowledge_node_id=?", (TARGET,))
    change(db, "UPDATE knowledge_node SET status='active' WHERE knowledge_node_id=?", (dependent,))
    change(db, "DELETE FROM knowledge_edge WHERE target_node_id=?", (dependent,))
    change(db, "INSERT INTO knowledge_edge VALUES('unlocked-node',?,?,'prerequisite_of','Test','synthetic-catalog','v2','active')", (ROOT, dependent))
    action = service.prepare_daily_action(CHILD)
    assert action["knowledge_node_id"] == dependent
    assert action["action"] == "establish_baseline"
    assert service.next_daily_action(CHILD) == action


def own_catalog_source(flow, node=ROOT):
    change(flow[0], """INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
        source_ref,license_status,review_status,content_hash,imported_at)
        VALUES('roadmap-source',?,'test','Synthetic owned catalog','数学','四年级','owned-roadmap-source','local','approved','source-v1','2026-01-01')""", (CHILD,))
    change(flow[0], "UPDATE knowledge_node SET source_ref='owned-roadmap-source' WHERE knowledge_node_id=?", (node,))


@pytest.mark.parametrize("condition", ["withdrawn", "expired", "not_effective", "invalid_date"])
def test_catalog_source_validity_gates_prerequisites_without_writing(flow, condition):
    own_catalog_source(flow)
    assert read(flow)["next_daily_action"] is not None
    if condition == "withdrawn":
        change(flow[0], "UPDATE knowledge_source SET review_status='needs_review' WHERE source_id='roadmap-source'")
    elif condition == "expired":
        change(flow[0], "UPDATE knowledge_source SET effective_to=? WHERE source_id='roadmap-source'", ((family_today() - timedelta(days=1)).isoformat(),))
    elif condition == "not_effective":
        change(flow[0], "UPDATE knowledge_source SET effective_from=? WHERE source_id='roadmap-source'", ((family_today() + timedelta(days=1)).isoformat(),))
    else:
        change(flow[0], "UPDATE knowledge_source SET effective_to='2026-02-30' WHERE source_id='roadmap-source'")
    before = rows(flow[0], "review_task"), rows(flow[0], "goal_program")
    result = read(flow)
    assert result["next_daily_action"] is None
    assert result["burden"]["feasibility"] == "unknown"
    assert not result["burden"]["estimate_complete"]
    assert before == (rows(flow[0], "review_task"), rows(flow[0], "goal_program"))


def test_withdrawn_target_source_cannot_silently_shrink_goal_to_other_targets(flow):
    own_catalog_source(flow, TARGET)
    with flow[0].connect() as c:
        other = c.execute("SELECT knowledge_node_id FROM knowledge_node WHERE track='competition_math' AND knowledge_node_id!=? LIMIT 1", (TARGET,)).fetchone()[0]
    change(flow[0], "UPDATE knowledge_node SET status='active' WHERE knowledge_node_id=?", (other,))
    before = read(flow)
    change(flow[0], "UPDATE knowledge_source SET review_status='withdrawn' WHERE source_id='roadmap-source'")
    after = read(flow)
    assert after["source_version"] != before["source_version"]
    assert any(r["code"] == "target_source_unavailable" for r in after["risks"])
    assert after["next_daily_action"] is None
    assert after["burden"]["feasibility"] == "unknown" and not after["burden"]["estimate_complete"]


def test_source_revision_changes_route_version_but_other_child_source_does_not(flow):
    own_catalog_source(flow)
    original = read(flow)
    change(flow[0], "UPDATE knowledge_source SET content_hash='source-v2',version_label='v2' WHERE source_id='roadmap-source'")
    revised = read(flow)
    assert revised["source_version"] != original["source_version"]
    assert revised["next_daily_action"]["roadmap_source_version"] == revised["source_version"]
    change(flow[0], """INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
        source_ref,license_status,review_status,content_hash,imported_at)
        VALUES('foreign-source',?,'test','Private foreign material','数学','初三','owned-roadmap-source','local','withdrawn','foreign-hash','2026-01-01')""", (OTHER,))
    assert read(flow) == revised


@pytest.mark.parametrize("condition", ["expired", "withdrawn"])
def test_prepare_revalidates_source_before_mutating_existing_reviews(junior_route, monkeypatch, condition):
    db, service, _ = junior_route
    score(junior_route, monkeypatch, answer="wrong", days_ago=2)
    own_catalog_source(junior_route)
    before_route = service.roadmap(CHILD, "junior_exam_readiness")
    assert before_route["next_daily_action"]["dispatchable"]
    if condition == "expired":
        change(db, "UPDATE knowledge_source SET effective_to=? WHERE source_id='roadmap-source'", ((family_today() - timedelta(days=1)).isoformat(),))
    else:
        change(db, "UPDATE knowledge_source SET review_status='withdrawn' WHERE source_id='roadmap-source'")
    before = rows(db, "review_task"), rows(db, "diagnostic_attempt"), rows(db, "audit_event")
    assert service.prepare_daily_action(CHILD) is None
    assert before == (rows(db, "review_task"), rows(db, "diagnostic_attempt"), rows(db, "audit_event"))


def test_approved_source_alias_cannot_mask_withdrawn_applicable_revision(flow):
    own_catalog_source(flow)
    change(flow[0], """INSERT INTO knowledge_source(source_id,child_id,source_type,title,subject,grade_band,
        source_ref,license_status,review_status,content_hash,imported_at)
        VALUES('conflicting-revision',NULL,'test','Ambiguous applicable revision','数学','四年级','owned-roadmap-source','local','withdrawn','other-revision','2026-01-01')""")
    result = read(flow)
    assert result["next_daily_action"] is None
    assert not result["burden"]["estimate_complete"]
