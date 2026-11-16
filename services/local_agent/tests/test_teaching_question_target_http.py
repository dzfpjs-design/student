"""Real host integration gates. Only the model provider is an offline recorder."""

import json

from home_study_agent.database import Database
from home_study_agent.model_gateway import ModelResult
from home_study_agent.teaching_question_target import resolve_question_target
from test_app import client_for


CHILD = "child-primary-4"
ITEM = "diag-p4-arithmetic-closed-1"
SECOND = "diag-p4-arithmetic-closed-2"


class OfflineTutor:
    configured = True

    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ModelResult(content="先找题目中的运算符号，再复述运算顺序。", provider="offline-test", model="offline", latency_ms=1)


def prepare(client, sid, item, key):
    response = client.post(f"/v1/sessions/{sid}/diagnostic-prompt/prepare", json={
        "preferred_item_id": item, "client_action_id": key})
    assert response.status_code == 200, response.text
    return response.json()["prompt"]


def setup_question_target_http(client):
    """Reusable main-thread fixture: real HTTP prompts and verified local scoring."""
    started = client.post("/v1/sessions", json={"child_id": CHILD, "client_action_id": "target-http-session"})
    assert started.status_code == 201, started.text
    sid = started.json()["session_id"]
    first = prepare(client, sid, ITEM, "target-http-baseline")
    scored = client.post(f"/v1/sessions/{sid}/diagnostic-prompt/answer", json={
        "prompt_id": first["prompt_id"], "item_version": first["item_version"],
        "answer": "13", "client_action_id": "target-http-score"})
    assert scored.status_code == 200, scored.text
    assert scored.json()["attempt"]["score_status"] == "verified_local"
    return sid, prepare(client, sid, SECOND, "target-http-current")


def tutor_payload(prompt, key, **overrides):
    return {"subject": "数学", "question": prompt["item"]["prompt"], "hint_level": 1,
            "prompt_id": prompt["prompt_id"], "item_version": prompt["item_version"],
            "client_action_id": key, **overrides}


def test_http_prompt_persistence_is_sufficient_for_readonly_target_resolution(tmp_path):
    gateway = OfflineTutor()
    with client_for(tmp_path, model_gateway=gateway) as client:
        sid, prompt = setup_question_target_http(client)
        db = Database(tmp_path / "test.db")
        with db.connect() as c:
            before = "\n".join(c.iterdump())
            result = resolve_question_target(c, sid, subject="数学", question=prompt["item"]["prompt"],
                prompt_id=prompt["prompt_id"], item_version=prompt["item_version"])
            assert result["status"] == "resolved"
            assert "\n".join(c.iterdump()) == before
        assert gateway.requests == []


def test_http_bound_tutor_parent_restart_and_source_revocation(tmp_path):
    gateway = OfflineTutor()
    with client_for(tmp_path, model_gateway=gateway) as client:
        sid, prompt = setup_question_target_http(client)
        payload = tutor_payload(prompt, "target-http-tutor")
        response = client.post(f"/v1/sessions/{sid}/tutor", json=payload)
        assert response.status_code == 200, response.text
        saved = response.json()
        audit = saved["output_envelope"]["local_teaching_control"]
        assert (audit.get("question_target") or {}).get("status") == "resolved", "Host must pass the frozen question target to tutor_controls"
        assert audit["controls"]["explanation_pacing"] == "short_step_then_restate"
        messages = json.dumps(gateway.requests[-1].messages, ensure_ascii=False)
        for private in (prompt["prompt_id"], prompt["item_version"], audit["question_target"]["fingerprint"],
                        "question_target", "evidence_refs", "node-p4-math-arithmetic"):
            assert private not in messages
        assert client.post(f"/v1/sessions/{sid}/tutor", json=payload).json()["turn_id"] == saved["turn_id"]
        assert len(gateway.requests) == 1
    restarted = OfflineTutor()
    with client_for(tmp_path, model_gateway=restarted) as client:
        assert client.post(f"/v1/sessions/{sid}/tutor", json=payload).json()["turn_id"] == saved["turn_id"]
        assert restarted.requests == []
        followup = {"subject": "数学", "question": prompt["item"]["prompt"], "hint_level": 2,
                    "parent_turn_id": saved["turn_id"], "client_action_id": "target-http-followup"}
        response = client.post(f"/v1/sessions/{sid}/tutor", json=followup)
        assert response.status_code == 200, response.text
        target = response.json()["output_envelope"]["local_teaching_control"]["question_target"]
        assert target["binding"]["prompt_id"] == prompt["prompt_id"]
        with Database(tmp_path / "test.db").connect() as c:
            c.execute("UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id=?", (SECOND,))
            c.commit()
        rejected = client.post(f"/v1/sessions/{sid}/tutor", json={**followup, "client_action_id": "target-http-withdrawn"})
        assert rejected.status_code == 409, rejected.text
        assert len(restarted.requests) == 1


def test_http_same_question_new_prompt_cannot_reuse_old_reservation_identity(tmp_path):
    gateway = OfflineTutor()
    with client_for(tmp_path, model_gateway=gateway) as client:
        sid, original = setup_question_target_http(client)
        payload = tutor_payload(original, "target-http-bound-action")
        response = client.post(f"/v1/sessions/{sid}/tutor", json=payload)
        assert response.status_code == 200, response.text
        cancelled = client.post(f"/v1/sessions/{sid}/diagnostic-prompt/cancel", json={
            "prompt_id": original["prompt_id"], "item_version": original["item_version"]})
        assert cancelled.status_code == 200, cancelled.text
        replacement = prepare(client, sid, SECOND, "target-http-replacement")
        assert replacement["item"]["prompt"] == original["item"]["prompt"]
        assert replacement["prompt_id"] != original["prompt_id"]
        conflict = client.post(f"/v1/sessions/{sid}/tutor", json=tutor_payload(replacement, payload["client_action_id"]))
        assert conflict.status_code == 409, "Same text is not the same prompt reservation"
        assert len(gateway.requests) == 1
        second = client.post(f"/v1/sessions/{sid}/tutor", json=tutor_payload(replacement, "target-http-new-action"))
        assert second.status_code == 200, second.text
        with Database(tmp_path / "test.db").connect() as c:
            hashes = c.execute("SELECT DISTINCT request_hash FROM tutor_request_reservation").fetchall()
            assert len(hashes) == 2, "Reservation hashes must bind prompt_id and item_version"
