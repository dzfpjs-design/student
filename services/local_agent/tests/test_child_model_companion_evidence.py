"""W24 child-model consumer: lifecycle, self-report, task receipt, child outcome differ."""

from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys

import pytest

from home_study_agent.child_model import ChildModelService
from home_study_agent.companion_activity_voice import CompanionActivityVoiceService
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_companion_activity_outcome import activity, feedback, legacy_activity
from test_companion_activity_voice import voice_turn


def change(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def snapshot(activity, child="child-game"):
    return ChildModelService(activity[0]).snapshot(child)


def balance(activity, child="child-game"):
    return snapshot(activity, child)["payload"]["companion_balance"]


def resume(activity, *, voice=False):
    db, runtime, service, oid, sid, item = activity
    runtime.end_companion_activity(sid, "turn_limit")
    if voice:
        turn = voice_turn(activity, key="resume-voice", operation="request_resume", quote="现在继续刚才的数学。")
        requested = CompanionActivityVoiceService(db).execute(turn)
    else:
        requested = service.request_resume("child-game", oid, note="Continue original task", actor="child",
                                           expected_revision=1, client_action_id="resume-request")
    request = requested["event"]
    key = "companion-outcome-resume:" + request["event_id"]
    StudyRepository(db).resume_plan_item(item, key)
    with db.connect() as c:
        receipt = c.execute("SELECT event_id FROM plan_item_event WHERE client_action_id=?", (key,)).fetchone()[0]
    confirmed = service.confirm_resume("child-game", oid, request_event_id=request["event_id"],
        plan_item_event_id=receipt, expected_revision=request["revision"], client_action_id="resume-confirm")
    return request, receipt, confirmed["event"]


def test_ended_with_paused_reference_is_not_return_evidence(activity):
    activity[1].end_companion_activity(activity[4], "turn_limit")
    result = balance(activity)
    assert result["activity_sessions"] == 1
    assert result["return_to_learning_sessions"] == 0
    assert result["task_resume_receipt_sessions"] == 0
    assert result["verified_child_return_sessions"] == 0
    assert result["boundary_status"] == "return_evidence_missing"
    assert result["usage_boundary_status"] == "within_recorded_limits"


def test_no_activity_is_no_evidence_not_within_bounds(activity):
    result = balance(activity, "other")
    assert result["total_sessions"] == 0
    assert result["boundary_status"] == "no_evidence"
    assert result["return_to_learning_sessions"] == 0


@pytest.mark.parametrize("basis,actor,field", [
    ("child_report", "child", "self_report_sessions"),
    ("parent_observed", "parent", "parent_observed_sessions"),
])
def test_report_is_separate_from_task_receipt_and_verified_return(activity, basis, actor, field):
    _, runtime, service, oid, sid, _ = activity
    runtime.end_companion_activity(sid, "turn_limit")
    feedback(service, oid, evidence_basis=basis, actor=actor)
    result = balance(activity)
    assert result[field] == 1
    assert result["task_resume_receipt_sessions"] == 0
    assert result["verified_child_return_sessions"] == result["return_to_learning_sessions"] == 0
    assert result["habit_success_sessions"] == 0


@pytest.mark.parametrize("later", ["task_completed", "session_completed", "task_paused_again"])
def test_real_historical_receipt_survives_later_task_and_session_state(activity, later):
    resume(activity)
    before = balance(activity)
    assert before["task_resume_receipt_sessions"] == 1
    assert before["verified_child_return_sessions"] == 0
    if later == "session_completed":
        change(activity[0], "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (activity[4],))
    else:
        change(activity[0], "UPDATE plan_item SET status=? WHERE plan_item_id=?",
               ("completed" if later == "task_completed" else "paused", activity[5]))
    result = balance(activity)
    assert result["task_resume_receipt_sessions"] == 1
    assert result["return_to_learning_sessions"] == 0


@pytest.mark.parametrize("withdraw", ["request", "confirmation", "receipt", "source"])
def test_retracted_or_changed_sources_downgrade_real_receipt(activity, withdraw):
    request, receipt, confirmed = resume(activity)
    before = snapshot(activity)
    if withdraw in {"request", "confirmation"}:
        activity[2].retract("child-game", activity[3], target_revision=(request if withdraw == "request" else confirmed)["revision"],
            actor="parent", reason="Correcting the source", expected_revision=confirmed["revision"], client_action_id="retract")
    elif withdraw == "receipt":
        change(activity[0], "UPDATE plan_item_event SET client_action_id='unrelated' WHERE event_id=?", (receipt,))
    else:
        change(activity[0], "UPDATE companion_activity SET topic='Changed source' WHERE session_id=?", (activity[4],))
    after = snapshot(activity)
    assert after["payload"]["companion_balance"]["task_resume_receipt_sessions"] == 0
    assert after["evidence_fingerprint"] != before["evidence_fingerprint"]
    assert after["snapshot_id"] != before["snapshot_id"]


def test_voice_revision_changes_current_projection_even_without_new_row_or_timestamp(activity):
    turn = voice_turn(activity, quote="我完成了。", arguments={"execution": "completed", "experience": "comfortable"})
    CompanionActivityVoiceService(activity[0]).execute(turn)
    before = snapshot(activity)
    assert before["payload"]["companion_balance"]["self_report_sessions"] == 1
    change(activity[0], "UPDATE agent_turn SET transcript='我没有做。' WHERE agent_turn_id=?", (turn,))
    after = snapshot(activity)
    assert after["payload"]["companion_balance"]["self_report_sessions"] == 0
    assert after["evidence_fingerprint"] != before["evidence_fingerprint"]
    assert ChildModelService(activity[0]).compact_for_agent("child-game")["companion_balance"]["self_report_sessions"] == 0


def test_revised_voice_request_invalidates_historical_resume(activity):
    resume(activity, voice=True)
    assert balance(activity)["task_resume_receipt_sessions"] == 1
    change(activity[0], "UPDATE agent_turn SET transcript='还不想继续。' WHERE agent_turn_id='resume-voice'")
    assert balance(activity)["task_resume_receipt_sessions"] == 0


def test_journal_retraction_changes_snapshot_without_rewriting_lifecycle(activity):
    feedback(activity[2], activity[3])
    before = snapshot(activity)
    activity[2].retract("child-game", activity[3], target_revision=2, reason="Not actually done", actor="parent",
                        expected_revision=2, client_action_id="retract-feedback")
    after = snapshot(activity)
    assert after["snapshot_id"] != before["snapshot_id"]
    assert after["payload"]["companion_balance"]["self_report_sessions"] == 0


def test_legacy_terminal_record_and_uncorrelated_resume_never_certify_return(activity):
    activity[1].end_companion_activity(activity[4], "turn_limit")
    legacy = legacy_activity(activity[0], activity[4], activity[5])
    change(activity[0], "UPDATE companion_activity SET status='completed',end_reason='time_limit' WHERE activity_id=?", (legacy,))
    change(activity[0], """INSERT INTO plan_item_event VALUES ('legacy-resumed',?,'resumed',?,'{}','old-auto-resume')""",
           (activity[5], datetime.now(timezone.utc).isoformat()))
    result = balance(activity)
    assert result["legacy_unverified_sessions"] == 1
    assert result["task_resume_receipt_sessions"] == result["return_to_learning_sessions"] == 0


def test_child_isolation_and_readonly_rebuild_survive_process_restart(activity):
    resume(activity)
    service = ChildModelService(activity[0])
    before = service.snapshot("child-game")
    assert balance(activity, "other")["task_resume_receipt_sessions"] == 0
    assert service.snapshot("child-game")["snapshot_id"] == before["snapshot_id"]
    with activity[0].connect() as c:
        dump = "\n".join(c.iterdump())
    expected = service.compact_for_agent("child-game")["companion_balance"]
    with activity[0].connect() as c:
        assert "\n".join(c.iterdump()) == dump
    script = """import json,sys
from home_study_agent.database import Database
from home_study_agent.child_model import ChildModelService
print(json.dumps(ChildModelService(Database(sys.argv[1])).compact_for_agent('child-game')['companion_balance']))
"""
    result = subprocess.run([sys.executable, "-c", script, str(activity[0].path)], capture_output=True,
                            check=True, text=True, env=os.environ.copy())
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("kind", ["game", "content"])
def test_real_controlled_end_with_zero_resume_receipts_does_not_report_return(tmp_path, kind):
    from home_study_agent.controlled_game import ControlledGameService
    from home_study_agent.controlled_content import ControlledContentService
    from test_controlled_game import seed as game_seed
    from test_controlled_content import seed as content_seed
    db = Database(tmp_path / "controlled-model.db")
    db.initialize()
    if kind == "game":
        sid, item = game_seed(db, tmp_path / "synthetic.exe")
        child, service = "child-game", ControlledGameService(db)
        service.update_rule(child, enabled=True, max_daily_minutes=10, max_item_minutes=5)
        service.approve(child, "Game", str(tmp_path / "synthetic.exe"), 5)
        started = service.start(sid, "Game", "start", item)
    else:
        sid, resource = content_seed(db, tmp_path / "video")
        child, item, service = "child-media", "item-media", ControlledContentService(db)
        service.update_rule(child, enabled=True, max_daily_minutes=10, max_item_minutes=5)
        service.approve(child, resource, 5)
        started = service.start(sid, "分数", "start", item)
    identity_key = "game_session_id" if kind == "game" else "content_session_id"
    service.end(sid, "process_exited" if kind == "game" else "media_ended",
                **{identity_key: started[identity_key]})
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM plan_item_event WHERE event_type='resumed'").fetchone()[0] == 0
    result = ChildModelService(db).snapshot(child)["payload"]["companion_balance"]
    assert result["return_to_learning_sessions"] == result["task_resume_receipt_sessions"] == 0
    assert result["boundary_status"] == "return_evidence_missing"


@pytest.mark.parametrize("source", ["voice", "activity"])
def test_repeated_source_revision_changes_watermark_even_if_status_stays_unknown(activity, source):
    if source == "voice":
        turn = voice_turn(activity)
        CompanionActivityVoiceService(activity[0]).execute(turn)
        sql, args = "UPDATE agent_turn SET transcript=? WHERE agent_turn_id=?", (turn,)
    else:
        sql, args = "UPDATE companion_activity SET topic=? WHERE session_id=?", (activity[4],)
    change(activity[0], sql, ("First withdrawn source content", *args))
    first = snapshot(activity)
    change(activity[0], sql, ("Second revised source content", *args))
    second = snapshot(activity)
    assert first["payload"]["companion_balance"]["return_to_learning_sessions"] == 0
    assert second["payload"]["companion_balance"]["return_to_learning_sessions"] == 0
    assert first["source_watermark"]["companion_outcome_projection"] != second["source_watermark"]["companion_outcome_projection"]


def test_duplicate_resume_confirmation_is_one_historical_session_not_more(activity):
    request, receipt, _ = resume(activity)
    before = snapshot(activity)
    activity[2].confirm_resume("child-game", activity[3], request_event_id=request["event_id"],
        plan_item_event_id=receipt, expected_revision=request["revision"], client_action_id="resume-confirm")
    after = snapshot(activity)
    assert after["payload"]["companion_balance"]["task_resume_receipt_sessions"] == 1
    assert after["snapshot_id"] == before["snapshot_id"]


def test_later_deferral_does_not_erase_real_historical_resume(activity):
    _, _, confirmation = resume(activity)
    activity[2].defer_resume("child-game", activity[3], note="Rest later", actor="child",
        expected_revision=confirmation["revision"], client_action_id="later-rest")
    result = balance(activity)
    assert result["task_resume_receipt_sessions"] == 1
    assert result["outcome_evidence"][0]["current_resume_status"] == "deferred"
    assert result["return_to_learning_sessions"] == 0


def test_missing_registered_source_cannot_hide_an_unregistered_legacy_source(activity):
    activity[1].end_companion_activity(activity[4], "turn_limit")
    legacy = legacy_activity(activity[0], activity[4], activity[5])
    change(activity[0], "DELETE FROM companion_activity WHERE activity_id!=?", (legacy,))
    result = balance(activity)
    assert result["total_sessions"] == 1
    assert result["legacy_unverified_sessions"] == 1
    assert result["journal_sessions"] == 0
    assert result["task_resume_receipt_sessions"] == 0


def test_companion_summary_contains_evidence_ids_but_no_verbatim_or_media_path(activity):
    turn = voice_turn(activity, quote="PRIVATE child utterance")
    CompanionActivityVoiceService(activity[0]).execute(turn)
    result = balance(activity)
    assert "PRIVATE" not in json.dumps(result)
    assert str(activity[0].path.parent) not in json.dumps(result)
    assert result["cloud_transmission_allowed"] is False
