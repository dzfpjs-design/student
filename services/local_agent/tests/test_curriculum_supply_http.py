import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from home_study_agent.curriculum_supply import CurriculumSupplyService
from home_study_agent.curriculum_supply_http import create_curriculum_supply_router
from home_study_agent.database import Database
from home_study_agent.parent_auth import ParentAuth
from test_curriculum_supply import CHILD, prepared


BASE = f"/api/parent/children/{CHILD}/curriculum-supply"


def application(db, auth):
    app = FastAPI()
    app.include_router(create_curriculum_supply_router(CurriculumSupplyService(db), auth))
    return app


@pytest.fixture
def flow(tmp_path):
    db, _, payload = prepared(tmp_path)
    auth = ParentAuth(db)
    token = auth.setup("7391")
    client = TestClient(application(db, auth), client=("127.0.0.1", 50000), headers={"X-HSC-Parent": token})
    source = {k: v for k, v in payload.items() if k not in {"prompt", "expected_answer"}}
    body = {"action_id": "http-generate", "expected_version": 0, "expected_scope_version": 1, "source": source}
    return db, auth, client, body


def post(client, path, body, expected=200):
    response = client.post(BASE+path, json=body)
    assert response.status_code == expected, response.text[:800]
    return response.json()


def generated(client, body):
    return post(client, "/generate", body)


def approved(client, body):
    item = generated(client, body)
    cid = item["candidate_id"]
    checked = post(client, f"/{cid}/validate", {"action_id": "v", "expected_version": 1})
    assert checked["state"] == "validated"
    reviewed = post(client, f"/{cid}/review", {"action_id": "r", "expected_version": 2, "decision": "approve", "note": "核对教材、课标与四则运算范围"})
    assert reviewed["state"] == "reviewed"
    return cid


def test_actual_http_review_journey_restart_auth_and_withdrawal(flow):
    db, auth, client, body = flow
    with db.connect() as c:
        scope = json.loads(c.execute("SELECT scope_json FROM curriculum_supply_selection").fetchone()[0])
    selected = post(client, "/select", {"scope": scope, "action_id": "select", "expected_version": 1})
    assert selected["version"] == 2
    body["expected_scope_version"] = 2
    cid = approved(client, body)
    publish = {"action_id": "p", "expected_version": 3}
    published = post(client, f"/{cid}/publish", publish)
    assert published["eligible"] is True
    assert post(client, f"/{cid}/publish", publish)["diagnostic_item_id"] == published["diagnostic_item_id"]
    assert client.get(BASE+"/audit").json()["eligible_curriculum_items"] == 1
    reopened = Database(db.path)
    new_auth = ParentAuth(reopened)
    resumed = TestClient(application(reopened, new_auth), client=("127.0.0.1", 50001))
    assert resumed.get(BASE+f"/{cid}", headers=dict(client.headers)).status_code == 401
    resumed.headers["X-HSC-Parent"] = new_auth.unlock("7391")
    receipt = resumed.get(BASE+f"/{cid}")
    assert receipt.status_code == 200 and receipt.json()["eligible"] is True
    assert receipt.headers["Cache-Control"] == "no-store"
    packet = resumed.get(BASE+f"/{cid}/export")
    assert packet.status_code == 200
    assert packet.json()["cloud_transmission_allowed"] is False
    assert packet.json()["local_only"] is True
    assert "attachment" in packet.headers["Content-Disposition"]
    assert "local_root_path" not in packet.text
    result = post(resumed, f"/{cid}/withdraw", {"action_id": "w", "expected_version": 4, "note": "撤回此题"})
    assert result["eligible"] is False and result["state"] == "withdrawn"
    assert {event["actor"] for event in result["events"]} == {
        "local:reviewed_source_extractor_v1", "local:arithmetic_expression_v1", "parent"}


@pytest.mark.parametrize("method,path", [("get", "/audit"), ("get", "/missing"), ("get", "/missing/export"),
    ("post", "/select"), ("post", "/generate"), ("post", "/missing/validate"),
    ("post", "/missing/review"), ("post", "/missing/publish"), ("post", "/missing/withdraw")])
def test_all_endpoints_require_actual_parent_auth(flow, method, path):
    db, _, _, _ = flow
    client = TestClient(application(db, ParentAuth(db)))
    response = client.request(method, BASE+path, **({"json": {}} if method == "post" else {}))
    assert response.status_code == 401
    assert "source" not in response.text
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM curriculum_supply_package").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("actor", "parent"), ("author", "checker"), ("reviewer", "parent"),
    ("cloud_transmission_allowed", True), ("expected_version", True), ("expected_scope_version", "1")])
def test_strict_request_cannot_spoof_identity_or_coerce_version(flow, field, value):
    db, _, client, body = flow
    post(client, "/generate", {**body, field: value}, 422)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM curriculum_supply_package").fetchone()[0] == 0


def test_strict_nested_source_and_stale_scope(flow):
    _, _, client, body = flow
    post(client, "/generate", {**body, "source": {**body["source"], "prompt": "伪造题目"}}, 422)
    post(client, "/generate", {**body, "expected_scope_version": 0}, 422)
    post(client, "/generate", {**body, "expected_scope_version": 2}, 409)


def test_cross_child_resources_hidden_and_scope_cannot_borrow_textbook(flow):
    db, _, client, body = flow
    item = generated(client, body)
    other = BASE.replace(CHILD, "child-junior-3")
    assert client.get(other+"/"+item["candidate_id"]).status_code == 404
    assert client.post(other+f"/{item['candidate_id']}/withdraw", json={"action_id": "w", "expected_version": 1, "note": "wrong"}).status_code == 404
    with db.connect() as c:
        c.execute("UPDATE child_profile SET grade='四年级' WHERE child_id='child-junior-3'")
        scope = json.loads(c.execute("SELECT scope_json FROM curriculum_supply_selection").fetchone()[0])
        c.commit()
    assert client.post(other+"/select", json={"scope": scope, "action_id": "s", "expected_version": 0}).status_code == 404
    assert client.post(other+"/generate", json=body).status_code == 404


def test_source_invalidation_is_visible_and_cannot_publish(flow):
    db, _, client, body = flow
    cid = approved(client, body)
    with db.connect() as c:
        c.execute("UPDATE knowledge_source SET review_status='quarantined'")
        c.commit()
    blocked = post(client, f"/{cid}/publish", {"action_id": "p", "expected_version": 3})
    assert blocked["state"] == "blocked" and blocked["eligible"] is False
    assert "CURRICULUM_TEXTBOOK_UNREVIEWED" in blocked["blockers"]
    assert client.get(BASE+"/audit").json()["eligible_curriculum_items"] == 0


def test_http_concurrent_publish_and_conflicting_action(flow):
    db, auth, client, body = flow
    cid = approved(client, body)
    request = {"action_id": "p", "expected_version": 3}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(client.post, BASE+f"/{cid}/publish", json=request) for _ in range(2)]
        results = [future.result() for future in futures]
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json()["diagnostic_item_id"] == results[1].json()["diagnostic_item_id"]
    post(client, f"/{cid}/publish", {"action_id": "another", "expected_version": 3}, 409)
    post(client, "/generate", {**body, "source": {**body["source"], "explanation": "改写说明"}}, 409)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM curriculum_supply_event WHERE candidate_id=?", (cid,)).fetchone()[0] == 4


def test_expired_token_and_non_loopback_export_are_rejected(flow):
    db, auth, client, body = flow
    cid = generated(client, body)["candidate_id"]
    remote = TestClient(application(db, auth), client=("192.0.2.10", 4444), headers=dict(client.headers))
    assert remote.get(BASE+f"/{cid}/export", headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    assert remote.get(BASE+f"/{cid}").status_code == 403
    assert client.get(BASE+f"/{cid}/export?destination=cloud").status_code == 422
    token = client.headers["X-HSC-Parent"]
    auth._tokens[token] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.get(BASE+f"/{cid}").status_code == 401


def test_http_revision_and_spoofed_reviewer_are_not_shortcuts(flow):
    _, _, client, body = flow
    cid = approved(client, body)
    post(client, f"/{cid}/review", {"action_id": "spoof", "expected_version": 3,
         "decision": "approve", "note": "假身份", "actor": "second-parent"}, 422)
    post(client, "/generate", {**body, "supersedes": cid}, 422)
    revision = {**body, "action_id": "revise", "supersedes": cid, "supersedes_version": 3}
    result = post(client, "/generate", revision)
    assert result["supersedes"] == cid and result["version"] == 1 and result["state"] == "draft"
    assert client.get(BASE+f"/{cid}").json()["state"] == "withdrawn"
    assert post(client, "/generate", revision)["candidate_id"] == result["candidate_id"]


def test_http_publish_fault_rolls_back_durably_then_can_retry(flow, monkeypatch):
    db, _, client, body = flow
    cid = approved(client, body)
    from home_study_agent import specialized_supply
    real = specialized_supply.promote_candidate

    def fail(c, row):
        real(c, row)
        raise OSError("isolated publish failure")

    monkeypatch.setattr(specialized_supply, "promote_candidate", fail)
    error = post(client, f"/{cid}/publish", {"action_id": "p", "expected_version": 3}, 503)
    assert error["detail"]["code"] == "CURRICULUM_STORAGE_UNAVAILABLE"
    assert CurriculumSupplyService(Database(db.path)).get(CHILD, cid)["state"] == "reviewed"
    monkeypatch.setattr(specialized_supply, "promote_candidate", real)
    assert post(client, f"/{cid}/publish", {"action_id": "p", "expected_version": 3})["eligible"] is True
