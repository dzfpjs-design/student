import json
import sys

import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.pet_media_evidence import PetMediaEvidenceService
from home_study_agent.pet_local_audio import render_original_practice
from test_pet_media_evidence import media, CHILD, speaking, bind_speaking, change
from test_pet_media_http import mounted


def test_read_aloud_source_can_reach_real_speaking_binding(media):
    prompt, _ = speaking(media)
    service = DiagnosticPromptService(media[0])
    service.cancel(media[2], prompt_id=prompt["prompt_id"], item_version=prompt["item_version"])
    current = service.prepare(media[2], track="pet", preferred_item_id="diag-pet-read-aloud-1", client_action_id="read-aloud")["prompt"]
    change(media[0], "UPDATE voice_input SET purpose='pet_read_aloud',created_at=? WHERE voice_input_id='voice'", (current["created_at"],))
    row = bind_speaking(media, current)
    report = media[1].speaking_readiness(media[2], media_id=row["media_id"])
    assert report["task_family"] == "read_aloud" and report["signal_usable"]
    assert not report["skill_qualified"]


def test_synthetic_tone_is_not_speaking_competence_or_duration_score(media):
    prompt, _ = speaking(media)
    row = bind_speaking(media, prompt)
    report = media[1].speaking_readiness(media[2], media_id=row["media_id"])
    assert report["status"] == "recording_review_needed"
    assert not report["speech_activity"]["speech_candidate"]
    assert report["speech_activity"]["analyzed_frames"] == 33
    assert "999999" not in json.dumps(report)
    assert not report["skill_qualified"] and report["assessment_status"] == "missing_validated_audio_assessment"


def test_readonly_http_restart_and_source_retraction(media):
    prompt, transcript = speaking(media)
    row = bind_speaking(media, prompt)
    client, _ = mounted(media)
    path = f"/v1/sessions/{media[2]}/pet-media/{row['media_id']}/speaking-readiness"
    assert client.get(path).status_code == 401
    client.headers["X-HSC-Session"] = "local-test-token"
    before = client.get(path)
    assert before.status_code == 200
    assert transcript not in before.text and str(media[4]) not in before.text
    reopened = PetMediaEvidenceService(media[0], media_root=media[4].parent)
    assert reopened.speaking_readiness(media[2], media_id=row["media_id"]) == before.json()
    assert client.get(path.replace(media[2], "another-session")).status_code == 404
    change(media[0], "UPDATE voice_input SET transcript='changed original words' WHERE voice_input_id='voice'")
    after = client.get(path).json()
    assert after["status"] == "source_unavailable" and after["evidence_version"] != before.json()["evidence_version"]
    with media[0].connect() as c:
        assert c.execute("SELECT count(*) FROM pet_media_event").fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM diagnostic_attempt").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM model_call").fetchone()[0] == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="Uses installed offline voice on this host, not a child outcome")
def test_actual_local_public_speech_can_reach_assessor_ready_but_not_a_score(media):
    prompt, _ = speaking(media)
    content, _ = render_original_practice("I like reading in the library because it is quiet. I read books with my friends after school.")
    media[4].write_bytes(content)
    row = bind_speaking(media, prompt)
    report = media[1].speaking_readiness(media[2], media_id=row["media_id"])
    assert report["status"] == "ready_for_audio_assessment"
    assert report["speech_activity"]["speech_candidate"] and report["signal_usable"]
    assert not report["skill_qualified"] and not report["certification_claim_allowed"]
    assert report["obligations"][-1]["status"] == "assessor_not_installed"
