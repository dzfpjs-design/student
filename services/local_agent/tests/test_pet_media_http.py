"""Real isolated FastAPI, parent authentication, SQLite and synthetic PCM."""

import hashlib
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from home_study_agent.parent_auth import ParentAuth
from home_study_agent.pet_media_evidence import PetMediaEvidenceService
from home_study_agent.pet_media_http import create_pet_media_router
from home_study_agent.database import Database
from test_pet_media_evidence import media, change, speaking, wav_file


def mounted(media, *, restarted=False):
    db, service, sid, _, path, _ = media
    if restarted:
        service = PetMediaEvidenceService(Database(db.path), media_root=path.parent, runtime_epoch="http-restart")
    auth = ParentAuth(db)
    token = auth.unlock("123456") if auth.status()["configured"] else auth.setup("123456")

    def local_session(request: Request):
        if request.headers.get("X-HSC-Session") != "local-test-token":
            raise HTTPException(401, detail={"code": "LOCAL_SESSION_REQUIRED"})

    app = FastAPI()
    app.include_router(create_pet_media_router(service, auth, local_session_dependency=local_session))
    return TestClient(app), token


@pytest.fixture
def http(media):
    client, token = mounted(media)
    client.headers["X-HSC-Session"] = "local-test-token"
    return client, token, f"/v1/sessions/{media[2]}/pet-media"


def start(http, media):
    client, _, base = http
    response = client.post(base + "/listening/begin", json=dict(prompt_id=media[3]["prompt_id"],
        item_version=media[3]["item_version"], artifact_id="audio", client_action_id="http-begin"))
    assert response.status_code == 200, response.text
    return response.json()["media"]


def approve(http, row):
    client, token, base = http
    response = client.post(base + f"/{row['media_id']}/source-review", headers={"X-HSC-Parent": token},
        json=dict(audio_sha256=row["audio"]["sha256"], operation="approve", client_action_id="http-review"))
    assert response.status_code == 200, response.text
    return response.json()["media"]


def control(http, row, command="resume", key="resume"):
    response = http[0].post(http[2] + "/listening/control", json=dict(media_id=row["media_id"],
        generation=row["generation"], expected_revision=row["revision"], command=command, client_action_id=key))
    assert response.status_code == 200, response.text
    return response.json()["media"]


def event(http, row, name, frame, clock, key):
    return http[0].post(http[2] + "/listening/playback", json=dict(media_id=row["media_id"],
        generation=row["generation"], expected_revision=row["revision"], event=name,
        audio_sha256=row["audio"]["sha256"], frame_position=frame, monotonic_ms=clock, client_action_id=key))


def test_auth_preview_review_actual_wav_delivery_and_receipts_are_separate(http, media):
    client, token, base = http
    row = start(http, media)
    url = base + f"/{row['media_id']}/audio"
    query = {"audio_sha256": row["audio"]["sha256"]}
    assert client.get(url, params=query).status_code == 409
    preview = base + f"/{row['media_id']}/review-audio"
    assert client.get(preview, params=query).status_code == 401
    response = client.get(preview, params=query, headers={"X-HSC-Parent": token})
    assert response.status_code == 200 and response.content == media[4].read_bytes()
    row = approve(http, row)
    response = client.get(url, params=query)
    assert response.status_code == 200
    assert hashlib.sha256(response.content).hexdigest() == query["audio_sha256"]
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["accept-ranges"] == "none"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert client.get(url, params=query, headers={"Range": "bytes=0-12"}).status_code == 416
    assert client.get(url, params={"audio_sha256": "0" * 64}).status_code == 409
    assert not client.get(base + f"/{row['media_id']}").json()["media"]["qualification"]["listening_delivery_qualified"]
    row = control(http, row)
    response = event(http, row, "started", 0, 1000, "http-start")
    assert response.status_code == 200
    row = response.json()["media"]
    media[5][0] += 1000
    ended = event(http, row, "ended", 16000, 2000, "http-end")
    assert ended.status_code == 200
    assert event(http, row, "ended", 16000, 2000, "http-end").json() == ended.json()
    assert ended.json()["media"]["qualification"]["listening_delivery_qualified"] is True
    assert ended.json()["media"]["qualification"]["skill_qualified"] is False


def test_local_session_dependency_is_required_even_for_parent(http, media):
    client, token, base = http
    row = start(http, media)
    client.headers.pop("X-HSC-Session")
    assert client.get(base + f"/{row['media_id']}", headers={"X-HSC-Parent": token}).status_code == 401
    assert client.get(base + f"/{row['media_id']}/review-audio", params={"audio_sha256": row["audio"]["sha256"]},
        headers={"X-HSC-Parent": token}).status_code == 401


@pytest.mark.parametrize("extra", [{"reviewer_role": "parent"}, {"reviewer_id": "admin"},
    {"quality": {"passed": True}}, {"score": 100}, {"local_path": "C:\\private.wav"}])
def test_schema_rejects_self_authorization_scores_quality_and_paths(http, media, extra):
    row = start(http, media)
    response = http[0].post(http[2] + f"/{row['media_id']}/source-review", headers={"X-HSC-Parent": http[1]},
        json=dict(audio_sha256=row["audio"]["sha256"], operation="approve", client_action_id="forged", **extra))
    assert response.status_code == 422
    with media[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='review_source'").fetchone()[0] == 0


def test_review_identity_is_derived_from_real_parent_auth(http, media):
    row = start(http, media)
    approve(http, row)
    with media[0].connect() as c:
        payload = json.loads(c.execute("SELECT payload_json FROM pet_media_event WHERE operation='review_source'").fetchone()[0])
        assert payload["reviewer_id"] == "household-parent" and payload["reviewer_role"] == "parent"


def test_restart_rejects_old_parent_token_and_end_until_new_generation(http, media):
    row = control(http, approve(http, start(http, media)))
    row = event(http, row, "started", 0, 1000, "started").json()["media"]
    client, token = mounted(media, restarted=True)
    client.headers["X-HSC-Session"] = "local-test-token"
    newer = client, token, http[2]
    read = client.get(http[2] + f"/{row['media_id']}").json()["media"]
    assert read["status"] == "paused" and read["resume_required"]
    assert event(newer, row, "ended", 16000, 2000, "old-end").status_code == 409
    assert client.get(http[2] + f"/{row['media_id']}/review-audio", params={"audio_sha256": row["audio"]["sha256"]},
        headers={"X-HSC-Parent": http[1]}).status_code == 401
    resumed = control(newer, read, key="restart-resume")
    assert resumed["generation"] > row["generation"]
    assert event(http, row, "ended", 16000, 2000, "late-old-process").status_code == 409


@pytest.mark.parametrize("change_source", ["file", "item", "prompt", "review"])
def test_http_current_source_recheck_blocks_stale_delivery(http, media, change_source):
    row = approve(http, start(http, media))
    if change_source == "file":
        wav_file(media[4], "silence")
    elif change_source == "item":
        change(media[0], "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (row["diagnostic_item_id"],))
    elif change_source == "prompt":
        change(media[0], "UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?", (row["prompt_id"],))
    else:
        assert http[0].post(http[2] + f"/{row['media_id']}/source-review", headers={"X-HSC-Parent": http[1]},
            json=dict(audio_sha256=row["audio"]["sha256"], operation="retract", client_action_id="retract")).status_code == 200
    response = http[0].get(http[2] + f"/{row['media_id']}/audio", params={"audio_sha256": row["audio"]["sha256"]})
    assert response.status_code == 409 and response.headers["content-type"] == "application/json"
    assert str(media[4]) not in response.text


def test_router_does_not_add_upload_or_child_voice_download(http, media):
    prompt, _ = speaking(media)
    response = http[0].post(http[2] + "/speaking/bind", json=dict(prompt_id=prompt["prompt_id"],
        item_version=prompt["item_version"], voice_input_id="voice", client_action_id="spoken-bind"))
    assert response.status_code == 200
    row = response.json()["media"]
    for route in ("audio", "review-audio"):
        assert http[0].get(http[2] + f"/{row['media_id']}/{route}", params={"audio_sha256": row["audio"]["sha256"]},
            headers={"X-HSC-Parent": http[1]}).status_code == 409
    assert http[0].post(http[2] + "/upload", files={"file": ("voice.wav", media[4].read_bytes())}).status_code in {404, 405}


def test_cross_session_http_cannot_read_review_or_download(http, media):
    row = approve(http, start(http, media))
    base = "/v1/sessions/nonexistent-session/pet-media"
    for route in ("", "/audio", "/review-audio"):
        response = http[0].get(base + f"/{row['media_id']}" + route,
            params={"audio_sha256": row["audio"]["sha256"]}, headers={"X-HSC-Parent": http[1]})
        assert response.status_code == 404


def test_audio_changed_between_inspection_and_response_is_never_returned(http, media, monkeypatch):
    row = approve(http, start(http, media))
    from home_study_agent import pet_media_evidence as module
    original = module.read_managed_media
    calls = []

    def race(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            wav_file(media[4], "silence")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "read_managed_media", race)
    response = http[0].get(http[2] + f"/{row['media_id']}/audio", params={"audio_sha256": row["audio"]["sha256"]})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PET_MEDIA_AUDIO_CHANGED_DURING_DELIVERY"


@pytest.mark.parametrize("edit", [{"generation": True}, {"frame_position": "0"}, {"quality": "trusted"}])
def test_http_rejects_untrusted_playback_fields(http, media, edit):
    row = control(http, approve(http, start(http, media)))
    body = dict(media_id=row["media_id"], generation=row["generation"], expected_revision=row["revision"],
        event="started", audio_sha256=row["audio"]["sha256"], frame_position=0, monotonic_ms=1000, client_action_id="invalid")
    response = http[0].post(http[2] + "/listening/playback", json={**body, **edit})
    assert response.status_code == 422


def test_body_cannot_supply_parent_role_instead_of_authentication(http, media):
    row = start(http, media)
    response = http[0].post(http[2] + f"/{row['media_id']}/source-review",
        json=dict(audio_sha256=row["audio"]["sha256"], operation="approve", client_action_id="forged",
                  reviewer_id="household-parent", reviewer_role="parent"))
    assert response.status_code == 401


def test_real_other_child_session_cannot_get_an_owned_clip(http, media):
    from home_study_agent.repository import StudyRepository
    row = approve(http, start(http, media))
    change(media[0], "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (media[2],))
    sid = StudyRepository(media[0]).start_session("child-junior-3", "other-child")["session_id"]
    response = http[0].get(f"/v1/sessions/{sid}/pet-media/{row['media_id']}/audio",
                          params={"audio_sha256": row["audio"]["sha256"]})
    assert response.status_code == 404


def test_exact_prompt_listing_empty_is_read_only_and_never_synthesizes_stimulus(http, media):
    path = http[2] + f"/prompt/{media[3]['prompt_id']}"
    query = {"item_version": media[3]["item_version"]}
    with media[0].connect() as c:
        before = {table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("pet_media_binding", "pet_media_event", "capture_artifact", "model_call")}
    response = http[0].get(path, params=query)
    assert response.status_code == 200
    data = response.json()
    assert data["listening"] == [] and data["speaking"] == [] and data["status"] == "missing_media"
    assert data["selection_required"] is False
    assert "stimulus_text" not in response.text and str(media[4]) not in response.text
    with media[0].connect() as c:
        assert before == {table: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
    assert http[0].get(path, params={"item_version": "stale"}).status_code == 409
    http[0].headers.pop("X-HSC-Session")
    assert http[0].get(path, params=query).status_code == 401


def test_prompt_collection_preserves_multiple_original_media_after_restart(http, media):
    first = start(http, media)
    change(media[0], """INSERT INTO capture_artifact SELECT 'audio-2',child_id,session_id,artifact_type,local_path,
        extracted_text,confidence,details_json,status,created_at,'audio-action-2' FROM capture_artifact WHERE artifact_id='audio'""")
    response = http[0].post(http[2] + "/listening/begin", json=dict(prompt_id=media[3]["prompt_id"],
        item_version=media[3]["item_version"], artifact_id="audio-2", client_action_id="second-media"))
    assert response.status_code == 200
    second = response.json()["media"]
    client, _ = mounted(media, restarted=True)
    client.headers["X-HSC-Session"] = "local-test-token"
    path = http[2] + f"/prompt/{media[3]['prompt_id']}"
    data = client.get(path, params={"item_version": media[3]["item_version"]}).json()
    assert data["selection_required"] is True
    assert {row["media_id"] for row in data["listening"]} == {first["media_id"], second["media_id"]}
    assert data["speaking"] == [] and "selected_media_id" not in data
    change(media[0], "UPDATE diagnostic_item SET stimulus_text='revised original' WHERE diagnostic_item_id=?",
           (media[3]["diagnostic_item_id"],))
    assert client.get(path, params={"item_version": media[3]["item_version"]}).status_code == 409


def test_prompt_list_rejects_other_child_session_even_with_valid_parent_auth(http, media):
    from home_study_agent.repository import StudyRepository
    start(http, media)
    change(media[0], "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (media[2],))
    sid = StudyRepository(media[0]).start_session("child-junior-3", "list-other-child")["session_id"]
    response = http[0].get(f"/v1/sessions/{sid}/pet-media/prompt/{media[3]['prompt_id']}",
        params={"item_version": media[3]["item_version"]}, headers={"X-HSC-Parent": http[1]})
    assert response.status_code == 404


def test_http_pending_end_202_is_durable_and_exact_retry_confirms_200(http, media):
    row = control(http, approve(http, start(http, media)))
    row = event(http, row, "started", 0, 1000, "start").json()["media"]
    media[5][0] += 850
    response = event(http, row, "ended", 16000, 2000, "end")
    assert response.status_code == 202
    pending = response.json()["media"]
    assert pending["receipt_retry_after_ms"] == 150 and pending["status"] == "pending_receipt"
    assert pending["qualification"]["task_delivery_qualified"] is False
    recovered = http[0].get(http[2] + f"/{row['media_id']}").json()["media"]
    assert recovered["pending_playback_receipt"] == pending["pending_playback_receipt"]
    media[5][0] += 150
    settled = http[0].post(http[2] + "/listening/playback", json=recovered["pending_playback_receipt"])
    assert settled.status_code == 200 and settled.json()["media"]["status"] == "playback_finished"
