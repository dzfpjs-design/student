"""Strong HTTP and runtime integration; only the external planner is simulated."""

import json

from fastapi.testclient import TestClient
import pytest

from home_study_agent.agent_runtime import AgentRuntimeStore, CapabilityRegistry, InvalidAgentPlanError
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.model_gateway import ModelResult
from home_study_agent.pet_vocabulary import PetVocabularyService
from test_pet_vocabulary_prompt import CHILD, OTHER, change, flow, rows


class Planner:
    configured = True

    def __init__(self):
        self.requests = []
        self.before_reply = None
        self.select("pet_vocabulary.submit", "unrelated", role="answer")

    def select(self, capability, utterance, *, role="task_request", arguments=None):
        self.payload = {
            "objective": {"mode": "new", "type": "learning", "summary": "Synthetic vocabulary turn",
                          "success_criteria": ["Persist the real result"], "phase": "execute"},
            "next_step": {"capability": capability, "arguments": arguments if arguments is not None else {"answer": utterance},
                          "reason": "Synthetic test decision", "expected_evidence": []},
            "spoken_transition": "我们核对这一张。", "requires_child_reply": False, "confidence": .95,
            "utterance_binding": {"role": role, "quote": utterance},
        }

    async def complete(self, request):
        self.requests.append(request)
        if self.before_reply:
            self.before_reply()
        return ModelResult(content=json.dumps(self.payload, ensure_ascii=False), provider="test",
                           model="synthetic-planner", latency_ms=1, prompt_tokens=1, completion_tokens=1)


@pytest.fixture
def environment(flow, tmp_path, monkeypatch):
    db, _, sid = flow
    monkeypatch.setenv("HSC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HSC_DATABASE_PATH", str(db.path))
    return Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False, agent_runtime_enabled=True), db, sid


def base(sid):
    return f"/v1/sessions/{sid}/pet-vocabulary"


def next_prompt(client, sid):
    response = client.get(base(sid) + "/next")
    assert response.status_code == 200, response.text
    return response.json()["card"]


def binding(prompt):
    return {key: prompt[key] for key in ("prompt_session_id", "card_version")}


def submit(client, sid, prompt, *, answer="unrelated", key="vocabulary-http-answer"):
    return client.post(base(sid) + "/prompt/answer", json={**binding(prompt), "answer": answer, "client_action_id": key})


def turn(client, sid, transcript, key):
    response = client.post(f"/v1/sessions/{sid}/agent-turn", json={
        "transcript": transcript, "client_action_id": key, "cloud_text_allowed": True})
    assert response.status_code == 200, response.text
    return response.json()


def test_strong_http_multicard_receipts_restart_and_cancel_never_rebind(environment):
    settings, db, sid = environment
    with TestClient(create_app(settings)) as client:
        original = next_prompt(client, sid)
        response = submit(client, sid, original)
        assert response.status_code == 200, response.text
        first = response.json()
        assert first["status"] == "answered" and first["review"]["result"] == first["attempt"]
        later = first["review"]["next_card"]
        assert later and later["prompt_session_id"] != original["prompt_session_id"]
        assert first["review"]["summary"]["total_attempts"] == 1
        stored_attempts = rows(db, "pet_vocabulary_attempt")
        stored_cards = rows(db, "pet_vocabulary_card")
    with TestClient(create_app(settings)) as client:
        recovered = client.get(base(sid) + "/prompt/" + original["prompt_session_id"])
        assert recovered.status_code == 200 and recovered.json()["prompt"] == original
        replay = submit(client, sid, original)
        assert replay.status_code == 200, replay.text
        assert replay.json()["attempt"] == first["attempt"]
        assert replay.json()["review"]["next_card"] == later
        assert rows(db, "pet_vocabulary_attempt") == stored_attempts
        assert rows(db, "pet_vocabulary_card") == stored_cards
        assert submit(client, sid, original, answer="changed answer").status_code == 409
        assert submit(client, sid, later).status_code == 409
        assert client.post(base(sid) + "/prompt/cancel", json=binding(original)).status_code == 409
        cancelled = client.post(base(sid) + "/prompt/cancel", json=binding(later))
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        late = submit(client, sid, later, key="late-after-cancel")
        assert late.status_code == 200 and late.json()["attempt"] is None and late.json()["review"] is None
        assert PetVocabularyService(db).active_prompt(sid) is None
        assert rows(db, "pet_vocabulary_attempt") == stored_attempts


def test_strong_http_rejects_partial_forged_and_foreign_bindings(environment):
    settings, db, sid = environment
    with TestClient(create_app(settings)) as client:
        prompt = next_prompt(client, sid)
        for action in ("answer", "cancel"):
            for missing in ("prompt_session_id", "card_version"):
                payload = binding(prompt)
                payload.pop(missing)
                if action == "answer":
                    payload.update(answer="unrelated", client_action_id="partial-binding-answer")
                assert client.post(base(sid) + "/prompt/" + action, json=payload).status_code == 422
        assert submit(client, sid, {**prompt, "card_version": "forged"}).status_code == 409
        assert submit(client, sid, {**prompt, "prompt_session_id": "missing-prompt"}).status_code == 409
        extra = {**binding(prompt), "answer": "unrelated", "client_action_id": "extra-field-answer", "allow": True}
        assert client.post(base(sid) + "/prompt/answer", json=extra).status_code == 422
        switched = client.post(f"/v1/sessions/{sid}/switch-child", json={
            "target_child_id": OTHER, "client_action_id": "vocabulary-switch-child"})
        assert switched.status_code == 200, switched.text
        other_sid = switched.json()["session_id"]
        assert client.get(base(other_sid) + "/prompt/" + prompt["prompt_session_id"]).status_code in {404, 409}
        assert submit(client, other_sid, prompt).status_code == 409
        assert client.post(base(other_sid) + "/prompt/cancel", json=binding(prompt)).status_code == 409
        assert submit(client, sid, prompt).status_code == 409
        assert rows(db, "pet_vocabulary_attempt") == []


def test_runtime_multiturn_reconstructs_next_real_card_and_drops_finished_waiting(environment):
    settings, db, sid = environment
    gateway = Planner()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        gateway.select("pet_vocabulary.start", "背单词", arguments={})
        started = turn(client, sid, "背单词", "vocabulary-voice-start")
        assert started["action"]["type"] == "start_pet_vocabulary_review"
        first = next_prompt(client, sid)
        pending = AgentRuntimeStore(Database(db.path)).pending_child_reply(sid)
        assert pending["capability_name"] == "pet_vocabulary.start"
        assert pending["result"] == binding(first)
        assert "senses_json" not in json.dumps(pending)
        gateway.select("pet_vocabulary.submit", "unrelated", role="answer")
        heard = turn(client, sid, "unrelated", "vocabulary-voice-first")
        action = heard["action"]
        assert action["type"] == "submit_pet_vocabulary_answer"
        assert action["vocabulary_prompt_id"] == first["prompt_session_id"]
        assert action["vocabulary_card_version"] == first["card_version"]
        response = submit(client, sid, first, key="vocabulary-first-score")
        assert response.status_code == 200, response.text
        second = response.json()["review"]["next_card"]
        assert second and second["prompt_session_id"] != first["prompt_session_id"]
        first_turn = heard["agent_turn_id"]
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        pending = client.app.state.agent_runtime.pending_child_reply(sid)
        assert pending["result"] == binding(second)
        replay = turn(client, sid, "unrelated", "vocabulary-voice-first")
        assert replay["agent_turn_id"] == first_turn
        assert replay["action"]["vocabulary_prompt_id"] == first["prompt_session_id"]
        assert submit(client, sid, first, key="vocabulary-first-score").json()["review"]["next_card"] == second
        change(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        gateway.select("pet_vocabulary.submit", "another response", role="answer")
        heard = turn(client, sid, "another response", "vocabulary-voice-second")
        assert heard["action"]["vocabulary_prompt_id"] == second["prompt_session_id"]
        response = submit(client, sid, second, answer="another response", key="vocabulary-second-score")
        assert response.status_code == 200, response.text
        assert response.json()["review"]["next_card"] is None
        assert response.json()["review"]["summary"]["total_attempts"] == 2
        assert client.app.state.agent_runtime.pending_child_reply(sid) is None
        assert len(rows(db, "pet_vocabulary_attempt")) == 2
        assert all(row["status"] == "completed" for row in rows(db, "pet_vocabulary_prompt_session"))
        assert rows(db, "daily_session")[0]["state"] == "reflection"


def test_runtime_model_latency_cannot_rebind_vocabulary_answer(environment):
    settings, db, sid = environment
    gateway = Planner()
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        original = next_prompt(client, sid)
        service = client.app.state.pet_vocabulary
        changed = []
        def replace():
            service.cancel_prompt(sid, **binding(original))
            changed.append(service.present_next(sid))
        gateway.before_reply = replace
        gateway.payload["next_step"]["arguments"].update(vocabulary_prompt_id="model-invented", vocabulary_card_version="model-invented")
        heard = turn(client, sid, "unrelated", "vocabulary-latency-turn")
        action = heard["action"]
        assert action["type"] == "submit_pet_vocabulary_answer", heard
        assert action["vocabulary_prompt_id"] == original["prompt_session_id"]
        assert action["vocabulary_card_version"] == original["card_version"]
        late = submit(client, sid, original)
        assert late.status_code == 200 and late.json()["status"] == "cancelled"
        assert late.json()["review"] is None
        assert service.active_prompt(sid) == changed[0]
        assert rows(db, "pet_vocabulary_attempt") == []


@pytest.mark.parametrize("capability", ["pet_vocabulary.submit", "diagnostic.submit"])
def test_runtime_competing_typed_questions_clarify_without_scoring(environment, capability):
    settings, db, sid = environment
    gateway = Planner()
    gateway.select(capability, "unrelated", role="answer")
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        vocabulary = next_prompt(client, sid)
        diagnostic = DiagnosticPromptService(db).prepare(sid, preferred_item_id="diag-p4-arithmetic-closed-1",
                                                        client_action_id="conflicting-question")["prompt"]
        pending = AgentRuntimeStore(Database(db.path)).pending_child_reply(sid)
        assert pending["capability_name"] == "input.choose_prompt"
        assert set(pending["conflicting_owners"]) == {"diagnostic.start", "pet_vocabulary.start"}
        heard = turn(client, sid, "unrelated", "conflicting-answer-turn")
        assert heard["action"]["type"] not in {"submit_pet_vocabulary_answer", "submit_diagnostic_answer", "evaluate_child_response"}
        # Local question/answer material must not leave the device just to
        # disambiguate two owners. The conflict projection remains local.
        assert gateway.requests == []
        assert heard["action"]["local_only"] is True
        assert heard["action"]["cloud_transmission_allowed"] is False
        assert client.app.state.agent_runtime.pending_child_reply(sid)["capability_name"] == "input.choose_prompt"
        assert rows(db, "pet_vocabulary_attempt") == [] and rows(db, "diagnostic_attempt") == []
        assert client.app.state.pet_vocabulary.active_prompt(sid) == vocabulary
        DiagnosticPromptService(db).cancel(sid, prompt_id=diagnostic["prompt_id"], item_version=diagnostic["item_version"])
        assert client.app.state.agent_runtime.pending_child_reply(sid)["result"] == binding(vocabulary)


def test_vocabulary_capabilities_match_active_nonterminal_phase_contract():
    registry = CapabilityRegistry()
    for phase in ("check_in", "classroom_recall", "homework_capture", "plan_review", "study", "break", "reflection"):
        for name in ("pet_vocabulary.start", "pet_vocabulary.submit"):
            assert registry.require(name, phase).name == name
    for phase in ("evolving", "completed"):
        for name in ("pet_vocabulary.start", "pet_vocabulary.submit"):
            with pytest.raises(InvalidAgentPlanError):
                registry.require(name, phase)


def test_offline_runtime_competing_questions_do_not_default_to_vocabulary(environment):
    settings, db, sid = environment
    gateway = Planner()
    gateway.configured = False
    with TestClient(create_app(settings, model_gateway=gateway)) as client:
        vocabulary = next_prompt(client, sid)
        diagnostic = DiagnosticPromptService(db).prepare(sid, preferred_item_id="diag-p4-arithmetic-closed-1",
                                                        client_action_id="offline-conflicting-question")["prompt"]
        pending = client.app.state.agent_runtime.pending_child_reply(sid)
        assert pending["capability_name"] == "input.choose_prompt"
        response = client.post(f"/v1/sessions/{sid}/agent-turn", json={
            "transcript": "不知道", "client_action_id": "offline-conflict-turn", "cloud_text_allowed": False})
        assert response.status_code == 200, response.text
        assert response.json()["action"]["type"] not in {"submit_pet_vocabulary_answer", "submit_diagnostic_answer", "evaluate_child_response"}
        assert not gateway.requests
        assert rows(db, "pet_vocabulary_attempt") == [] and rows(db, "diagnostic_attempt") == []
        assert client.app.state.pet_vocabulary.active_prompt(sid) == vocabulary
        assert DiagnosticPromptService(db).read(sid)["prompt"] == diagnostic
