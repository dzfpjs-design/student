"""Parent source-stage annotations survive the existing publication journal."""

import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.parent_auth import ParentAuth
from home_study_agent.stage_source_review import reviewed_stage
from test_curriculum_supply_http import flow, BASE, application, generated, post
from test_curriculum_supply import CHILD


def review_request(context, stage):
    return {"action_id": "review-stage", "expected_version": 2, "decision": "approve",
            "note": "Synthetic parent source-stage review; not child transfer evidence",
            "stage_review": {"schema": "specialized_stage_review_v1", "stage": stage,
                "binding_version": context["binding_version"], "source_quote": context["source_prompt"],
                "candidate_quote": context["candidate_prompt"],
                "same_skill_rationale": "Synthetic explicit reviewer rationale about the shared mathematical operation.",
                "changed_context_rationale": "Synthetic explicit reviewer rationale comparing the original problem conditions."}}


@pytest.mark.parametrize("stage", ["near_transfer", "far_transfer"])
def test_parent_stage_review_publish_restart_and_withdraw(flow, stage):
    db, _, client, body = flow
    cid = generated(client, body)["candidate_id"]
    checked = post(client, f"/{cid}/validate", {"action_id": "check", "expected_version": 1})
    context = checked["stage_review_context"]
    assert context["child_id"] == CHILD and context["candidate_id"] == cid
    request = review_request(context, stage)
    assert post(client, f"/{cid}/review", request)["state"] == "reviewed"
    published = post(client, f"/{cid}/publish", {"action_id": "publish", "expected_version": 3})
    reopened = Database(db.path)
    auth = ParentAuth(reopened)
    resumed = TestClient(application(reopened, auth), client=("127.0.0.1", 50001),
                         headers={"X-HSC-Parent": auth.unlock("7391")})
    assert post(resumed, f"/{cid}/review", request)["diagnostic_item_id"] == published["diagnostic_item_id"]
    with reopened.connect() as c:
        candidate = c.execute("SELECT * FROM specialized_supply_candidate WHERE candidate_id=?", (cid,)).fetchone()
        projection = reviewed_stage(c, CHILD, candidate)
        assert projection["stage"] == stage
        assert projection["qualification_scope"] == "explicit_human_source_stage_review_not_child_mastery"
        event = c.execute("SELECT result_json FROM curriculum_supply_event WHERE kind='review'").fetchone()
        assert json.loads(event[0])["stage_review"] == request["stage_review"]
        assert c.execute("SELECT count(*) FROM learning_evidence").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM curriculum_supply_event WHERE kind='review'").fetchone()[0] == 1
    post(resumed, f"/{cid}/withdraw", {"action_id": "withdraw", "expected_version": 4, "note": "Original source withdrawn"})
    with reopened.connect() as c:
        candidate = c.execute("SELECT * FROM specialized_supply_candidate WHERE candidate_id=?", (cid,)).fetchone()
        assert reviewed_stage(c, CHILD, candidate) is None


@pytest.mark.parametrize("fault", ["auth", "child", "binding", "extra"])
def test_stage_review_rejects_untrusted_or_changed_binding(flow, fault):
    db, _, client, body = flow
    cid = generated(client, body)["candidate_id"]
    checked = post(client, f"/{cid}/validate", {"action_id": "check", "expected_version": 1})
    request = review_request(checked["stage_review_context"], "far_transfer")
    path = BASE + f"/{cid}/review"
    if fault == "auth":
        client.headers.pop("X-HSC-Parent")
    elif fault == "child":
        path = path.replace(CHILD, "child-junior-3")
    elif fault == "binding":
        request["stage_review"]["binding_version"] = "changed"
    else:
        request["stage_review"]["allow_transfer"] = True
    response = client.post(path, json=request)
    if fault == "binding":
        assert response.status_code == 200 and response.json()["state"] == "blocked"
        assert "STAGE_REVIEW_SOURCE_CONFLICT" in response.json()["blockers"]
    else:
        assert response.status_code == {"auth": 401, "child": 404, "extra": 422}[fault]
    with db.connect() as c:
        candidate = c.execute("SELECT validation_json FROM specialized_supply_candidate WHERE candidate_id=?", (cid,)).fetchone()
        assert "stage_review" not in json.loads(candidate[0])
        assert not c.execute("SELECT 1 FROM specialized_supply_candidate WHERE status='promoted'").fetchone()
