"""Audio-reference admission, not certification of synthetic fixture speech."""
import json
import sys

import pytest

from home_study_agent.pet_speaking_review import prepare_speaking_audio_review, validate_speaking_audio_review
from test_pet_media_evidence import media, speaking, bind_speaking, local_review, change, CHILD


def setup(media):
    prompt, transcript = speaking(media)
    row = bind_speaking(media, prompt)
    assessment = local_review(media, prompt, transcript, approve=False)
    args = dict(media_id=row["media_id"], assessment_id=assessment["assessment_id"], media_root=media[4].parent)
    return row, args


def observation(packet):
    return {"schema": "pet_speaking_audio_references_v1", "binding_version": packet["binding_version"],
            "audio_sha256": packet["audio"]["sha256"], "observations": [{"dimension_id": name,
                "start_frame": 0, "end_frame": 16000, "heard_text": "Synthetic fixture annotation, not scored",
                "rationale": "Fixture verifies frame bounds and provenance only"} for name in packet["dimensions_required"]]}


def test_real_waveform_reference_is_readonly_recoverable_and_not_a_score(media):
    _, args = setup(media)
    with media[0].connect() as c:
        before = list(c.iterdump())
        packet = prepare_speaking_audio_review(c, media[2], **args)
        result = validate_speaking_audio_review(c, media[2], review=observation(packet), **args)
        assert list(c.iterdump()) == before
    with media[0].connect() as c:
        assert prepare_speaking_audio_review(c, media[2], **args) == packet
    assert packet["audio"]["frames"] == 16000 and len(packet["waveform"]) == 10
    assert packet["waveform"][0]["rms"] > 0 and packet["waveform"][0]["end_frame"] == 1600
    assert result["status"] == "audio_references_validated_not_scored"
    assert not result["skill_qualified"] and not result["mastery_claim_allowed"]
    assert "999999" not in json.dumps(packet) and str(media[4]) not in json.dumps(packet)
    assert "score" not in result["audio_evidence"] and result["cloud_transmission_allowed"] is False


@pytest.mark.parametrize("fault", ["sha", "version", "negative", "past_end", "bool", "empty", "duplicate", "score", "role", "nan"])
def test_untrusted_audio_annotations_cannot_self_certify_or_escape_original_frames(media, fault):
    _, args = setup(media)
    with media[0].connect() as c:
        review = observation(prepare_speaking_audio_review(c, media[2], **args))
        if fault == "sha": review["audio_sha256"] = "a" * 64
        elif fault == "version": review["binding_version"] = "old"
        elif fault == "negative": review["observations"][0]["start_frame"] = -1
        elif fault == "past_end": review["observations"][0]["end_frame"] = 16001
        elif fault == "bool": review["observations"][0]["end_frame"] = True
        elif fault == "empty": review["observations"] = []
        elif fault == "duplicate": review["observations"][0] = dict(review["observations"][1])
        elif fault == "score": review["observations"][0]["score"] = 3
        elif fault == "role": review["reviewer_role"] = "teacher"
        elif fault == "nan": review["observations"][0]["start_frame"] = float("nan")
        with pytest.raises(ValueError):
            validate_speaking_audio_review(c, media[2], review=review, **args)


@pytest.mark.parametrize("fault", ["transcript", "audio", "cancel", "other_child", "attempt", "evidence_revision"])
def test_review_rechecks_exact_sources_in_same_transaction(media, fault):
    row, args = setup(media)
    with media[0].connect() as c:
        packet = prepare_speaking_audio_review(c, media[2], **args)
        c.execute("BEGIN IMMEDIATE")
        if fault == "transcript": c.execute("UPDATE voice_input SET transcript='revised words' WHERE voice_input_id='voice'")
        elif fault == "audio": media[4].write_bytes(b"not audio")
        elif fault == "cancel": c.execute("UPDATE pet_media_binding SET state_json=json_set(state_json,'$.status','cancelled') WHERE media_id=?", (row["media_id"],))
        elif fault == "other_child": c.execute("UPDATE voice_input SET child_id='child-junior-3' WHERE voice_input_id='voice'")
        elif fault == "attempt": c.execute("UPDATE diagnostic_attempt SET submitted_answer='different answer'")
        else: c.execute("UPDATE learning_evidence SET evidence_summary='revised original evidence'")
        before = c.total_changes
        with pytest.raises(ValueError):
            validate_speaking_audio_review(c, media[2], review=observation(packet), **args)
        assert c.total_changes == before and c.in_transaction
        c.rollback()


def test_cross_session_and_unrelated_assessment_rejected(media):
    _, args = setup(media)
    with media[0].connect() as c:
        with pytest.raises(ValueError):
            prepare_speaking_audio_review(c, "other-session", **args)
        with pytest.raises(ValueError):
            prepare_speaking_audio_review(c, media[2], **{**args, "assessment_id": "another-assessment"})


def test_parent_local_http_original_audio_review_has_no_play_or_score_writes(media):
    from test_pet_media_http import mounted
    row, args = setup(media)
    client, token = mounted(media)
    base = f"/v1/sessions/{media[2]}/pet-media/{row['media_id']}"
    params = {"assessment_id": args["assessment_id"]}
    assert client.get(base + "/speaking-review", params=params).status_code == 401
    client.headers["X-HSC-Session"] = "local-test-token"
    assert client.get(base + "/speaking-review", params=params).status_code == 401
    headers = {"X-HSC-Parent": token}
    with media[0].connect() as c:
        before = list(c.iterdump())
    response = client.get(base + "/speaking-review", params=params, headers=headers)
    assert response.status_code == 200, response.text
    packet = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert str(media[4]) not in response.text
    params["audio_sha256"] = packet["audio"]["sha256"]
    url = base + "/speaking-review-audio"
    assert client.get(url, params=params).status_code == 401
    response = client.get(url, params=params, headers=headers)
    assert response.status_code == 200 and response.content == media[4].read_bytes()
    assert response.headers["cache-control"] == "no-store"
    assert client.get(url, params=params, headers={**headers, "Range": "bytes=0-100"}).status_code == 416
    assert client.get(url, params={**params, "audio_sha256": "a" * 64}, headers=headers).status_code == 409
    assert client.get(url, params={**params, "assessment_id": "wrong"}, headers=headers).status_code == 409
    with media[0].connect() as c:
        assert list(c.iterdump()) == before
    change(media[0], "UPDATE voice_input SET transcript='revised' WHERE voice_input_id='voice'")
    assert client.get(url, params=params, headers=headers).status_code == 409


@pytest.mark.skipif(sys.platform != "darwin", reason="Installed offline voice, no real child recording")
def test_actual_host_authorized_original_voice_bytes_and_waveform(media, tmp_path):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from home_study_agent.pet_local_audio import render_original_practice
    from test_pet_reviewed_source_actual_http import NoCloud
    transcript = "I like reading in the library because it is quiet. I read story books there after school with my friends."
    content, engine = render_original_practice(transcript)
    media[4].write_bytes(content)
    prompt, transcript = speaking(media, transcript=transcript)
    row = bind_speaking(media, prompt)
    assessment = local_review(media, prompt, transcript, approve=False)
    settings = Settings(data_dir=media[4].parent, database_path=media[0].path,
                        maintenance_enabled=False, session_token="isolated-speaking-review")
    local = {"X-HSC-Session": settings.session_token}
    base = f"/v1/sessions/{media[2]}/pet-media/{row['media_id']}"
    params = {"assessment_id": assessment["assessment_id"]}
    with TestClient(create_app(settings, model_gateway=NoCloud())) as client:
        setup_response = client.post("/v1/parent/setup", json={"pin": "2468"}, headers=local)
        assert setup_response.status_code == 200
        headers = {**local, "X-HSC-Parent": setup_response.json()["parent_token"]}
        response = client.get(base + "/speaking-review", params=params, headers=headers)
        assert response.status_code == 200, response.text
        packet = response.json()
        assert packet["audio"]["frames"] > 16000 and not packet["skill_qualified"]
        params["audio_sha256"] = packet["audio"]["sha256"]
        response = client.get(base + "/speaking-review-audio", params=params, headers=headers)
        assert response.status_code == 200 and response.content == content
        assert engine == "macos_installed_daniel_v1"
        with media[0].connect() as c:
            assert c.execute("SELECT COUNT(*) FROM rubric_review_event").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM pet_media_event WHERE operation='playback'").fetchone()[0] == 0
