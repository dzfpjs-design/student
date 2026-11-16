"""A spoken correction replaces score authority without erasing history."""
import json
from dataclasses import replace as replace_settings

from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.learning_obligations import (
    project_child_mastery_evidence,
    withdrawn_evidence_ids,
)
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.app import create_app
from test_mistake_pending_answer_http import execute, hear
from test_mistake_dialogue_diagnosis import local_variant, opened, speak
from test_mistake_gate_reliability import CHILD, flow
from test_mistake_voice_journey import ok, settings
from test_cloud_first_agent import Planner


def test_scored_diagnosis_answer_correction_withdraws_old_score_and_survives_restart(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "scored-probe-reason")
    speak(dialogue, sid, "41", "scored-probe-old-answer")
    with db.connect() as c:
        old = dict(c.execute("SELECT * FROM diagnostic_attempt").fetchone())

    restarted = MistakeDialogueService(Database(db.path))
    changed = speak(restarted, sid, "刚才的答案改成42", "scored-probe-correction")
    assert "更正" in changed["response_text"]

    with db.connect() as c:
        attempts = [dict(row) for row in c.execute(
            "SELECT * FROM diagnostic_attempt ORDER BY rowid")]
        assert [row["submitted_answer"] for row in attempts] == ["41", "42"]
        assert old["evidence_id"] in withdrawn_evidence_ids(c, CHILD)
        projection = project_child_mastery_evidence(c, CHILD)
        effective = {row["evidence_id"] for value in projection.values()
                     for row in value["effective_evidence"]}
        assert old["evidence_id"] not in effective
        assert attempts[-1]["evidence_id"] in effective
        receipt = dict(c.execute(
            "SELECT * FROM learning_evidence_withdrawal WHERE evidence_id=?",
            (old["evidence_id"],)).fetchone())
        assert receipt["replacement_evidence_id"] == attempts[-1]["evidence_id"]
        assert receipt["reason"] == "answer_corrected"

    state = book.diagnosis_state(case, CHILD)
    probe = state["probes"][0]
    assert probe["answer"]["answer"] == "42"
    assert [item["evidence_id"] for item in probe["evidence"]] == [attempts[-1]["evidence_id"]]
    original = next(event for event in state["history"]
                    if event["kind"] == "answer" and event["payload"]["answer"] == "41")
    assert original["active"] is False


def test_scored_mastery_answer_correction_replays_gate_and_keeps_history(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "mastery-correction-reason")
    speak(dialogue, sid, "42", "mastery-correction-probe")
    _, expected = local_variant(db, case)
    ready = speak(dialogue, sid, "明白了", "mastery-correction-ready")
    assert ready["phase"] == "attempt"
    speak(dialogue, sid, "999", "mastery-old-wrong")

    with db.connect() as c:
        old = dict(c.execute("SELECT * FROM mistake_attempt").fetchone())
        before = dict(c.execute(
            "SELECT * FROM mastery_state WHERE child_id=? AND knowledge_node_id=(SELECT knowledge_node_id FROM mistake_case WHERE mistake_id=?)",
            (CHILD, case)).fetchone())

    restarted = MistakeDialogueService(Database(db.path))
    changed = speak(restarted, sid, f"刚才的答案改成{expected}", "mastery-answer-correction")
    assert "更正" in changed["response_text"]

    with db.connect() as c:
        attempts = [dict(row) for row in c.execute(
            "SELECT * FROM mistake_attempt ORDER BY rowid")]
        assert [row["submitted_answer"] for row in attempts] == ["999", str(expected)]
        withdrawn = withdrawn_evidence_ids(c, CHILD)
        assert old["evidence_id"] in withdrawn
        assert attempts[-1]["evidence_id"] not in withdrawn
        node = c.execute("SELECT knowledge_node_id FROM mistake_case WHERE mistake_id=?", (case,)).fetchone()[0]
        projection = project_child_mastery_evidence(c, CHILD)[node]
        effective = {row["evidence_id"] for row in projection["effective_evidence"]}
        assert old["evidence_id"] not in effective
        assert attempts[-1]["evidence_id"] in effective
        after = dict(c.execute(
            "SELECT * FROM mastery_state WHERE child_id=? AND knowledge_node_id=?",
            (CHILD, node)).fetchone())
        assert after["mastery_probability"] > before["mastery_probability"]
        assert c.execute("SELECT COUNT(*) FROM learning_evidence_withdrawal").fetchone()[0] == 1
        audit = json.loads(c.execute(
            "SELECT summary_json FROM audit_event WHERE action='learning_answer_corrected' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0])
        assert audit["old_evidence_id"] == old["evidence_id"]
        assert audit["replacement_evidence_id"] == attempts[-1]["evidence_id"]


def test_correct_to_wrong_correction_rolls_back_gate_instead_of_preserving_pass(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "rollback-correction-reason")
    speak(dialogue, sid, "42", "rollback-correction-probe")
    _, expected = local_variant(db, case)
    ready = speak(dialogue, sid, "明白了", "rollback-correction-ready")
    assert ready['phase'] == 'attempt'
    speak(dialogue, sid, str(expected), "rollback-old-pass")
    with db.connect() as c:
        old = dict(c.execute('SELECT * FROM mistake_attempt').fetchone())
    changed = speak(dialogue, sid, "刚才答案改成999", "rollback-to-wrong")
    assert '更正' in changed['response_text']
    gate = book.get(case, CHILD)['mastery_gate']
    assert gate['missing'][0] == old['stage']
    assert book.get(case, CHILD)['status'] != 'mastered'
    with db.connect() as c:
        assert old['evidence_id'] in withdrawn_evidence_ids(c, CHILD)
        assert c.execute('''SELECT independent FROM mistake_attempt
            WHERE client_action_id='rollback-to-wrong:attempt' ''').fetchone()[0] == 0


def test_scored_answer_correction_is_idempotent_and_child_scoped(flow):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "scoped-correction-reason")
    speak(dialogue, sid, "41", "scoped-old-answer")
    first = speak(dialogue, sid, "刚才答案改成42", "scoped-correction")
    assert speak(dialogue, sid, "刚才答案改成42", "scoped-correction") == first
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM learning_evidence_withdrawal").fetchone()[0] == 1


def test_committed_correction_resumes_after_reply_save_failure(flow, monkeypatch):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "durable-correction-reason")
    speak(dialogue, sid, "41", "durable-old-answer")
    original_save = MistakeDialogueService._save

    def fail_once(self, *args, **kwargs):
        raise RuntimeError('synthetic reply save failure')

    monkeypatch.setattr(MistakeDialogueService, '_save', fail_once)
    failed = speak(dialogue, sid, "刚才答案改成42", "durable-correction")
    assert "重试" in failed["response_text"]
    monkeypatch.setattr(MistakeDialogueService, '_save', original_save)

    restarted = MistakeDialogueService(Database(db.path))
    resumed = speak(restarted, sid, "重试", "durable-correction-retry")
    assert "pending_operation" not in resumed["state"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM learning_evidence_withdrawal").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='learning_answer_corrected'").fetchone()[0] == 1


def test_agent_runtime_routes_explicit_scored_correction_without_buttons(flow, tmp_path):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "agent-correction-reason")
    speak(dialogue, sid, "41", "agent-old-answer")
    configured = replace_settings(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured)) as client:
        planned = hear(client, sid, "刚才的答案改成42", "agent-scored-correction")
        assert planned['action']['type'] == 'continue_mistake_dialogue'
        assert planned['action']['scored_answer_control'] == 'replace'
        result = execute(client, planned)
        assert '更正' in result['execution']['result']['response_text']
    with db.connect() as c:
        assert [row[0] for row in c.execute(
            'SELECT submitted_answer FROM diagnostic_attempt ORDER BY rowid')] == ['41', '42']


def test_cloud_understands_natural_scored_correction_without_receiving_old_answer(flow, tmp_path):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "natural-correction-reason")
    speak(dialogue, sid, "41", "natural-old-answer")
    text = "我刚才说错了，应该是42"
    gateway = Planner('mistake.dialogue', {
        'utterance': text, 'scored_answer_control': 'replace',
        'replacement_answer': '42'}, text, role='correction')
    configured = replace_settings(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured, model_gateway=gateway)) as client:
        planned = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'natural-scored-correction',
            'cloud_text_allowed': True}))
        assert planned['action']['source'] == 'agent_runtime_planner'
        assert planned['action']['scored_answer_control'] == 'replace'
        result = execute(client, planned)
        assert '更正' in result['execution']['result']['response_text']
    planner_requests = [request for request in gateway.requests if request.request_type == 'agent_plan']
    assert len(planner_requests) == 1
    transmitted = json.dumps(planner_requests[0].messages, ensure_ascii=False)
    assert '41' not in transmitted
    assert 'scored-probe' not in transmitted


def test_cloud_scored_correction_rejects_changed_source_before_write(flow, tmp_path):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "changed-source-reason")
    speak(dialogue, sid, "41", "changed-source-old-answer")
    text = "我刚才说错了，应该是42"
    gateway = Planner('mistake.dialogue', {
        'utterance': text, 'scored_answer_control': 'replace',
        'replacement_answer': '42'}, text, role='correction')
    configured = replace_settings(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured, model_gateway=gateway)) as client:
        planned = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'changed-source-correction',
            'cloud_text_allowed': True}))
        with db.connect() as c:
            item = dialogue.state(sid)['state']['last_scored_answer']['diagnostic_item_id']
            c.execute("UPDATE diagnostic_item SET prompt=prompt||' changed' WHERE diagnostic_item_id=?", (item,))
            c.commit()
        response = client.post(f"/v1/agent-turns/{planned['agent_turn_id']}/execute")
        assert response.status_code in {200, 409, 422}, response.text
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM learning_evidence_withdrawal').fetchone()[0] == 0


def test_cloud_cannot_turn_reported_or_negated_words_into_scored_correction(flow, tmp_path):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "unsafe-correction-reason")
    speak(dialogue, sid, "41", "unsafe-old-answer")
    text = "老师说答案应该是42，但先别改"
    gateway = Planner('mistake.dialogue', {
        'utterance': text, 'scored_answer_control': 'replace',
        'replacement_answer': '42'}, text, role='correction')
    configured = replace_settings(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured, model_gateway=gateway)) as client:
        planned = ok(client.post(f'/v1/sessions/{sid}/agent-turn', json={
            'transcript': text, 'client_action_id': 'unsafe-scored-correction',
            'cloud_text_allowed': True}))
        assert planned['action']['type'] == 'none'
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM learning_evidence_withdrawal').fetchone()[0] == 0


def test_scored_diagnosis_answer_can_be_withdrawn_without_replacement(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "withdraw-scored-reason")
    speak(dialogue, sid, "41", "withdraw-scored-old-answer")
    with db.connect() as c:
        old = dict(c.execute("SELECT * FROM diagnostic_attempt").fetchone())

    withdrawn = speak(dialogue, sid, "撤回刚才已评分的答案", "withdraw-scored-answer")
    assert "撤回" in withdrawn["response_text"]
    assert "last_scored_answer" not in withdrawn["state"]

    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
        receipt = dict(c.execute(
            "SELECT * FROM learning_evidence_withdrawal WHERE evidence_id=?",
            (old["evidence_id"],)).fetchone())
        assert receipt["reason"] == "answer_withdrawn"
        assert receipt["replacement_evidence_id"] is None
        projection = project_child_mastery_evidence(c, CHILD)
        effective = {row["evidence_id"] for value in projection.values()
                     for row in value["effective_evidence"]}
        assert old["evidence_id"] not in effective
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='learning_answer_withdrawn'").fetchone()[0] == 1
    diagnosis = book.diagnosis_state(case, CHILD)
    assert diagnosis["probes"][0]["answer"] is None

    replay = speak(dialogue, sid, "撤回刚才已评分的答案", "withdraw-scored-answer")
    assert replay == withdrawn
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_evidence_withdrawal").fetchone()[0] == 1


def test_scored_mastery_answer_withdrawal_rolls_back_mastery_gate(flow):
    db, _, _, _ = flow
    dialogue, book, case, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "withdraw-mastery-reason")
    speak(dialogue, sid, "42", "withdraw-mastery-probe")
    _, expected = local_variant(db, case)
    assert speak(dialogue, sid, "明白了", "withdraw-mastery-ready")["phase"] == "attempt"
    speak(dialogue, sid, str(expected), "withdraw-mastery-old-pass")
    stage = dialogue.state(sid)["state"]["last_scored_answer"]["stage"]
    before = book.get(case, CHILD)["mastery_gate"]
    assert before["satisfied"][stage] >= 1

    result = speak(dialogue, sid, "刚才的答案不算", "withdraw-mastery-score")
    assert "撤回" in result["response_text"]
    gate = book.get(case, CHILD)["mastery_gate"]
    assert gate["satisfied"].get(stage, 0) == 0
    assert gate["missing"][0] == stage
    assert book.get(case, CHILD)["status"] != "mastered"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM mistake_attempt").fetchone()[0] == 1
        receipt = c.execute("SELECT reason,replacement_evidence_id FROM learning_evidence_withdrawal").fetchone()
        assert tuple(receipt) == ("answer_withdrawn", None)


def test_agent_runtime_routes_explicit_scored_withdrawal_without_buttons(flow, tmp_path):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "agent-withdraw-reason")
    speak(dialogue, sid, "41", "agent-withdraw-answer")
    configured = replace_settings(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured)) as client:
        planned = hear(client, sid, "撤回刚才已评分的答案", "agent-scored-withdraw")
        assert planned["action"]["type"] == "continue_mistake_dialogue"
        assert planned["action"]["scored_answer_control"] == "withdraw"
        result = execute(client, planned)
        assert "撤回" in result["execution"]["result"]["response_text"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
        assert c.execute("SELECT reason FROM learning_evidence_withdrawal").fetchone()[0] == "answer_withdrawn"


def test_cloud_understands_natural_scored_withdrawal_without_receiving_answer(flow, tmp_path):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "natural-withdraw-reason")
    speak(dialogue, sid, "41", "natural-withdraw-answer")
    text = "我刚才那份回答说得不算数，帮我撤掉"
    gateway = Planner("mistake.dialogue", {
        "utterance": text, "scored_answer_control": "withdraw"},
        text, role="task_request")
    configured = replace_settings(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured, model_gateway=gateway)) as client:
        planned = ok(client.post(f"/v1/sessions/{sid}/agent-turn", json={
            "transcript": text, "client_action_id": "natural-scored-withdraw",
            "cloud_text_allowed": True}))
        assert planned["action"]["source"] == "agent_runtime_planner"
        assert planned["action"]["scored_answer_control"] == "withdraw"
        result = execute(client, planned)
        assert "撤回" in result["execution"]["result"]["response_text"]
    planner_requests = [request for request in gateway.requests if request.request_type == "agent_plan"]
    assert len(planner_requests) == 1
    transmitted = json.dumps(planner_requests[0].messages, ensure_ascii=False)
    assert "41" not in transmitted
    assert "natural-withdraw-answer" not in transmitted


def test_cloud_cannot_turn_negated_scored_withdrawal_into_side_effect(flow, tmp_path):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "negated-withdraw-reason")
    speak(dialogue, sid, "41", "negated-withdraw-answer")
    text = "不要撤回刚才的答案"
    gateway = Planner("mistake.dialogue", {
        "utterance": text, "scored_answer_control": "withdraw"},
        text, role="task_request")
    configured = replace_settings(settings(tmp_path), database_path=db.path)
    with TestClient(create_app(configured, model_gateway=gateway)) as client:
        planned = ok(client.post(f"/v1/sessions/{sid}/agent-turn", json={
            "transcript": text, "client_action_id": "negated-scored-withdraw",
            "cloud_text_allowed": True}))
        assert planned["action"]["type"] == "none"
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_evidence_withdrawal").fetchone()[0] == 0


def test_committed_scored_withdrawal_resumes_after_reply_save_failure(flow, monkeypatch):
    db, _, _, _ = flow
    dialogue, _, _, sid = opened(flow)
    speak(dialogue, sid, "我不知道哪里错了", "durable-withdraw-reason")
    speak(dialogue, sid, "41", "durable-withdraw-answer")
    original_save = MistakeDialogueService._save

    def fail_once(self, *args, **kwargs):
        raise RuntimeError("synthetic withdrawal reply save failure")

    monkeypatch.setattr(MistakeDialogueService, "_save", fail_once)
    failed = speak(dialogue, sid, "撤回刚才已评分的答案", "durable-scored-withdraw")
    assert "重试" in failed["response_text"]
    monkeypatch.setattr(MistakeDialogueService, "_save", original_save)

    restarted = MistakeDialogueService(Database(db.path))
    resumed = speak(restarted, sid, "重试", "durable-scored-withdraw-retry")
    assert "pending_operation" not in resumed["state"]
    assert "last_scored_answer" not in resumed["state"]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM learning_evidence_withdrawal").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='learning_answer_withdrawn'").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM mistake_diagnosis_event WHERE kind='retraction'").fetchone()[0] == 1
