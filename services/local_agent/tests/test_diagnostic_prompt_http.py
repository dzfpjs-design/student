"""Real HTTP/finalize paths on disposable databases; no family data or models."""

from datetime import datetime, timezone
import json
from unittest.mock import patch

from fastapi.testclient import TestClient
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.knowledge_graph.host_diagnostic_bridge import HostDiagnosticKnowledgeGraphBridge
from home_study_agent.knowledge_graph.service import KnowledgeGraphService


CHILD = "child-primary-4"
OTHER = "child-junior-3"
ITEM = "diag-p4-arithmetic-closed-1"
SECOND = "diag-p4-arithmetic-closed-2"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HSC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HSC_DATABASE_PATH", str(tmp_path / "http-prompts.db"))
    settings = Settings(data_dir=tmp_path, database_path=tmp_path / "http-prompts.db", maintenance_enabled=False)
    return settings, Database(settings.database_path)


@pytest.fixture
def http(environment):
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        yield client, db, start(client)


def start(client, child=CHILD, action="start-http-session"):
    response = client.post("/v1/sessions", json={"child_id": child, "client_action_id": action})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def endpoint(sid, action=None):
    return f"/v1/sessions/{sid}/diagnostic-prompt" + ("/" + action if action else "")


def prepare(client, sid, **kwargs):
    response = client.post(endpoint(sid, "prepare"), json={
        "preferred_item_id": ITEM, "client_action_id": "prepare-http-question", **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def binding(prompt):
    return {key: prompt[key] for key in ("prompt_id", "item_version")}


def answer(client, sid, prompt, **kwargs):
    return client.post(endpoint(sid, "answer"), json={**binding(prompt), "answer": "12",
        "client_action_id": "answer-http-question", **kwargs})


def execute(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def fetch(db, sql, args=()):
    with db.connect() as c:
        return [dict(row) for row in c.execute(sql, args)]


def approve_synthetic_kg_binding(db):
    """Explicit synthetic approval fixture, not a claim about shipped content."""
    graph = KnowledgeGraphService(db)
    target = fetch(db, """SELECT e.source_node_id FROM kg_edge e JOIN kg_node x ON x.node_id=e.target_node_id
        WHERE x.canonical_key=? AND e.relation_type='assessed_by' AND e.review_status='approved'""",
        ("diagnostic:" + ITEM,))[0]["source_node_id"]
    standard = graph.upsert_node(canonical_key="http-test-only-standard", node_type="knowledge_point",
        title="Synthetic HTTP fixture standard", scope={"grade": "四年级"},
        source_ref="product_evidence:http-test-only", review_status="approved")
    graph.link(target, standard, "aligned_to", rationale="Synthetic test alignment",
        source_ref="product_evidence:http-test-only", review_status="approved")
    return target


def assert_finalized(db, result, *, kg_recorded=False):
    assert result["status"] == "answered"
    attempt = result["attempt"]
    assert attempt["score_status"] == "verified_local" and attempt["correctness"] == 0
    stored = fetch(db, "SELECT * FROM diagnostic_attempt WHERE attempt_id=?", (attempt["attempt_id"],))
    assert len(stored) == 1 and stored[0]["evidence_id"] == attempt["evidence_id"]
    mistake = attempt["mistake"]
    assert mistake and mistake["child_id"] == CHILD
    saved_case = fetch(db, "SELECT * FROM mistake_case WHERE mistake_id=?", (mistake["mistake_id"],))[0]
    assert saved_case["source_evidence_id"] == attempt["evidence_id"]
    assert saved_case["diagnostic_item_id"] == ITEM
    adaptation = attempt["learning_adaptation"]
    assert adaptation["status"] == "refreshed"
    snapshot = fetch(db, "SELECT * FROM child_model_snapshot WHERE snapshot_id=?", (adaptation["snapshot_id"],))[0]
    assert snapshot["child_id"] == CHILD
    assert snapshot["evidence_fingerprint"] == adaptation["evidence_fingerprint"]
    assert attempt["evidence_id"] in snapshot["payload_json"]
    kg = attempt["knowledge_graph"]
    assert kg["status"] == ("recorded" if kg_recorded else "manual_confirmation_required")
    evidence = fetch(db, "SELECT * FROM kg_mastery_evidence WHERE idempotency_key=?",
                     ("host-diagnostic:" + attempt["attempt_id"],))
    assert len(evidence) == int(kg_recorded)
    if evidence:
        assert evidence[0]["child_id"] == CHILD and evidence[0]["origin_id"] == mistake["mistake_id"]
    return attempt["attempt_id"], mistake["mistake_id"], adaptation["snapshot_id"]


@pytest.mark.parametrize("stage", ["check_in", "homework_capture", "reflection"])
def test_http_cross_phase_scores_and_finalizes_without_changing_stage(http, stage):
    client, db, sid = http
    execute(db, "UPDATE daily_session SET state=? WHERE session_id=?", (stage, sid))
    assert client.get(endpoint(sid)).json()["status"] == "no_item"
    prompt = prepare(client, sid)["prompt"]
    assert prompt["origin_session_id"] == sid and prompt["child_id"] == CHILD
    assert "expected_answer" not in json.dumps(prompt)
    assert client.get(endpoint(sid)).json()["prompt"] == prompt
    response = answer(client, sid, prompt)
    assert response.status_code == 200, response.text
    first = assert_finalized(db, response.json())
    assert fetch(db, "SELECT state FROM daily_session WHERE session_id=?", (sid,))[0]["state"] == stage
    assert assert_finalized(db, answer(client, sid, prompt).json()) == first
    resumed = client.post(endpoint(sid, "resume"), json=binding(prompt))
    assert resumed.status_code == 200, resumed.text
    assert assert_finalized(db, resumed.json()) == first
    assert len(fetch(db, "SELECT * FROM diagnostic_attempt")) == 1
    assert len(fetch(db, "SELECT * FROM mastery_update_event")) == 1
    assert len(fetch(db, "SELECT * FROM mistake_case")) == 1


def test_http_restart_replays_real_qualified_kg_and_snapshot_without_duplicates(environment):
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        approve_synthetic_kg_binding(db)
        prompt = prepare(client, sid)["prompt"]
        assert prepare(client, sid)["prompt"] == prompt
        response = answer(client, sid, prompt)
        assert response.status_code == 200, response.text
        first = assert_finalized(db, response.json(), kg_recorded=True)
        reviews = fetch(db, "SELECT review_task_id FROM review_task ORDER BY review_task_id")
    with TestClient(create_app(settings)) as client:
        recovered = client.get(endpoint(sid))
        assert recovered.status_code == 200 and recovered.json()["prompt"] == prompt
        assert recovered.json()["attempt"]["attempt_id"] == first[0]
        replay = client.post(endpoint(sid, "resume"), json=binding(prompt))
        assert replay.status_code == 200, replay.text
        assert assert_finalized(db, replay.json(), kg_recorded=True) == first
        assert fetch(db, "SELECT review_task_id FROM review_task ORDER BY review_task_id") == reviews
        assert len(fetch(db, "SELECT * FROM diagnostic_attempt")) == 1
        assert len(fetch(db, "SELECT * FROM mistake_case")) == 1


def test_http_finalize_failure_after_score_and_mistake_recovers_on_restart(environment):
    settings, db = environment
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        sid = start(client)
        approve_synthetic_kg_binding(db)
        prompt = prepare(client, sid)["prompt"]
        with patch.object(HostDiagnosticKnowledgeGraphBridge, "record_attempt", side_effect=RuntimeError("injected finalize interruption")):
            failed = answer(client, sid, prompt)
        assert failed.status_code == 500
        attempt_id = fetch(db, "SELECT attempt_id FROM diagnostic_attempt")[0]["attempt_id"]
        assert len(fetch(db, "SELECT * FROM mistake_case")) == 1
        assert fetch(db, "SELECT * FROM kg_mastery_evidence") == []
        assert client.get(endpoint(sid)).json()["status"] == "answered"
    with TestClient(create_app(settings)) as client:
        recovered = client.post(endpoint(sid, "resume"), json=binding(prompt))
        assert recovered.status_code == 200, recovered.text
        assert assert_finalized(db, recovered.json(), kg_recorded=True)[0] == attempt_id
        assert assert_finalized(db, answer(client, sid, prompt).json(), kg_recorded=True)[0] == attempt_id
        assert len(fetch(db, "SELECT * FROM diagnostic_attempt")) == 1
        assert len(fetch(db, "SELECT * FROM mistake_case")) == 1


def test_http_saved_answer_crash_recovers_and_finalizes_across_app_restart(environment):
    settings, db = environment
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        sid = start(client)
        prompt = prepare(client, sid)["prompt"]
        with patch.object(DiagnosticPromptService, "resume", side_effect=RuntimeError("injected crash after save")):
            assert answer(client, sid, prompt).status_code == 500
        assert client.get(endpoint(sid)).json()["status"] == "answer_saved"
        assert fetch(db, "SELECT * FROM diagnostic_attempt") == []
    with TestClient(create_app(settings)) as client:
        response = client.post(endpoint(sid, "resume"), json=binding(prompt))
        assert response.status_code == 200, response.text
        assert_finalized(db, response.json())
        assert fetch(db, "SELECT state FROM daily_session WHERE session_id=?", (sid,))[0]["state"] == "check_in"


def test_http_real_profile_switch_cannot_rebind_old_question(http):
    client, db, sid = http
    prompt = prepare(client, sid)["prompt"]
    switched = client.post(f"/v1/sessions/{sid}/switch-child", json={
        "target_child_id": OTHER, "client_action_id": "switch-http-child"})
    assert switched.status_code == 200, switched.text
    other_sid = switched.json()["session_id"]
    assert other_sid != sid
    assert client.get(endpoint(other_sid)).json()["status"] == "no_item"
    for action in ("answer", "resume", "cancel"):
        payload = binding(prompt)
        if action == "answer":
            payload.update(answer="12", client_action_id="other-child-answer")
        response = client.post(endpoint(other_sid, action), json=payload)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "DIAGNOSTIC_PROMPT_PROMPT_OWNERSHIP_CONFLICT"
    assert answer(client, sid, prompt).json()["status"] == "invalidated"
    assert fetch(db, "SELECT * FROM diagnostic_attempt") == []
    assert fetch(db, "SELECT * FROM mistake_case") == []


def test_http_empty_cancel_stale_version_and_retired_question_never_finalize(http):
    client, db, sid = http
    assert prepare(client, sid, preferred_item_id="missing-question")["status"] == "no_item"
    prompt = prepare(client, sid)["prompt"]
    wrong_version = {**binding(prompt), "item_version": "stale-version"}
    for action in ("resume", "cancel"):
        assert client.post(endpoint(sid, action), json=wrong_version).status_code == 409
    assert answer(client, sid, prompt, item_version="stale-version").status_code == 409
    cancelled = client.post(endpoint(sid, "cancel"), json=binding(prompt))
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert answer(client, sid, prompt).json()["status"] == "cancelled"
    new = prepare(client, sid, client_action_id="another-prepare-http")["prompt"]
    execute(db, "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
    retired = answer(client, sid, new)
    assert retired.status_code == 200 and retired.json()["status"] == "invalidated"
    assert retired.json()["attempt"] is None
    assert fetch(db, "SELECT * FROM diagnostic_attempt") == []
    assert fetch(db, "SELECT * FROM mistake_case") == []


@pytest.mark.parametrize("field,value", [("hint_level", True), ("hint_level", 6), ("answer", " ")])
def test_http_invalid_answer_never_creates_scoring_evidence(http, field, value):
    client, db, sid = http
    prompt = prepare(client, sid)["prompt"]
    response = answer(client, sid, prompt, **{field: value})
    assert response.status_code == (409 if field == "answer" else 422), response.text
    assert client.get(endpoint(sid)).json()["status"] == "waiting_answer"
    assert fetch(db, "SELECT * FROM diagnostic_attempt") == []


def test_http_legacy_flags_cannot_bypass_study_or_supply_internal_permission(http):
    client, db, sid = http
    prompt = prepare(client, sid)["prompt"]
    response = client.post(f"/v1/sessions/{sid}/diagnostics/answer", json={
        "diagnostic_item_id": ITEM, "answer": "12", "client_action_id": "legacy-forged-flag",
        "allow": True, "trusted_prompt_id": prompt["prompt_id"], "_trusted_prompt_id": prompt["prompt_id"],
    })
    assert response.status_code in {409, 422}, response.text
    assert fetch(db, "SELECT * FROM diagnostic_attempt") == []
    response = answer(client, sid, {**prompt, "prompt_id": "forged-prompt"})
    assert response.status_code == 409
    assert fetch(db, "SELECT * FROM diagnostic_attempt") == []


@pytest.mark.parametrize("withdraw_source", [False, True])
def test_http_cancelled_parallel_review_cannot_reopen_after_mistake(http, withdraw_source):
    client, db, sid = http
    source_prompt = prepare(client, sid)["prompt"]
    source = answer(client, sid, source_prompt).json()["attempt"]
    review = fetch(db, "SELECT * FROM review_task WHERE source_evidence_id=? AND interval_label='immediate'",
                   (source["evidence_id"],))[0]
    assert review["status"] == "cancelled"
    result = prepare(client, sid, preferred_item_id=None, review_task_id=review["review_task_id"],
                     client_action_id="prepare-http-review")
    assert result["prompt"] is None
    if withdraw_source:
        response = client.post(f"/v1/mistakes/{source['mistake']['mistake_id']}/confirm", json={
            "child_id": CHILD, "decision": "exclude", "client_action_id": "withdraw-http-source"})
        assert response.status_code == 200, response.text
    assert fetch(db, "SELECT status FROM review_task WHERE review_task_id=?",
                 (review["review_task_id"],))[0]["status"] == "cancelled"
    assert len(fetch(db, "SELECT * FROM diagnostic_attempt")) == 1


def test_http_guided_finalize_persists_once_across_phases_and_restart(environment):
    settings, db = environment
    with TestClient(create_app(settings)) as client:
        sid = start(client)
        now = datetime.now(timezone.utc).isoformat()
        execute(db, """INSERT INTO daily_plan(plan_id,child_id,session_id,plan_date,status,total_minutes,
            generator,version,created_at,client_action_id)
            VALUES('http-guided-plan',?,?,'2026-09-07','active',15,'synthetic_http_test',1,?,'http-guided-plan-action')""",
            (CHILD, sid, now))
        execute(db, """INSERT INTO plan_item(plan_item_id,plan_id,subject,title,expected_minutes,priority,status,
            position,started_at,source_type,source_ref,completion_standard,cognitive_load,task_type)
            VALUES('http-guided-item','http-guided-plan','数学','Synthetic guided task',15,1,'active',1,?,
                'competition_math','competition_math.logic','Save original response','medium','oral')""", (now,))
        run = client.app.state.specialized_program.start_for_plan_item("http-guided-item", "http-guided-start")
        assert run is not None and run["completed_items"] == 0
        execute(db, "UPDATE daily_session SET state='reflection' WHERE session_id=?", (sid,))
        prompt = prepare(client, sid, preferred_item_id=None, track=run["catalog_track"])["prompt"]
        assert prompt["diagnostic_item_id"] == run["next_item_id"]
        response = answer(client, sid, prompt)
        assert response.status_code == 200, response.text
        attempt = response.json()["attempt"]
        guided = attempt["specialized_assessment"]
        assert guided["run_id"] == run["run_id"] and guided["completed_items"] == 1
        persisted = fetch(db, "SELECT * FROM specialized_assessment_run_attempt WHERE run_id=?", (run["run_id"],))
        assert len(persisted) == 1 and persisted[0]["attempt_id"] == attempt["attempt_id"]
        assert persisted[0]["diagnostic_item_id"] == prompt["diagnostic_item_id"]
        assert answer(client, sid, prompt).json()["attempt"]["specialized_assessment"]["completed_items"] == 1
        assert attempt["learning_adaptation"]["status"] == "refreshed"
    with TestClient(create_app(settings)) as client:
        resumed = client.post(endpoint(sid, "resume"), json=binding(prompt))
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["attempt"]["specialized_assessment"]["completed_items"] == 1
        assert fetch(db, "SELECT * FROM specialized_assessment_run_attempt WHERE run_id=?", (run["run_id"],)) == persisted
        assert len(fetch(db, "SELECT * FROM diagnostic_attempt")) == 1
        assert fetch(db, "SELECT state FROM daily_session WHERE session_id=?", (sid,))[0]["state"] == "reflection"
        # Replaying prepare must retain the original question even as guided next advances.
        replay = prepare(client, sid, preferred_item_id=None, track=run["catalog_track"])
        assert replay["prompt"] == prompt


def test_http_bound_original_read_and_resume_never_target_later_prompt(http):
    client, db, sid = http
    original = prepare(client, sid)["prompt"]
    scored = answer(client, sid, original)
    assert scored.status_code == 200, scored.text
    original_receipts = assert_finalized(db, scored.json())
    later = prepare(client, sid, preferred_item_id=SECOND, client_action_id="prepare-later-question")["prompt"]
    assert later["prompt_id"] != original["prompt_id"]
    assert later["item_version"] != original["item_version"]
    assert client.get(endpoint(sid)).json()["prompt"] == later

    bound = client.get(endpoint(sid), params={"prompt_id": original["prompt_id"]})
    assert bound.status_code == 200, bound.text
    assert bound.json()["prompt"] == original and bound.json()["status"] == "answered"
    assert bound.json()["attempt"]["attempt_id"] == original_receipts[0]
    before = {table: fetch(db, f"SELECT * FROM {table} ORDER BY rowid") for table in (
        "diagnostic_prompt", "diagnostic_attempt", "mistake_case", "kg_mastery_evidence", "child_model_snapshot")}

    # A version from the newer question cannot authorize recovery of the old one.
    wrong_version = client.post(endpoint(sid, "resume"), json={
        "prompt_id": original["prompt_id"], "item_version": later["item_version"]})
    assert wrong_version.status_code == 409, wrong_version.text
    assert wrong_version.json()["code"] == "DIAGNOSTIC_PROMPT_ITEM_VERSION_CONFLICT"
    for table, rows in before.items():
        assert fetch(db, f"SELECT * FROM {table} ORDER BY rowid") == rows

    recovered = client.post(endpoint(sid, "resume"), json=binding(original))
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["prompt"] == original
    assert assert_finalized(db, recovered.json()) == original_receipts
    assert assert_finalized(db, answer(client, sid, original).json()) == original_receipts
    assert client.post(endpoint(sid, "cancel"), json=binding(original)).status_code == 409
    current = client.get(endpoint(sid)).json()
    assert current["prompt"] == later and current["status"] == "waiting_answer"
    assert len(fetch(db, "SELECT * FROM diagnostic_attempt")) == 1

    assert client.post(endpoint(sid, "cancel"), json=binding(later)).json()["status"] == "cancelled"
    newest = prepare(client, sid, client_action_id="prepare-newest-question")["prompt"]
    old_cancelled = client.get(endpoint(sid), params={"prompt_id": later["prompt_id"]})
    assert old_cancelled.status_code == 200 and old_cancelled.json()["status"] == "cancelled"
    assert old_cancelled.json()["prompt"] == later
    assert client.post(endpoint(sid, "cancel"), json=binding(later)).json()["status"] == "cancelled"
    assert answer(client, sid, later, client_action_id="late-cancelled-answer").json()["status"] == "cancelled"
    assert client.get(endpoint(sid)).json()["prompt"] == newest
    assert client.get(endpoint(sid)).json()["status"] == "waiting_answer"
    assert len(fetch(db, "SELECT * FROM diagnostic_attempt")) == 1


def test_http_bound_get_rejects_foreign_session_even_for_same_child(http):
    client, db, sid = http
    original = prepare(client, sid)["prompt"]
    execute(db, "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (sid,))
    same_child_sid = start(client, action="start-same-child-again")
    assert same_child_sid != sid
    foreign = client.get(endpoint(same_child_sid), params={"prompt_id": original["prompt_id"]})
    assert foreign.status_code in {404, 409}, foreign.text
    assert "prompt" not in foreign.json()
    switched = client.post(f"/v1/sessions/{same_child_sid}/switch-child", json={
        "target_child_id": OTHER, "client_action_id": "switch-for-bound-get"})
    assert switched.status_code == 200, switched.text
    other_sid = switched.json()["session_id"]
    foreign = client.get(endpoint(other_sid), params={"prompt_id": original["prompt_id"]})
    assert foreign.status_code in {404, 409}, foreign.text
    assert "prompt" not in foreign.json()
    missing_session = client.get(endpoint("missing-session"), params={"prompt_id": original["prompt_id"]})
    assert missing_session.status_code in {404, 409}
    missing_prompt = client.get(endpoint(other_sid), params={"prompt_id": "missing-prompt"})
    assert missing_prompt.status_code in {404, 409}
    assert client.get(endpoint(other_sid)).json()["status"] == "no_item"
    assert fetch(db, "SELECT * FROM diagnostic_attempt") == []


def test_http_mutations_without_complete_binding_fail_before_touching_latest(http):
    client, db, sid = http
    prompt = prepare(client, sid)["prompt"]
    for action in ("answer", "resume", "cancel"):
        for missing in ("prompt_id", "item_version"):
            payload = binding(prompt)
            payload.pop(missing)
            if action == "answer":
                payload.update(answer="12", client_action_id="answer-missing-binding")
            response = client.post(endpoint(sid, action), json=payload)
            assert response.status_code == 422, response.text
    current = client.get(endpoint(sid), params={"prompt_id": prompt["prompt_id"]}).json()
    assert current["prompt"] == prompt and current["status"] == "waiting_answer"
    assert fetch(db, "SELECT * FROM diagnostic_attempt") == []
    assert fetch(db, "SELECT * FROM mistake_case") == []


# Blocking local operations must preserve same-loop responsiveness and durability.
import asyncio
import threading
import time
import httpx
from home_study_agent.app import DiagnosticPromptAnswerRequest
from home_study_agent.diagnostic_prompt import DiagnosticPromptError
from test_tutor_child_model_controls import RecordingGateway

def _worker_http_environment(tmp_path,dbpath=None):
 settings=Settings(data_dir=tmp_path,database_path=dbpath or tmp_path/'synthetic-business.db',maintenance_enabled=False,session_token='public-worker-test-session-token',data_encryption_key=b'0'*32)
 gateway=RecordingGateway();gateway.configured=False
 app=create_app(settings,model_gateway=gateway)
 return app,settings,app.state.repository.database,gateway


@pytest.mark.parametrize('phase',['answer','finalize'])
def test_actual_HTTP_answer_and_finalize_allow_same_eventloop_progress(tmp_path,monkeypatch,phase):
 app,settings,db,gateway=_worker_http_environment(tmp_path);original_answer=DiagnosticPromptService.answer;original_record=HostDiagnosticKnowledgeGraphBridge.record_attempt
 entered=threading.Event();released=threading.Event();facts={};phase_calls=[]
 def gate():
  phase_calls.append(phase);facts['worker_thread']=threading.get_ident()!=main_ident;entered.set();assert released.wait(3)
 def answer(self,*a,**kw):
  if phase=='answer':gate()
  return original_answer(self,*a,**kw)
 def record(self,*a,**kw):
  value=original_record(self,*a,**kw)
  if phase=='finalize':gate()
  return value
 monkeypatch.setattr(DiagnosticPromptService,'answer',answer);monkeypatch.setattr(HostDiagnosticKnowledgeGraphBridge,'record_attempt',record)
 main_ident=None
 async def run_body():
  nonlocal main_ident
  main_ident=threading.get_ident()
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',41000)),base_url='http://synthetic.local',headers={'X-HSC-Session':settings.session_token},trust_env=False)as client:
   s=await client.post('/v1/sessions',json={'child_id':'child-primary-4','client_action_id':'worker-session'});assert s.status_code==201
   sid=s.json()['session_id'];base=f'/v1/sessions/{sid}/diagnostic-prompt'
   p=await client.post(base+'/prepare',json={'preferred_item_id':'diag-p4-arithmetic-closed-1','client_action_id':'worker-prepare'});assert p.status_code==200
   prompt=p.json()['prompt']
   def release():
    entered.wait(10);time.sleep(.25);released.set()
   release_thread=threading.Thread(target=release);release_thread.start()
   start=time.monotonic();ticks=[]
   async def beat():
    await asyncio.sleep(.025);ticks.append(time.monotonic()-start)
   hb=asyncio.create_task(beat());await asyncio.sleep(0)
   try:
    r=await client.post(base+'/answer',json={**{k:prompt[k]for k in('prompt_id','item_version')},'answer':'-999','client_action_id':'worker-answer'})
   finally:
    entered.set();released.set();release_thread.join(2)
   await hb;assert not release_thread.is_alive()
   assert r.status_code==200
   facts.update(heartbeat_ms=round(ticks[0]*1000,3),HTTP_ms=round((time.monotonic()-start)*1000,3),phase=phase,status=r.status_code,original_business_once=phase_calls==[phase])
   with db.connect()as c:
    saved=c.execute('SELECT status,attempt_id FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
    attempt=c.execute('SELECT evidence_id,correctness FROM diagnostic_attempt WHERE attempt_id=?',(saved['attempt_id'],)).fetchone()
    assert saved['status']=='answered'and attempt and attempt['correctness']==0
    assert c.execute('SELECT 1 FROM learning_evidence WHERE evidence_id=?',(attempt['evidence_id'],)).fetchone()
   facts['original_attempt_evidence_persisted']=True
 async def run():
  async with app.router.lifespan_context(app):
   await run_body()
 asyncio.run(run());assert not gateway.requests
 assert facts['original_business_once']and facts['worker_thread']
 assert facts['heartbeat_ms']<200


def test_actual_HTTP_error_keeps_same_original_exception_handler_and_no_attempt(tmp_path,monkeypatch):
 app,settings,db,gateway=_worker_http_environment(tmp_path);original=DiagnosticPromptService.answer;seen=[];responses=[]
 def observe(self,*a,**kw):
  try:return original(self,*a,**kw)
  except DiagnosticPromptError as error:seen.append(error);raise
 monkeypatch.setattr(DiagnosticPromptService,'answer',observe)
 handler=app.exception_handlers[DiagnosticPromptError]
 async def observer(request,error):
  assert seen==[error]
  response=await handler(request,error);responses.append(response);return response
 app.add_exception_handler(DiagnosticPromptError,observer)
 async def run():
  async with app.router.lifespan_context(app):
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',41000)),base_url='http://synthetic.local',headers={'X-HSC-Session':settings.session_token},trust_env=False)as client:
    r=await client.post('/v1/sessions',json={'child_id':'child-primary-4','client_action_id':'exception-session'});assert r.status_code==201
    path=f"/v1/sessions/{r.json()['session_id']}/diagnostic-prompt"
    p=await client.post(path+'/prepare',json={'preferred_item_id':'diag-p4-arithmetic-closed-1','client_action_id':'exception-prepare'});assert p.status_code==200
    prompt=p.json()['prompt'];r=await client.post(path+'/answer',json={'prompt_id':prompt['prompt_id'],'item_version':'public-wrong-version','answer':'-999','client_action_id':'exception-answer'})
    assert r.status_code==409 and len(seen)==len(responses)==1
    assert responses[0].status_code==r.status_code and responses[0].body==r.content
    with db.connect()as c:assert c.execute('SELECT COUNT(*)FROM diagnostic_attempt').fetchone()[0]==0
 asyncio.run(run());assert not gateway.requests


def test_actual_answer_worker_cancel_drains_single_persisted_operation(tmp_path,monkeypatch):
 app,settings,db,gateway=_worker_http_environment(tmp_path);original=DiagnosticPromptService.answer
 entered=threading.Event();release=threading.Event();retired=threading.Event();calls=[]
 def held(self,*a,**kw):
  calls.append(True);entered.set();assert release.wait(3)
  try:return original(self,*a,**kw)
  finally:retired.set()
 monkeypatch.setattr(DiagnosticPromptService,'answer',held)
 endpoint=next(r.endpoint for r in app.routes if getattr(r,'path',None)=='/v1/sessions/{session_id}/diagnostic-prompt/answer')
 async def run():
  async with app.router.lifespan_context(app):
   async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,client=('127.0.0.1',41000)),base_url='http://synthetic.local',headers={'X-HSC-Session':settings.session_token},trust_env=False)as client:
    r=await client.post('/v1/sessions',json={'child_id':'child-primary-4','client_action_id':'cancel-session'});assert r.status_code==201
    sid=r.json()['session_id'];p=await client.post(f'/v1/sessions/{sid}/diagnostic-prompt/prepare',json={'preferred_item_id':'diag-p4-arithmetic-closed-1','client_action_id':'cancel-prepare'});assert p.status_code==200
    prompt=p.json()['prompt'];payload=DiagnosticPromptAnswerRequest(**{k:prompt[k]for k in('prompt_id','item_version')},answer='-999',client_action_id='cancel-answer')
    task=asyncio.create_task(endpoint(sid,payload))
    while not entered.is_set():await asyncio.sleep(.005)
    task.cancel();await asyncio.sleep(.025);assert not task.done()and not retired.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert retired.is_set()and calls==[True]and not db._original_connections
    with db.connect()as c:
     row=c.execute('SELECT status,attempt_id FROM diagnostic_prompt WHERE prompt_id=?',(prompt['prompt_id'],)).fetchone()
     assert row['status']=='answered'and row['attempt_id']and c.execute('SELECT COUNT(*)FROM diagnostic_attempt').fetchone()[0]==1
 asyncio.run(run());assert not gateway.requests

