"""Voice service journeys on disposable databases; no microphone or family data."""
from datetime import timedelta
import json
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.learning_state import LearningStateService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.mistake_capture import InvalidMistakeCaptureCandidateError
from home_study_agent.repository import StudyRepository
from test_mistake_gate_reliability import CHILD, NODE, ITEM, SECOND, flow


def opened(flow):
    db, _, sid, _ = flow
    book = MistakeBookService(db)
    case = book.capture(child_id=CHILD, knowledge_node_id=NODE, subject="数学", title="四则运算",
        prompt_summary="计算 (24+6)×3，只回答一个整数。", submitted_answer="54", source_type="manual",
        source_ref=None, confidence=1, client_action_id="dialogue-case", diagnostic_item_id=ITEM)["mistake_id"]
    book.confirm(case, CHILD, "confirm", "dialogue-confirm")
    dialogue = MistakeDialogueService(db)
    result = dialogue.open_review(sid, "open-dialogue")
    assert result["phase"] == "diagnosis"
    return dialogue, book, case, sid


def speak(dialogue, sid, text, action=None):
    return dialogue.respond(sid, text, action or str(uuid4()))


def local_variant(db, case):
    from home_study_agent.variant_supply import VariantSupplyService

    class NoCloud:
        configured = False

        async def complete(self, *args, **kwargs):
            raise AssertionError("Cloud calls forbidden")

    supply = VariantSupplyService(db, NoCloud())
    supply.enqueue_local(case, CHILD, "dialogue-local-supply")
    assert supply.process_local_pending()["jobs"][0]["status"] == "completed"
    with db.connect() as c:
        candidate = c.execute("SELECT candidate_id FROM variant_candidate WHERE mistake_id=? AND stage='near_transfer'", (case,)).fetchone()[0]
    item_id = supply.qualify_local(candidate, CHILD, "dialogue-local-qualification")["diagnostic_item_id"]
    with db.connect() as c:
        answer = json.loads(c.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()[0])["exact_answers"][0]
    return item_id, answer


def test_voice_diagnosis_scores_once_then_remediates_and_verifies_delayed_gate(flow):
    db, _, _, clock = flow
    dialogue, book, case, sid = opened(flow)
    reason = "不是粗心，我不知道为什么错"
    started = speak(dialogue, sid, reason, "voice-diagnosis")
    assert started["phase"] == "diagnosis_probe"
    diagnosis = book.diagnosis_state(case, CHILD)
    assert diagnosis["hypotheses"][0]["stance"] == "unknown"
    assert diagnosis["hypotheses"][0]["explanation"] == reason
    assert "独立完成" in started["response_text"]
    assert not dialogue.teaching_request(started)
    clock[0] += timedelta(seconds=1)
    answer = speak(dialogue, sid, "答案是42", "voice-diagnosis-answer")
    assert answer["phase"] == "remediation"
    assert "不能确定错因" in answer["response_text"]
    assert book.diagnosis_state(case, CHILD)["probes"][0]["evidence"]
    assert speak(dialogue, sid, "答案是42", "voice-diagnosis-answer") == answer
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM mistake_attempt").fetchone()[0] == 0
    local_variant(db, case)
    ready = speak(dialogue, sid, "明白了", "voice-remediated")
    assert ready["phase"] == "attempt"
    item_id = ready["state"]["next_activity"]["item"]["diagnostic_item_id"]
    with db.connect() as c:
        value = json.loads(c.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()[0])["exact_answers"][0]
    checked = speak(dialogue, sid, value, "voice-transfer")
    assert checked["phase"] == "scheduled"
    assert book.get(case, CHILD)["status"] != "mastered"
    clock[0] += timedelta(hours=49)
    dialogue = MistakeDialogueService(Database(db.path))
    delayed = speak(dialogue, sid, "继续错题", "voice-delay-resume")
    assert delayed["phase"] == "attempt"
    with db.connect() as c:
        delayed_answer = json.loads(c.execute("SELECT expected_answer_json FROM diagnostic_item WHERE diagnostic_item_id=?",
            (delayed["state"]["next_activity"]["item"]["diagnostic_item_id"],)).fetchone()[0])["exact_answers"][0]
    speak(dialogue, sid, delayed_answer, "voice-delayed-check")
    assert book.get(case, CHILD)["status"] == "mastered"
    assert book.diagnosis_state(case, CHILD)["assessment"]["causal_status"] == "unconfirmed"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM mistake_attempt").fetchone()[0] == 2


@pytest.mark.parametrize("utterance", ["是的", "确认", "对"])
def test_generic_affirmation_does_not_become_diagnosis_evidence(flow, utterance):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)

    result = speak(dialogue, sid, utterance, f"generic-diagnosis-{utterance}")

    assert result["phase"] == "diagnosis"
    assert "不需要你确认这是不是错题" in result["response_text"]
    assert book.diagnosis_state(case, CHILD)["version"] == 0
    with db.connect() as c:
        table = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mistake_diagnosis_event'"
        ).fetchone()
        assert not table or c.execute(
            "SELECT COUNT(*) FROM mistake_diagnosis_event WHERE mistake_id=?", (case,)
        ).fetchone()[0] == 0


@pytest.mark.parametrize("failure_point", ["score", "attach", "save"])
def test_interrupted_answer_pipeline_resumes_without_double_scoring(flow, monkeypatch, failure_point):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里出错了")

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic interruption")

    target, name = ((LearningStateService, "submit_diagnostic") if failure_point == "score" else
                    (MistakeBookService, "attach_diagnosis_evidence") if failure_point == "attach" else
                    (MistakeDialogueService, "_save"))
    original = getattr(target, name)
    monkeypatch.setattr(target, name, fail)
    failed = speak(dialogue, sid, "42", "interrupted-answer")
    assert "重试" in failed["response_text"]
    assert dialogue.state(sid)["state"]["pending_operation"]["answer"] == "42"
    # Answer evidence and scoring now commit together; the earlier reservation
    # still protects the child's words when that transaction rolls back.
    assert bool(book.diagnosis_state(case, CHILD)["probes"][0]["answer"]) == (failure_point == 'save')
    monkeypatch.setattr(target, name, original)
    restarted = MistakeDialogueService(Database(db.path))
    resumed = speak(restarted, sid, "重试", "resume-interrupted")
    assert "pending_operation" not in resumed["state"]
    assert book.diagnosis_state(case, CHILD)["probes"][0]["evidence"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM mistake_diagnosis_event WHERE kind='answer'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM mistake_diagnosis_event WHERE kind='evidence'").fetchone()[0] == 1


def test_restarting_and_hinting_keeps_probe_binding_and_nonindependence(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    probe = book.diagnosis_state(case, CHILD)["probes"][0]["event_id"]
    hinted = speak(dialogue, sid, "给我一个提示")
    assert dialogue.teaching_request(hinted)
    dialogue = MistakeDialogueService(Database(db.path))
    speak(dialogue, sid, "继续错题")
    assert book.diagnosis_state(case, CHILD)["probes"][0]["event_id"] == probe
    speak(dialogue, sid, "42")
    evidence = book.diagnosis_state(case, CHILD)["probes"][0]["evidence"][0]
    assert not evidence["eligible"]
    with db.connect() as c:
        assert c.execute("SELECT hint_level FROM learning_evidence WHERE evidence_id=?", (evidence["evidence_id"],)).fetchone()[0] == 1


def test_child_switch_cannot_answer_or_replay_other_child_dialogue(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚", "child-one-reason")
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        c.commit()
    other = StudyRepository(db).start_session("child-junior-3", "child-two-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other,))
        c.commit()
    with pytest.raises(InvalidMistakeCaptureCandidateError):
        speak(dialogue, sid, "42")
    with pytest.raises(InvalidMistakeCaptureCandidateError):
        speak(dialogue, other, "不清楚", "child-one-reason")
    assert dialogue.open_review(other, "other-review") is None
    assert book.diagnosis_state(case, CHILD)["probes"][0]["answer"] is None


def test_changed_question_does_not_score_answer_against_a_new_prompt(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET prompt='changed source' WHERE diagnostic_item_id=?", (SECOND,))
        c.commit()
    response = speak(dialogue, sid, "42")
    assert "变化" in response["response_text"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 0


def test_spoken_answer_revision_and_retraction_preserve_history(flow):
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    speak(dialogue, sid, "42")
    original = book.diagnosis_state(case, CHILD)["probes"][0]["answer"]["event_id"]
    changed = speak(dialogue, sid, "刚才答案改成90", "spoken-revision")
    assert speak(dialogue, sid, "刚才答案改成90", "spoken-revision") == changed
    state = book.diagnosis_state(case, CHILD)
    assert next(e for e in state["history"] if e["event_id"] == original)["active"] is False
    assert state["probes"][0]["answer"]["answer"] == "90"
    speak(dialogue, sid, "撤回刚才的回答")
    state = book.diagnosis_state(case, CHILD)
    assert state["probes"][0]["answer"] is None
    assert state["assessment"]["support"] == []


def test_concurrent_voice_answers_cannot_overwrite_pending_operation(flow, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    barrier = Barrier(2)
    checkpoint = MistakeDialogueService._checkpoint

    def synchronized(self, focus, operation):
        if not focus["state"].get("pending_operation"):
            barrier.wait(timeout=5)
        return checkpoint(self, focus, operation)

    monkeypatch.setattr(MistakeDialogueService, "_checkpoint", synchronized)

    def answer(value):
        try:
            return speak(MistakeDialogueService(Database(db.path)), sid, value)
        except InvalidMistakeCaptureCandidateError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(answer, ["42", "90"]))
    assert sum(r is not None for r in results) == 1
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
    assert not dialogue.state(sid)["state"].get("pending_operation")


def test_external_pending_answer_is_scored_and_linked_on_voice_resume(flow):
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    diagnosis = book.diagnosis_state(case, CHILD)
    book.answer_diagnosis_probe(case, CHILD, diagnosis["probes"][0]["event_id"], "42", diagnosis["version"], "http-answer")
    result = speak(dialogue, sid, "继续错题")
    assert result["phase"] == "remediation"
    assert book.diagnosis_state(case, CHILD)["probes"][0]["evidence"]


def test_review_reopen_preserves_verification_progress_without_reasking_diagnosis(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    speak(dialogue, sid, "42")
    local_variant(db, case)
    speak(dialogue, sid, "明白了")
    reopened = MistakeDialogueService(Database(db.path)).open_review(sid, "reopen-diagnosis")
    assert reopened["phase"] == "attempt"
    assert len(book.diagnosis_state(case, CHILD)["probes"]) == 1


def test_stale_focus_cannot_reopen_completed_answer_as_pending(flow):
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    stale = dialogue.state(sid)
    speak(dialogue, sid, "42")
    with pytest.raises(InvalidMistakeCaptureCandidateError):
        dialogue._answer_probe(stale, "90", "stale-answer")
    assert not dialogue.state(sid)["state"].get("pending_operation")


def test_read_and_replay_refresh_external_evidence_projection(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    first = speak(dialogue, sid, "42", "replayed-diagnosis-answer")
    old_version = first["state"]["diagnosis"]["projection_version"]
    with db.connect() as c:
        c.execute("UPDATE learning_evidence SET confidence=.1")
        c.commit()
    read = dialogue.state(sid)
    assert read["state"]["diagnosis"]["projection_version"] != old_version
    replay = speak(dialogue, sid, "42", "replayed-diagnosis-answer")
    assert replay["state"]["diagnosis"]["projection_version"] == read["state"]["diagnosis"]["projection_version"]


def test_pending_answer_can_resume_in_a_new_session_for_the_same_child(flow, monkeypatch):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    attach = MistakeBookService.attach_diagnosis_evidence

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic connection loss")

    monkeypatch.setattr(MistakeBookService, "attach_diagnosis_evidence", fail)
    speak(dialogue, sid, "42", "cross-session-answer")
    monkeypatch.setattr(MistakeBookService, "attach_diagnosis_evidence", attach)
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        c.commit()
    resumed_sid = StudyRepository(db).start_session(CHILD, "same-child-next-session")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (resumed_sid,))
        c.commit()
    reopened = MistakeDialogueService(Database(db.path))
    assert reopened.open_review(resumed_sid, "next-session-review")["state"]["pending_operation"]
    result = speak(reopened, resumed_sid, "重试", "next-session-retry")
    assert not result["state"].get("pending_operation")
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1


def test_real_agent_turn_execute_routes_new_probe_without_double_write(tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from test_mistake_voice_journey import ok, say, settings

    with TestClient(create_app(settings(tmp_path))) as client:
        sid = ok(client.post("/v1/sessions", json={"child_id": CHILD, "client_action_id": "voice-integration-session"}))["session_id"]
        db = Database(client.app.state.settings.database_path)
        with db.connect() as c:
            c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            c.commit()
        book = MistakeBookService(db)
        case = book.capture(child_id=CHILD, knowledge_node_id=NODE, subject="数学", title="四则运算",
            prompt_summary="计算 (24+6)×3，只回答一个整数。", submitted_answer="54", source_type="manual",
            source_ref=None, confidence=1, client_action_id="http-voice-case", diagnostic_item_id=ITEM)["mistake_id"]
        book.confirm(case, CHILD, "confirm", "http-voice-confirm")
        MistakeDialogueService(db).open_review(sid, "http-open-review")
        explained = say(client, sid, "不是粗心，我不知道哪里出了问题", "http-reason")
        assert explained["execution"]["result"]["phase"] == "diagnosis_probe"
        assert "独立完成" in explained["turn"]["response_text"]
    with TestClient(create_app(settings(tmp_path))) as client:
        answered = say(client, sid, "42", "http-probe-answer")
        assert answered["execution"]["result"]["phase"] == "remediation"
        diagnosis = MistakeBookService(db).diagnosis_state(case, CHILD)
        assert diagnosis["probes"][0]["evidence"]
        assert diagnosis["assessment"]["causal_status"] == "unconfirmed"
        say(client, sid, "42", "http-probe-answer")
        with db.connect() as c:
            assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt WHERE child_id=?", (CHILD,)).fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM mistake_attempt WHERE child_id=?", (CHILD,)).fetchone()[0] == 0


def test_initial_diagnosis_committed_before_focus_failure_is_not_written_twice(flow, monkeypatch):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    save = MistakeDialogueService._save

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic focus write failure")

    monkeypatch.setattr(MistakeDialogueService, "_save", fail)
    with pytest.raises(RuntimeError):
        speak(dialogue, sid, "不清楚", "initial-diagnosis-fault")
    monkeypatch.setattr(MistakeDialogueService, "_save", save)
    resumed = speak(MistakeDialogueService(Database(db.path)), sid, "不清楚", "initial-diagnosis-fault")
    assert resumed["phase"] == "diagnosis_probe"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM mistake_diagnosis_event WHERE kind='hypotheses'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM mistake_diagnosis_event WHERE kind='probe'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM mistake_diagnosis").fetchone()[0] == 1


def test_committed_remediation_recovers_on_new_retry_without_repeating_it(flow, monkeypatch):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    speak(dialogue, sid, "42")
    local_variant(db, case)
    save = MistakeDialogueService._save

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic focus write failure")

    monkeypatch.setattr(MistakeDialogueService, "_save", fail)
    with pytest.raises(RuntimeError):
        speak(dialogue, sid, "明白了", "remediation-fault")
    monkeypatch.setattr(MistakeDialogueService, "_save", save)
    resumed = speak(MistakeDialogueService(Database(db.path)), sid, "重试", "retry-remediation-fault")
    assert resumed["phase"] == "attempt"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM remediation_step WHERE mistake_id=? AND outcome='completed'", (case,)).fetchone()[0] == 1


def test_pause_preserves_pending_operation_without_finishing_it(flow, monkeypatch):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    attach = MistakeBookService.attach_diagnosis_evidence

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic failure")

    monkeypatch.setattr(MistakeBookService, "attach_diagnosis_evidence", fail)
    speak(dialogue, sid, "42", "pause-pending-answer")
    paused = speak(dialogue, sid, "关闭错题整理", "pause-pending")
    assert paused["phase"] == "paused"
    assert paused["state"]["pending_operation"]
    assert not book.diagnosis_state(case, CHILD)["probes"][0]["evidence"]
    monkeypatch.setattr(MistakeBookService, "attach_diagnosis_evidence", attach)
    resumed = speak(MistakeDialogueService(Database(db.path)), sid, "继续错题")
    assert not resumed["state"].get("pending_operation")
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1


def test_existing_score_with_wrong_hint_conditions_is_not_reused(flow):
    db, learning, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "不清楚")
    speak(dialogue, sid, "给我提示")
    diagnosis = book.diagnosis_state(case, CHILD)
    answered = book.answer_diagnosis_probe(case, CHILD, diagnosis["probes"][0]["event_id"], "42", diagnosis["version"], "outside-answer")
    learning.submit_diagnostic(sid, SECOND, "42", f"mistake-diagnosis-score:{answered['event_id']}")
    result = speak(dialogue, sid, "继续错题")
    assert result["state"]["pending_error"] == "InvalidMistakeCaptureCandidateError"
    assert not book.diagnosis_state(case, CHILD)["probes"][0]["evidence"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
