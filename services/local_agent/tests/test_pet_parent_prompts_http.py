"""Parent prompt discovery uses real auth and temporary SQLite, never scoring."""

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.parent_auth import ParentAuth
from home_study_agent.pet_media_evidence import PetMediaEvidenceService
from home_study_agent.pet_media_http import create_parent_pet_prompts_router
from home_study_agent.repository import StudyRepository
from test_pet_media_evidence import CHILD, change, media


def mount(media, restart=False):
    db = Database(media[0].path) if restart else media[0]
    service = PetMediaEvidenceService(db, media_root=media[4].parent)
    auth = ParentAuth(db)
    token = auth.unlock("123456") if auth.status()["configured"] else auth.setup("123456")

    def local(request: Request):
        if request.headers.get("X-HSC-Session") != "test-local":
            raise HTTPException(401)

    app = FastAPI()
    app.include_router(create_parent_pet_prompts_router(service, auth, local_session_dependency=local))
    client = TestClient(app)
    client.headers.update({"X-HSC-Session": "test-local", "X-HSC-Parent": token})
    return client


def url(child=CHILD):
    return f"/v1/parent/children/{child}/pet-prompts"


def snapshot(db):
    with db.connect() as c:
        return list(c.iterdump())


def test_parent_and_local_tokens_required_no_role_self_authorization(media):
    client = mount(media)
    assert client.get(url()).status_code == 200
    for header in ("X-HSC-Parent", "X-HSC-Session"):
        value = client.headers.pop(header)
        assert client.get(url(), params={"role": "parent"}).status_code == 401
        client.headers[header] = value
    client.headers["X-HSC-Parent"] = "forged"
    assert client.get(url()).status_code == 401


def test_exact_safe_shape_read_only_and_restart(media):
    client = mount(media)
    before = snapshot(media[0])
    result = client.get(url())
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    data = result.json()
    assert data["child_id"] == CHILD
    assert data["local_only"] is True and data["cloud_transmission_allowed"] is False
    row, = data["prompts"]
    assert row["prompt_id"] == media[3]["prompt_id"]
    assert row["item_version"] == media[3]["item_version"]
    assert row["status"] == "waiting_answer"
    assert set(row["item"]) == {"track", "prompt", "stimulus_text", "pet_audio_modality"}
    assert row['item']['track'] == 'pet'
    assert row["item"]["pet_audio_modality"] == "listening"
    assert not any(key in result.text for key in ("answer_key", "answer_json", "submitted_answer", "stimulus_asset", str(media[4])))
    assert snapshot(media[0]) == before
    restarted = mount(media, True)
    # Unlock legitimately persists auth bookkeeping; the GET itself must not.
    before = snapshot(media[0])
    assert restarted.get(url()).json() == data
    assert snapshot(media[0]) == before


@pytest.mark.parametrize("mutation", [
    "UPDATE diagnostic_item SET status='withdrawn' WHERE diagnostic_item_id='diag-pet-listening-1'",
    "UPDATE diagnostic_item SET prompt=prompt||' revised' WHERE diagnostic_item_id='diag-pet-listening-1'",
    "UPDATE daily_session SET status='completed'",
])
def test_invalidated_visible_without_mutation_or_rebinding(media, mutation):
    client = mount(media)
    original = client.get(url()).json()["prompts"][0]
    change(media[0], mutation)
    before = snapshot(media[0])
    row, = client.get(url()).json()["prompts"]
    assert row["status"] == "invalidated"
    assert row["item_version"] == original["item_version"] and row["item"] == original["item"]
    assert snapshot(media[0]) == before


def test_child_isolation_active_child_and_corrupt_session_binding(media):
    client = mount(media)
    other = "child-junior-3"
    change(media[0], "UPDATE daily_session SET status='completed' WHERE session_id=?", (media[2],))
    sid = StudyRepository(media[0]).start_session(other, "other-start")["session_id"]
    assert client.get(url(other)).json()["prompts"] == []
    assert client.get(url()).json()["prompts"][0]["child_id"] == CHILD
    assert client.get(url("unknown")).status_code == 404
    change(media[0], "UPDATE diagnostic_prompt SET origin_session_id=?", (sid,))
    assert client.get(url()).json()["prompts"] == []
    assert client.get(url(other)).json()["prompts"] == []
    change(media[0], "UPDATE child_profile SET status='archived' WHERE child_id=?", (CHILD,))
    assert client.get(url()).status_code == 404


def test_recent_100_listening_only_statuses_and_no_answer_exposure(media):
    client = mount(media)
    db = media[0]
    change(db, "UPDATE diagnostic_prompt SET status='cancelled'")
    with db.connect() as c:
        seed = dict(c.execute("SELECT * FROM diagnostic_prompt").fetchone())
        for index in range(104):
            row = {**seed, "prompt_id": f"history-{index:03d}", "prepare_action_id": f"history-{index}",
                   "created_at": f"2026-09-07T12:{index // 60:02d}:{index % 60:02d}+00:00",
                   "answer_json": '{"answer":"PRIVATE_CHILD_ANSWER","hint_level":0}'}
            c.execute(f"INSERT INTO diagnostic_prompt ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
        c.commit()
    before = snapshot(db)
    response = client.get(url())
    rows = response.json()["prompts"]
    assert len(rows) == 100
    assert [r["created_at"] for r in rows] == sorted([r["created_at"] for r in rows], reverse=True)
    assert all(r["status"] == "cancelled" for r in rows)
    assert "PRIVATE_CHILD_ANSWER" not in response.text
    assert snapshot(db) == before


def test_saved_answer_and_missing_receipt_never_exposed_or_scored(media):
    client = mount(media)
    change(media[0], "UPDATE diagnostic_prompt SET status='answer_saved',answer_json=?",
           ('{"answer":"PRIVATE","hint_level":0}',))
    before = snapshot(media[0])
    response = client.get(url())
    assert response.json()["prompts"][0]["status"] == "answer_saved"
    assert "PRIVATE" not in response.text and snapshot(media[0]) == before
    change(media[0], "UPDATE diagnostic_prompt SET status='answered'")
    before = snapshot(media[0])
    assert client.get(url()).json()["prompts"][0]["status"] == "invalidated"
    assert snapshot(media[0]) == before


def test_node_authority_not_question_keywords_and_snapshot_tamper_invalidates(media):
    client = mount(media)
    with media[0].connect() as c:
        row = c.execute("SELECT item_json FROM diagnostic_prompt").fetchone()
    saved = json.loads(row[0])
    saved["answer_type"] = "text"
    change(media[0], "UPDATE diagnostic_prompt SET item_json=?", (json.dumps(saved),))
    projected = client.get(url()).json()["prompts"][0]
    assert projected["item"]["pet_audio_modality"] == "listening"
    assert projected["status"] == "invalidated"
    change(media[0], "UPDATE knowledge_node SET code='PET.READING.EXPLICIT' WHERE knowledge_node_id=?",
           (saved["knowledge_node_id"],))
    assert client.get(url()).json()["prompts"] == []


def test_no_prompt_table_is_read_only_empty_not_automatic_preparation(media):
    client = mount(media)
    change(media[0], "DROP TABLE diagnostic_prompt")
    before = snapshot(media[0])
    assert client.get(url()).json()["prompts"] == []
    assert snapshot(media[0]) == before
