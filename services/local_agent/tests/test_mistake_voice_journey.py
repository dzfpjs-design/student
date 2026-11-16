"""Full HTTP agent-turn journey; test speech is synthetic, not child evidence."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.mistake_dialogue import MistakeDialogueService, subject_for_question
from home_study_agent.mistake_capture import InvalidMistakeCaptureCandidateError
from home_study_agent.paper_scoring import PaperScoringService


def settings(root):
    return Settings(data_dir=root, database_path=root / "test.db", data_encryption_key=b"0" * 32, maintenance_enabled=False)


def ok(response):
    assert response.status_code in (200, 201), response.text[:1200]
    return response.json()


def fixture(client, artifact="voice-photo", child="child-primary-4"):
    session = ok(client.post("/v1/sessions", json={"child_id": child, "client_action_id": artifact + "-session"}))["session_id"]
    db = Database(client.app.state.settings.database_path)
    question = "4. 选出下列单词中划线字母读音不同的一项。A. grape B. make C. like"
    details = {"local_pipeline": {"wrong_question_candidates": [{"text": question, "confidence": .95,
               "evidence": "red_correction_mark_near_question", "requires_child_confirmation": True,
               "bbox_xywh": [40, 80, 500, 180]}]}}
    with db.connect() as c:
        c.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (session,))
        c.execute("""INSERT INTO capture_artifact(artifact_id, child_id, session_id, artifact_type, local_path,
            extracted_text, confidence, details_json, status, created_at, client_action_id)
            VALUES (?, ?, ?, 'document_ocr', NULL, ?, .95, ?, 'completed', datetime('now'), ?)""",
            (artifact, child, session, question, json.dumps(details), artifact + "-capture"))
        c.commit()
    PaperScoringService(db).register_exact_authority(
        child_id=child, session_id=session, question_text=question,
        expected_answers=["C"], authority_type="published_answer_key",
        authority_ref=f"fixture-workbook:{artifact}:q4", rubric_version="fixture-v1",
        client_action_id=f"{artifact}-answer-authority",
    )
    return session, db


def say(client, session, text, key):
    turn = ok(client.post(f"/v1/sessions/{session}/agent-turn", json={"transcript": text, "client_action_id": key}))
    assert turn["action"]["type"] == "continue_mistake_dialogue", turn
    executed = ok(client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute"))
    return executed


def begin(client, session, artifact="voice-photo"):
    return ok(client.post(f"/v1/sessions/{session}/mistake-dialogue", json={"artifact_id": artifact, "client_action_id": "begin-" + artifact}))


def focus(client, session):
    return ok(client.get(f"/v1/sessions/{session}/mistake-dialogue"))["dialogue"]


def test_camera_voice_confirmation_restart_reference_answers_do_not_certify(tmp_path: Path):
    with TestClient(create_app(settings(tmp_path))) as client:
        session, db = fixture(client)
        started = begin(client, session)
        assert started["state"]["subject"] == "英语"
        assert started["state"]["knowledge_node_id"] == "node-p4-english-phonics-discrimination"
        assert "不一定" in started["response_text"]
        selected = say(client, session, "第四题", "voice-select-number")
        assert selected["execution"]["result"]["candidate_id"] == started["candidate_id"]
        say(client, session, "对", "voice-confirm-stem")
        say(client, session, "我记不清了", "voice-forgot-original")
        assert focus(client, session)["phase"] == "answer"
        assert not focus(client, session)["state"].get("submitted_answer")
        say(client, session, "我选B", "voice-original-answer")
        assert focus(client, session)["state"]["submitted_answer"] == "B"
        assert db.path.exists()
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM mistake_case").fetchone()[0] == 0
    with TestClient(create_app(settings(tmp_path))) as client:
        assert focus(client, session)["phase"] == "confirm"
        saved = say(client, session, "是的", "voice-confirm-save")
        case_id = saved["execution"]["result"]["mistake_id"]
        assert case_id
        assert "为什么" in saved["turn"]["response_text"]
        repeated = say(client, session, "是的", "voice-confirm-save")
        assert repeated["execution"]["result"]["mistake_id"] == case_id
        diagnosed = say(client, session, "我是看拼写选的，不知道比较读音的方法", "voice-reason")
        # No configured model in this fixture: diagnosis survives a real tutor error.
        assert diagnosed["execution"]["teaching_error"]
        assert "已经保存" in diagnosed["turn"]["response_text"]
        case = client.app.state.mistake_book.get(case_id, "child-primary-4")
        assert case["diagnosis"]["status"] == "candidate"
        assert case["diagnosis"]["confidence"] == .6
        assert focus(client, session)["phase"] == "remediation"
        say(client, session, "跳过", "voice-skip-saved")
        assert focus(client, session)["phase"] == "paused"
        assert client.app.state.mistake_book.get(case_id, "child-primary-4")["status"] != "excluded"
        say(client, session, "继续错题", "voice-resume-saved")
        retried = say(client, session, "重试", "voice-teach-retry")
        assert retried["execution"]["result"]["mistake_id"] == case_id
        say(client, session, "明白了", "voice-understood")
        assert focus(client, session)["phase"] == "attempt"
        # Explicit test answers, never inserted into the user's family DB.
        for i in range(3):
            current = focus(client, session)
            activity = current["state"]["next_activity"]
            assert activity["kind"] == "attempt"
            prompt = activity["item"]["prompt"]
            answer = "bag /æ/ 不同，cake name /eɪ/" if "cake" in prompt else "sit /ɪ/ 不同，bike five /aɪ/" if "bike" in prompt else "hot /ɒ/ 不同，home nose /əʊ/"
            candidate = ok(client.post(f"/v1/sessions/{session}/agent-turn", json={
                "transcript": answer, "client_action_id": f"voice-check-{i:02d}"}))
            # Free-form answers stay candidates until their role is explicit.
            # Exercise the real clarification and confirmation, not a fake score.
            assert candidate['action']['source'] == 'pending_answer_clarification'
            ok(client.post(f"/v1/agent-turns/{candidate['agent_turn_id']}/execute"))
            say(client, session, '刚才那句就是我的答案', f'voice-check-confirm-{i:02d}')
        final = focus(client, session)
        assert final["phase"] == "scheduled"
        assert final["state"]["next_activity"]["stage"] == "near_transfer"
        assert final["state"]["next_activity"]["kind"] in {"blocked", "preparing_variant", "awaiting_parent_review"}
        assert client.app.state.mistake_book.get(case_id, "child-primary-4")["mastery_gate"]["satisfied"] == {}
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM mistake_case").fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM mistake_attempt").fetchone()[0] == 3
            assert c.execute("SELECT count(*) FROM mistake_diagnosis").fetchone()[0] == 1
            assert c.execute("SELECT count(*) FROM mistake_case WHERE child_id='child-junior-3'").fetchone()[0] == 0
        from home_study_agent.child_model import ChildModelService
        model = ChildModelService(db).snapshot("child-primary-4")
        assert case["knowledge_node"]["knowledge_node_id"] in json.dumps(model, ensure_ascii=False)
    with TestClient(create_app(settings(tmp_path))) as client:
        assert focus(client, session)["state"]["next_activity"]["stage"] == "near_transfer"
        say(client, session, "继续错题", "voice-too-early")
        assert focus(client, session)["phase"] == "scheduled"
        remembered = say(client, session, "你记得刚才我卡在哪道错题吗", "voice-memory")
        assert "grape" in remembered["turn"]["response_text"]
        assert "B" in remembered["turn"]["response_text"]
        # A legacy early due timestamp must not certify reference-only answers
        # or skip the still-missing near-transfer gate after session recovery.
        with db.connect() as c:
            c.execute("UPDATE mistake_mastery_gate SET delayed_48h_due_at='2020-01-01T00:00:00+00:00' WHERE mistake_id=?", (case_id,))
            c.execute("UPDATE daily_session SET status='completed', state='completed' WHERE session_id=?", (session,))
            c.commit()
        next_session = ok(client.post('/v1/sessions', json={"child_id": "child-primary-4", "client_action_id": "next-day-session"}))["session_id"]
        ok(client.post(f'/v1/sessions/{next_session}/transition', json={"expected_state": "check_in", "target_state": "classroom_recall", "reason": "test", "client_action_id": "next-recall"}))
        ok(client.post(f'/v1/sessions/{next_session}/recalls/none', json={"client_action_id": "next-no-recall"}))
        ok(client.post(f'/v1/sessions/{next_session}/transition', json={"expected_state": "classroom_recall", "target_state": "homework_capture", "reason": "test", "client_action_id": "next-homework"}))
        ok(client.post(f'/v1/sessions/{next_session}/homework', json={"subject": "英语", "title": "复习今天的单词", "expected_minutes": 10, "client_action_id": "next-assignment"}))
        plan = ok(client.post(f'/v1/sessions/{next_session}/plan/generate', json={"client_action_id": "next-plan"}))
        assert any(item["source_type"] == "mistake_review" and item["source_ref"] == case_id for item in plan["plan_items"])
        ok(client.post(f'/v1/sessions/{next_session}/transition', json={"expected_state": "plan_review", "target_state": "study", "reason": "test", "client_action_id": "next-study"}))
        turn = ok(client.post(f'/v1/sessions/{next_session}/agent-turn', json={"transcript": "打开错题本", "client_action_id": "next-open"}))
        ok(client.post(f"/v1/agent-turns/{turn['agent_turn_id']}/execute"))
        resumed = focus(client, next_session)
        assert resumed["mistake_id"] == case_id
        assert resumed["state"]["next_activity"]["stage"] == "near_transfer"
        assert resumed["phase"] == "scheduled"
        exported = client.app.state.privacy_lifecycle.export_child("child-primary-4")
        payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        assert payload["mistake_dialogues"]
        assert payload["mistake_dialogue_events"]


@pytest.mark.parametrize("spoken,answer", [
    ("我当时写了72", "72"),
    ("我原来选了B", "B"),
    ("我写了-3/4", "-3/4"),
    ("我当时写了x = -2", "x = -2"),
    ("我写的是了不起", "了不起"),
])
def test_original_answer_framing_readback_and_durable_confirmation(tmp_path, spoken, answer):
    with TestClient(create_app(settings(tmp_path))) as client:
        session, db = fixture(client)
        begin(client, session)
        say(client, session, "对", "confirm-stem")
        heard = say(client, session, spoken, "original-with-framing")
        current = focus(client, session)
        assert current["phase"] == "confirm"
        assert current["state"]["submitted_answer"] == answer
        assert f"原答案是“{answer}”" in heard["turn"]["response_text"]
        with db.connect() as c:
            assert c.execute("SELECT transcript FROM agent_turn WHERE agent_turn_id=?",
                             (heard["turn"]["agent_turn_id"],)).fetchone()[0] == spoken
            assert c.execute("SELECT count(*) FROM mistake_case").fetchone()[0] == 0
    with TestClient(create_app(settings(tmp_path))) as client:
        assert focus(client, session)["state"]["submitted_answer"] == answer
        saved = say(client, session, "是的", "confirm-original")
        mistake_id = saved["execution"]["result"]["mistake_id"]
        with db.connect() as c:
            assert c.execute("SELECT submitted_answer FROM mistake_case WHERE mistake_id=?",
                             (mistake_id,)).fetchone()[0] == answer
        assert say(client, session, "是的", "confirm-original")["execution"]["result"]["mistake_id"] == mistake_id


@pytest.mark.parametrize("pause,blocks", [("等一下", True), ("暂停", True), ("以后再做", False)])
def test_saved_paper_deferral_releases_navigation_but_preserves_resume(tmp_path, pause, blocks):
    from home_study_agent.task_start_binding import TaskStartBinding
    with TestClient(create_app(settings(tmp_path))) as client:
        session, db = fixture(client)
        begin(client, session)
        say(client, session, "对", "paper-stem")
        say(client, session, "我选了B", "paper-original")
        say(client, session, "是的", "save-original")
        original_phase = focus(client, session)["phase"]
        mistake_id = focus(client, session)["mistake_id"]
        dialogue = MistakeDialogueService(db)
        dialogue.respond(session, pause, "pause-paper")
        dialogue.respond(session, pause, "pause-paper-again")
        assert focus(client, session)["state"]["resume_phase"] == original_phase
    with TestClient(create_app(settings(tmp_path))) as client:
        with db.connect() as c:
            assert bool(TaskStartBinding.interaction_focus(c, session, "child-primary-4")) is blocks
        say(client, session, "继续错题", "resume-paper")
        restored = focus(client, session)
        assert restored["phase"] == original_phase
        assert restored["mistake_id"] == mistake_id
        assert restored["state"]["submitted_answer"] == "B"
        assert not restored["state"].get("deferred_for_other_tasks")
        with db.connect() as c:
            assert TaskStartBinding.interaction_focus(c, session, "child-primary-4")
            assert c.execute("SELECT count(*) FROM mistake_case").fetchone()[0] == 1


@pytest.mark.parametrize("guard", ["unsaved", "pending_operation", "foundation_origin", "invalid_state"])
def test_deferral_never_releases_unresolved_work(tmp_path, guard):
    from home_study_agent.task_start_binding import TaskStartBinding
    with TestClient(create_app(settings(tmp_path))) as client:
        session, db = fixture(client)
        begin(client, session)
        say(client, session, "对", "paper-stem")
        say(client, session, "我选B", "paper-original")
        if guard != "unsaved":
            say(client, session, "是的", "save-original")
        say(client, session, "以后再做", "defer-paper")
        state = focus(client, session)["state"]
        if guard in {"pending_operation", "foundation_origin"}:
            state[guard] = {"action_id": "unresolved-test-work"}
        with db.connect() as c:
            c.execute("UPDATE mistake_dialogue SET state_json=? WHERE session_id=?",
                      ("{" if guard == "invalid_state" else json.dumps(state), session))
            c.commit()
            assert TaskStartBinding.interaction_focus(c, session, "child-primary-4")


def test_skip_correction_isolation_and_conflicting_replay(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as client:
        session, db = fixture(client)
        begin(client, session)
        say(client, session, "题目是比较字母读音，A. cake B. bag C. name", "voice-edit-question")
        say(client, session, "我当时写的是A", "voice-edit-original")
        assert focus(client, session)["state"]["submitted_answer"] == "A"
        say(client, session, "这是对勾", "voice-exclude-tick")
        assert focus(client, session)["phase"] == "confirm"
        with db.connect() as c:
            assert c.execute("SELECT count(*) FROM mistake_case").fetchone()[0] == 0
        say(client, session, "跳过", "voice-skip-after-claim")
        assert focus(client, session)["phase"] == "closed"
        with db.connect() as c:
            c.execute("UPDATE daily_session SET status='completed', state='completed' WHERE session_id=?", (session,))
            c.commit()
        other, _ = fixture(client, "other-photo", "child-junior-3")
        assert client.post(f"/v1/sessions/{other}/mistake-dialogue", json={"artifact_id": "voice-photo", "client_action_id": "cross-child-begin"}).status_code == 404
        with pytest.raises(InvalidMistakeCaptureCandidateError):
            MistakeDialogueService(db)._replay(other, "begin-voice-photo", "different")


def test_mixed_chinese_phonics_instruction_detects_english():
    assert subject_for_question("选出下列单词中划线部分读音不同的一项 grape make like") == "英语"


@pytest.mark.parametrize("text", ["拍错题", "帮我拍一下错题", "用高拍仪帮我订正", "帮我看这道错题"])
def test_mistake_photo_utterance_requests_a_real_capture(text):
    from home_study_agent.agent_orchestrator import is_deictic_visual_question_request
    assert is_deictic_visual_question_request(text)
