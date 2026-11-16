"""Actual host publication and offline original audio, never a child outcome.

The audio renderer is real; segment receipts use controlled transport clocks.
This is not another Electron/device-render claim or official PET recording.
"""
import math
import sys

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.learning_obligations import project_child_mastery_evidence
from test_pet_reviewed_source_publication import prepared, CHILD
from test_pet_reviewed_source_contract import packet


class NoCloud:
    async def complete(self, *args, **kwargs):
        raise AssertionError("ISOLATED_TEST_CLOUD_FORBIDDEN")


def post(client, url, payload, headers):
    response = client.post(url, json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.skipif(sys.platform != "darwin", reason="Real installed offline macOS voice required")
@pytest.mark.parametrize("required,plays,answer,verified,score", [
    (1, 1, "at the red bridge", True, 1),
    (3, 3, "AT THE RED BRIDGE.", True, 1),
    (3, 1, "at the red bridge", False, None),
    (1, 1, "not at the red bridge", True, 0),
])
def test_actual_publication_original_audio_exact_answer_restart_and_withdraw(
        tmp_path, monkeypatch, required, plays, answer, verified, score):
    db, _, spec, original = prepared(tmp_path, packet(required_play_count=required,
        instruction="Listen once." if required == 1 else "Listen three times."))
    key = b"T" * 32
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        session_token="isolated-publication-host", data_encryption_key=key)
    local = {"X-HSC-Session": settings.session_token}
    configured = Database(db.path, media_root=tmp_path, data_encryption_key=key)
    clock = [1000]
    monkeypatch.setattr("home_study_agent.pet_media_evidence._clock_ms", lambda: clock[0])
    base = f"/api/parent/children/{CHILD}/curriculum-supply"
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=("127.0.0.1", 41000), raise_server_exceptions=False) as client:
        parent = {**local, "X-HSC-Parent": post(client, "/v1/parent/setup", {"pin": "2468"}, local)["parent_token"]}
        request = {"source": spec, "action_id": "original-generate", "expected_version": 0, "expected_scope_version": 1}
        assert client.post(base + "/generate", json=request, headers=local).status_code == 401
        assert client.post(base + "/generate", json={**request, "source": {**spec, "pet_listening": original}}, headers=parent).status_code == 422
        draft = post(client, base + "/generate", request, parent)
        assert draft["state"] == "draft", draft
        assert draft["review_packet"]["pet_listening"] == original
        cid = draft["candidate_id"]
        assert post(client, base + "/generate", request, parent)["candidate_id"] == cid
        for version, operation in enumerate(("validate", "review", "publish"), 1):
            payload = {"expected_version": version, "action_id": operation}
            if operation == "review":
                payload.update(decision="approve", note="Checked original complete response and play count; local practice only")
            result = post(client, base + f"/{cid}/{operation}", payload, parent)
        assert result["eligible"] and result["state"] == "published", result
        item_id = result["diagnostic_item_id"]
        assert item_id not in {"diag-pet-listening-1", "diag-pet-listening-2"}
        created = client.post("/v1/sessions", headers=local, json={"child_id": CHILD, "client_action_id": "isolated-session"})
        assert created.status_code == 201, created.text
        sid = created.json()["session_id"]
        diag = f"/v1/sessions/{sid}/diagnostic-prompt"
        prompt = post(client, diag + "/prepare", {"track": "pet", "preferred_item_id": item_id,
            "client_action_id": "question"}, local)["prompt"]
        assert prompt["diagnostic_item_id"] == item_id and prompt["item"]["pet_audio_modality"] == "listening"
        media = f"/v1/sessions/{sid}/pet-media"
        material_request = {"item_version": prompt["item_version"], "client_action_id": "make-original"}
        material_url = media + f"/prompt/{prompt['prompt_id']}/materialize"
        made = post(client, material_url, material_request, parent)["material"]
        assert made["engine"] == "macos_installed_daniel_v1" and not made["original_recording_verified"]
        assert post(client, material_url, material_request, parent)["material"] == made
        row = post(client, media + "/listening/begin", {"prompt_id": prompt["prompt_id"],
            "item_version": prompt["item_version"], "artifact_id": made["artifact_id"], "client_action_id": "bind"}, local)["media"]
        audio_url = media + f"/{row['media_id']}/audio?audio_sha256={row['audio']['sha256']}"
        assert client.get(audio_url, headers=local).status_code == 409
        preview = client.get(audio_url.replace("/audio?", "/review-audio?"), headers=parent)
        assert preview.status_code == 200 and preview.content.startswith(b"RIFF")
        assert not row["qualification"]["task_delivery_qualified"]
        row = post(client, media + f"/{row['media_id']}/source-review", {"audio_sha256": row["audio"]["sha256"],
            "operation": "approve", "client_action_id": "approve-audio"}, parent)["media"]
        assert client.get(audio_url, headers=local).content == preview.content
        for index in range(plays):
            row = post(client, media + "/listening/control", {"media_id": row["media_id"],
                "generation": row["generation"], "expected_revision": row["revision"],
                "command": "resume" if index == 0 else "restart", "client_action_id": f"play-{index}"}, local)["media"]
            start = clock[0]
            row = post(client, media + "/listening/playback", {"media_id": row["media_id"], "generation": row["generation"],
                "expected_revision": row["revision"], "event": "started", "frame_position": 0,
                "audio_sha256": row["audio"]["sha256"], "monotonic_ms": start, "client_action_id": f"start-{index}"}, local)["media"]
            clock[0] += math.ceil(row["audio"]["frames"] * 1000 / row["audio"]["sample_rate"])
            row = post(client, media + "/listening/playback", {"media_id": row["media_id"], "generation": row["generation"],
                "expected_revision": row["revision"], "event": "ended", "frame_position": row["audio"]["frames"],
                "audio_sha256": row["audio"]["sha256"], "monotonic_ms": clock[0], "client_action_id": f"end-{index}"}, local)["media"]
        assert row["qualification"]["task_delivery_qualified"] is (plays == required)
        answer_payload = {"prompt_id": prompt["prompt_id"], "item_version": prompt["item_version"],
                          "answer": answer, "client_action_id": "isolated-answer"}
        result = post(client, diag + "/answer", answer_payload, local)
        attempt = result["attempt"]
        assert (attempt["score_status"] == "verified_local") is verified, attempt
        assert post(client, diag + "/answer", answer_payload, local)["attempt"]["attempt_id"] == attempt["attempt_id"]
        with configured.connect() as c:
            saved = c.execute("SELECT * FROM diagnostic_attempt").fetchall()
            assert len(saved) == 1
            if verified:
                assert saved[0]["correctness"] == score
            assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=("127.0.0.1", 41000), raise_server_exceptions=False) as client:
        parent = {**local, "X-HSC-Parent": post(client, "/v1/parent/unlock", {"pin": "2468"}, local)["parent_token"]}
        read = client.get(diag, headers=local, params={"prompt_id": prompt["prompt_id"]})
        assert read.status_code == 200 and read.json()["attempt"]["attempt_id"] == attempt["attempt_id"]
        collection = client.get(media + f"/prompt/{prompt['prompt_id']}", headers=parent,
            params={"item_version": prompt["item_version"]})
        assert collection.status_code == 200, collection.text
        assert collection.json()["materials"][0]["artifact_id"] == made["artifact_id"]
        assert client.post(material_url, json=material_request, headers=parent).status_code == 409
        post(client, base + f"/{cid}/withdraw", {"expected_version": 4, "action_id": "withdraw",
             "note": "Original source withdrawn"}, parent)
        assert client.get(audio_url, headers=local).status_code == 409
        with configured.connect() as c:
            projection = project_child_mastery_evidence(c, CHILD)
            assert not any(row["evidence_id"] == attempt["evidence_id"] for node in projection.values() for row in node["effective_evidence"])
            assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
