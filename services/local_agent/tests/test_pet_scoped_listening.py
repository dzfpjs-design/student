import json

import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.local_supply import score_local_item
from home_study_agent.pet_skill_gate import pet_skill_gate
from home_study_agent.pet_listening_scoring import listening_scoring_item
from test_pet_media_evidence import media, CHILD, change, complete, control, record, review


def twice(media):
    row = complete(media)
    row = record(media, control(media, row, key="second-play"), mono=3000, key="second-start")
    media[5][0] += 1000
    return record(media, row, "ended", 16000, 4000, "second-end")


def save_answer(media, monkeypatch, answer="in front of the library"):
    service = DiagnosticPromptService(media[0])
    with monkeypatch.context() as patch:
        patch.setattr(service, "resume", lambda *args, **kwargs: {"status": "answer_saved"})
        return service.answer(media[2], prompt_id=media[3]["prompt_id"], item_version=media[3]["item_version"],
                              answer=answer, client_action_id="answer")


def gate(media, **kwargs):
    scope = dict(session_id=media[2], prompt_id=media[3]["prompt_id"], item_version=media[3]["item_version"],
                 media_root=media[4].parent)
    scope.update(kwargs)
    with media[0].connect() as c:
        before = c.total_changes
        result = pet_skill_gate(c, CHILD, "diag-pet-listening-1", **scope)
        assert c.total_changes == before
        return result


def test_positive_exact_original_delivery_before_saved_answer_and_restart(media, monkeypatch):
    twice(media)
    save_answer(media, monkeypatch)
    result = gate(media)
    assert result["skill_scoring_allowed"] and result["task_delivery_qualified"]
    with media[0].connect() as c:
        scoring = listening_scoring_item(c, CHILD, "diag-pet-listening-1")
        assert score_local_item(scoring, "in front of the library")[0] == 1
        assert score_local_item(scoring, "not in front of the library, go to the football field")[0] == 0
        assert score_local_item(scoring, "library front")[0] == 0
        assert not pet_skill_gate(c, CHILD, "diag-pet-listening-1")["skill_scoring_allowed"]
    answer = DiagnosticPromptService(media[0]).resume(media[2], prompt_id=media[3]["prompt_id"])
    result = gate(media, attempt_id=answer["attempt"]["attempt_id"], evidence_id=answer["attempt"]["evidence_id"])
    assert result["skill_scoring_allowed"]
    assert result["scope_evidence"]["answered_at"] < answer["attempt"]["created_at"]


@pytest.mark.parametrize("fault", ["only_one", "answer_first", "review_late", "source_withdraw", "review_retract",
    "changed_bytes", "wrong_session", "wrong_prompt", "wrong_item_version", "wrong_attempt", "wrong_evidence"])
def test_no_item_level_or_late_delivery_admission(media, monkeypatch, fault):
    if fault == "answer_first":
        save_answer(media, monkeypatch)
        # Existing binding can finish late, but admission must use answer arrival.
        # Begin requires waiting; create it before the saved answer instead.
        assert not gate(media)["skill_scoring_allowed"]
        return
    row = complete(media) if fault == "only_one" else twice(media)
    save_answer(media, monkeypatch)
    if fault == "review_late":
        review(media, row, "approve", "later-review")
    elif fault == "source_withdraw":
        change(media[0], "UPDATE capture_artifact SET status='deleted' WHERE artifact_id='audio'")
    elif fault == "review_retract":
        review(media, row, "retract", "retract")
    elif fault == "changed_bytes":
        media[4].write_bytes(b"not current original audio")
    scope = {fault[6:]: "different"} if fault.startswith("wrong_") else {}
    mapping = {"session": "session_id", "prompt": "prompt_id", "attempt": "attempt_id", "evidence": "evidence_id"}
    scope = {mapping.get(key, key): value for key, value in scope.items()}
    assert not gate(media, **scope)["skill_scoring_allowed"]


def test_resume_after_late_second_play_preserves_original_answer_cutoff(media, monkeypatch):
    row = complete(media)
    save_answer(media, monkeypatch)
    row = record(media, control(media, row, key="late-play"), mono=3000, key="late-start")
    media[5][0] += 1000
    record(media, row, "ended", 16000, 4000, "late-end")
    assert not gate(media)["skill_scoring_allowed"]
    result = DiagnosticPromptService(media[0]).resume(media[2], prompt_id=media[3]["prompt_id"])
    assert not gate(media, attempt_id=result["attempt"]["attempt_id"])["skill_scoring_allowed"]


@pytest.mark.parametrize("item,modality", [("diag-pet-listening-1", "listening"),
    ("diag-pet-listening-gist-baseline", "listening"), ("diag-pet-speaking-2", "speaking"),
    ("diag-pet-reading-1", None)])
def test_legacy_next_item_has_authoritative_modality(media, item, modality):
    result = LearningStateService(media[0]).next_diagnostic(CHILD, "pet", preferred_item_id=item)
    assert result["pet_audio_modality"] == modality


def test_new_submission_gate_is_not_only_a_historical_projection(media, monkeypatch):
    change(media[0], "UPDATE daily_session SET state='study' WHERE session_id=?", (media[2],))
    # Regression defense against a caller that still uses the old exact scorer.
    monkeypatch.setattr("home_study_agent.learning_state.qualified_exact_score", lambda *args, **kwargs: ({}, 1.0))
    result = LearningStateService(media[0]).submit_diagnostic(media[2], "diag-pet-listening-1", "library front", "direct")
    with media[0].connect() as c:
        evidence = c.execute("SELECT independent,confidence FROM learning_evidence WHERE evidence_id=?", (result["evidence_id"],)).fetchone()
    assert evidence["independent"] == 0 and evidence["confidence"] < 1
    assert "核实" in result["feedback"]


def test_new_trusted_submission_uses_scoped_exact_score_in_same_transaction(media, monkeypatch):
    twice(media)
    save_answer(media, monkeypatch)
    result = LearningStateService(media[0]).submit_diagnostic(media[2], "diag-pet-listening-1",
        "in front of the library", "diagnostic-prompt-answer:" + media[3]["prompt_id"],
        _trusted_prompt_id=media[3]["prompt_id"], _pet_media_scope={"media_root": media[4].parent})
    with media[0].connect() as c:
        evidence = c.execute("SELECT independent,confidence,evidence_summary FROM learning_evidence WHERE evidence_id=?",
                             (result["evidence_id"],)).fetchone()
        saved_feedback = c.execute("SELECT feedback FROM diagnostic_attempt WHERE attempt_id=?", (result["attempt_id"],)).fetchone()[0]
    assert evidence["independent"] == 1 and evidence["confidence"] == 1
    assert ";prompt_answer_saved_at=" in evidence["evidence_summary"]
    # The public attempt also runs main's historical projection. Until it passes
    # exact media scope, its current view correctly remains needs_review.
    assert result["correctness"] == 1 and "本次原题" in saved_feedback
    DiagnosticPromptService(media[0]).resume(media[2], prompt_id=media[3]["prompt_id"])
    assert gate(media, evidence_id=result["evidence_id"])["skill_scoring_allowed"]


def test_bare_media_scope_is_not_scoring_permission(media):
    with pytest.raises(ValueError, match="trusted diagnostic media scope required"):
        LearningStateService(media[0]).submit_diagnostic(media[2], "diag-pet-listening-1", "library", "plain",
            _pet_media_scope={"media_root": media[4].parent})
