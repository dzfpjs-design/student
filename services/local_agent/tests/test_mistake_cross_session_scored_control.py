"""A prior-session scored answer needs a local readback before mutation."""
import json
from datetime import timedelta
from dataclasses import replace as replace_settings

import pytest

from home_study_agent.database import Database
from home_study_agent.app import create_app
from home_study_agent.learning_obligations import withdrawn_evidence_ids
from home_study_agent.mistake_capture import InvalidMistakeCaptureCandidateError
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.repository import StudyRepository
from test_mistake_dialogue_diagnosis import opened, speak
from test_mistake_gate_reliability import CHILD, ITEM, NODE, SECOND, flow
from test_mistake_pending_answer_http import execute, hear
from test_mistake_voice_journey import settings
from fastapi.testclient import TestClient


def next_session(db, old_session, action="cross-session-next"):
    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?",
                  (old_session,))
        c.commit()
    session_id = StudyRepository(db).start_session(CHILD, action)["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session_id,))
        c.commit()
    return session_id


def scored_diagnosis(flow, answer="41"):
    db, _, _, _ = flow
    dialogue, book, case, old_session = opened(flow)
    speak(dialogue, old_session, "我不知道哪里错了", "cross-reason")
    speak(dialogue, old_session, answer, "cross-old-answer")
    with db.connect() as c:
        old = dict(c.execute("SELECT * FROM diagnostic_attempt").fetchone())
    return db, book, case, old_session, old


def test_prior_session_withdrawal_reads_back_then_replays_authority(flow):
    db, _, case, old_session, old = scored_diagnosis(flow)
    current_session = next_session(db, old_session)
    dialogue = MistakeDialogueService(Database(db.path))

    offered = speak(dialogue, current_session, "把上次那道题的答案撤回", "cross-offer")
    assert "计算 (24+6)×3" in offered["response_text"]
    assert "41" in offered["response_text"]
    assert "确认" in offered["response_text"]
    assert offered["state"]["historical_scored_control"]["control"] == "withdraw"
    with db.connect() as c:
        assert old["evidence_id"] not in withdrawn_evidence_ids(c, CHILD)

    restarted = MistakeDialogueService(Database(db.path))
    completed = speak(restarted, current_session, "对，就是这道，确认撤回", "cross-confirm")
    assert "已撤回" in completed["response_text"]
    assert "historical_scored_control" not in completed["state"]
    with db.connect() as c:
        assert old["evidence_id"] in withdrawn_evidence_ids(c, CHILD)
        receipt = dict(c.execute(
            "SELECT * FROM learning_evidence_withdrawal WHERE evidence_id=?",
            (old["evidence_id"],)).fetchone())
        assert receipt["session_id"] == old_session
        audit = json.loads(c.execute("""SELECT summary_json FROM audit_event
            WHERE action='learning_answer_withdrawn' ORDER BY rowid DESC LIMIT 1""").fetchone()[0])
        assert audit["origin_session_id"] == old_session
        assert audit["control_session_id"] == current_session
        assert audit["mistake_id"] == case


def test_prior_session_replacement_scores_in_current_session_and_preserves_origin(flow):
    db, _, _, old_session, old = scored_diagnosis(flow)
    current_session = next_session(db, old_session, "cross-replace-next")
    dialogue = MistakeDialogueService(Database(db.path))

    offered = speak(dialogue, current_session, "上次那道题的答案改成42", "cross-replace-offer")
    assert "改成“42”" in offered["response_text"]
    assert offered["state"]["historical_scored_control"]["control"] == "replace"
    speak(dialogue, current_session, "确认更正", "cross-replace-confirm")

    with db.connect() as c:
        attempts = [dict(row) for row in c.execute(
            "SELECT * FROM diagnostic_attempt ORDER BY rowid")]
        assert [row["submitted_answer"] for row in attempts] == ["41", "42"]
        replacement = c.execute("SELECT * FROM learning_evidence WHERE evidence_id=?",
                                (attempts[-1]["evidence_id"],)).fetchone()
        assert replacement["session_id"] == current_session
        receipt = dict(c.execute(
            "SELECT * FROM learning_evidence_withdrawal WHERE evidence_id=?",
            (old["evidence_id"],)).fetchone())
        assert receipt["session_id"] == old_session
        assert receipt["replacement_evidence_id"] == attempts[-1]["evidence_id"]


def test_prior_session_control_can_be_cancelled_without_side_effect(flow):
    db, _, _, old_session, old = scored_diagnosis(flow)
    current_session = next_session(db, old_session, "cross-cancel-next")
    dialogue = MistakeDialogueService(db)
    speak(dialogue, current_session, "撤回上次那道题的答案", "cross-cancel-offer")
    cancelled = speak(dialogue, current_session, "不是这道，取消", "cross-cancel")
    assert "没有撤回" in cancelled["response_text"]
    assert "historical_scored_control" not in cancelled["state"]
    with db.connect() as c:
        assert old["evidence_id"] not in withdrawn_evidence_ids(c, CHILD)


def test_prior_session_offer_rejects_source_change_and_other_child(flow):
    db, _, _, old_session, old = scored_diagnosis(flow)
    current_session = next_session(db, old_session, "cross-source-next")
    dialogue = MistakeDialogueService(db)
    offered = speak(dialogue, current_session, "撤回上次那道题的答案", "cross-source-offer")
    item_id = offered["state"]["historical_scored_control"]["binding"]["diagnostic_item_id"]
    with db.connect() as c:
        c.execute("UPDATE diagnostic_item SET prompt=prompt || '题源变更' WHERE diagnostic_item_id=?",
                  (item_id,))
        c.commit()
    with pytest.raises(InvalidMistakeCaptureCandidateError):
        speak(dialogue, current_session, "确认撤回", "cross-source-confirm")
    with db.connect() as c:
        assert old["evidence_id"] not in withdrawn_evidence_ids(c, CHILD)

    with db.connect() as c:
        c.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?",
                  (current_session,))
        c.commit()
    other = StudyRepository(db).start_session("child-junior-3", "cross-other-child")["session_id"]
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (other,))
        c.commit()
    other_dialogue = MistakeDialogueService(db)
    with pytest.raises(InvalidMistakeCaptureCandidateError):
        speak(other_dialogue, other, "撤回上次那道题的答案", "cross-other-offer")


def test_yesterday_scope_and_committed_reply_recovery(flow, monkeypatch):
    db, _, _, old_session, old = scored_diagnosis(flow)
    flow[3][0] += timedelta(days=1)
    current_session = next_session(db, old_session, "cross-yesterday-next")
    dialogue = MistakeDialogueService(db)
    speak(dialogue, current_session, "撤回昨天那道题的答案", "cross-yesterday-offer")
    original_save = MistakeDialogueService._save

    def fail_once(self, *args, **kwargs):
        raise RuntimeError("synthetic reply save failure")

    monkeypatch.setattr(MistakeDialogueService, "_save", fail_once)
    failed = speak(dialogue, current_session, "确认撤回", "cross-yesterday-confirm")
    assert "重试" in failed["response_text"]
    monkeypatch.setattr(MistakeDialogueService, "_save", original_save)

    restarted = MistakeDialogueService(Database(db.path))
    resumed = speak(restarted, current_session, "重试", "cross-yesterday-retry")
    assert "pending_operation" not in resumed["state"]
    with db.connect() as c:
        assert old["evidence_id"] in withdrawn_evidence_ids(c, CHILD)
        assert c.execute("SELECT COUNT(*) FROM learning_evidence_withdrawal").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='learning_answer_withdrawn'").fetchone()[0] == 1


def test_agent_runtime_routes_prior_session_control_without_cloud_history(flow, tmp_path):
    db, _, _, old_session, old = scored_diagnosis(flow)
    current_session = next_session(db, old_session, "cross-runtime-next")
    configured = replace_settings(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured)) as client:
        offered_turn = hear(client, current_session, "把上次那道题的答案撤回", "cross-runtime-offer")
        assert offered_turn["action"]["type"] == "continue_mistake_dialogue"
        assert offered_turn["action"]["historical_scored_control"] == "withdraw"
        assert offered_turn["action"]["local_only"] is True
        assert offered_turn["action"]["cloud_transmission_allowed"] is False
        offered = execute(client, offered_turn)
        assert "确认" in offered["execution"]["result"]["response_text"]

        confirmed_turn = hear(client, current_session, "对，就是这道，确认撤回", "cross-runtime-confirm")
        assert confirmed_turn["action"]["type"] == "continue_mistake_dialogue"
        confirmed = execute(client, confirmed_turn)
        assert "已撤回" in confirmed["execution"]["result"]["response_text"]
    with db.connect() as c:
        assert old["evidence_id"] in withdrawn_evidence_ids(c, CHILD)
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0


def test_yesterday_with_multiple_scored_answers_requires_spoken_selection(flow):
    db, _, _, first_session, first = scored_diagnosis(flow)
    second_session = next_session(db, first_session, "cross-second-old-session")
    book = MistakeBookService(db)
    second_case = book.capture(child_id=CHILD, knowledge_node_id=NODE, subject="数学",
        title="另一道题", prompt_summary="计算 8×7，只回答一个整数。", submitted_answer="48",
        source_type="manual", source_ref=None, confidence=1,
        client_action_id="cross-second-case", diagnostic_item_id=SECOND)["mistake_id"]
    book.confirm(second_case, CHILD, "confirm", "cross-second-confirm")
    dialogue = MistakeDialogueService(db)
    second_open = dialogue.open_review(second_session, "cross-second-open", mistake_id=second_case)
    second_reason = speak(dialogue, second_session, "我不知道哪里错了", "cross-second-reason")
    second_answer = speak(dialogue, second_session, "41", "cross-second-answer")
    with db.connect() as c:
        second_row = c.execute("SELECT * FROM diagnostic_attempt WHERE session_id=?",
                               (second_session,)).fetchone()
        assert second_row is not None, (
            second_open["phase"], second_open["response_text"],
            second_reason["phase"], second_reason["response_text"],
            second_answer["phase"], second_answer["response_text"])
        second = dict(second_row)
    flow[3][0] += timedelta(days=1)
    current_session = next_session(db, second_session, "cross-multiple-current")

    listed = speak(dialogue, current_session, "撤回昨天那道题的答案", "cross-multiple-list")
    assert "不止一道" in listed["response_text"]
    assert "第1道" in listed["response_text"] and "第2道" in listed["response_text"]
    with db.connect() as c:
        assert not withdrawn_evidence_ids(c, CHILD)

    selected = speak(dialogue, current_session, "第一道", "cross-multiple-select")
    assert ("计算 8×7" in selected["response_text"]
            or "计算 (24+6)×3" in selected["response_text"])
    assert "确认" in selected["response_text"]
    selected_evidence = selected["state"]["historical_scored_control"]["binding"]["evidence_id"]
    speak(dialogue, current_session, "确认撤回", "cross-multiple-confirm")
    with db.connect() as c:
        withdrawn = withdrawn_evidence_ids(c, CHILD)
        assert withdrawn == {selected_evidence}
        assert len({first["evidence_id"], second["evidence_id"]} - withdrawn) == 1
