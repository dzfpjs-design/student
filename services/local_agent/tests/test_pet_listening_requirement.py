import json

import pytest

from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.pet_media_evidence import PetMediaEvidenceError, PetMediaEvidenceService
from home_study_agent.pet_listening_requirement import listening_requirement
from test_pet_media_evidence import media, CHILD, complete, control, record, change


@pytest.mark.parametrize("item,expected", [("diag-pet-listening-1", "listening"),
    ("diag-pet-listening-gist-baseline", "listening"), ("diag-pet-speaking-2", "speaking"),
    ("diag-pet-reading-1", None)])
def test_prompt_modality_is_read_only_authoritative_and_version_stable(media, item, expected):
    db, _, sid, original, _, _ = media
    service = DiagnosticPromptService(db)
    service.cancel(sid, prompt_id=original["prompt_id"], item_version=original["item_version"])
    result = service.prepare(sid, track="pet", preferred_item_id=item, client_action_id="new")
    prompt = result["prompt"]
    assert prompt["pet_audio_modality"] == prompt["item"]["pet_audio_modality"] == expected
    with db.connect() as c:
        persisted = c.execute("SELECT item_json,item_version FROM diagnostic_prompt WHERE prompt_id=?",
                              (prompt["prompt_id"],)).fetchone()
        assert "pet_audio_modality" not in json.loads(persisted["item_json"])
        assert persisted["item_version"] == prompt["item_version"]
    reopened = DiagnosticPromptService(db).read(sid, prompt_id=prompt["prompt_id"])
    assert reopened == result


def test_two_current_version_full_plays_are_delivery_not_skill(media):
    row = complete(media)
    row = record(media, control(media, row, key="resume-two"), mono=3000, key="start-two")
    media[5][0] += 1000
    row = record(media, row, "ended", 16000, 4000, "end-two")
    q = row["qualification"]
    assert q["task_complete_play_count"] == q["required_play_count"] == 2
    assert q["task_delivery_qualified"] and not q["skill_qualified"]
    restarted = PetMediaEvidenceService(media[0], media_root=media[4].parent, runtime_epoch="other")
    assert restarted.read(media[2], media_id=row["media_id"]) == row


def test_requirement_unknown_is_not_blanket_two(media):
    with media[0].connect() as c:
        requirement = listening_requirement(c, CHILD, "diag-pet-listening-gist-baseline")
        assert requirement["status"] == "unknown" and requirement["required_play_count"] is None


@pytest.mark.parametrize("legacy", [True, False])
def test_old_or_revised_requirements_never_upgrade_receipts(media, monkeypatch, legacy):
    row = complete(media)
    if legacy:
        change(media[0], "UPDATE pet_media_binding SET state_json=json_remove(state_json,'$.delivery_requirement')")
    else:
        monkeypatch.setattr("home_study_agent.pet_listening_requirement.POLICY_VERSION", "new-policy")
    result = media[1].read(media[2], media_id=row["media_id"])
    q = result["qualification"]
    assert not q["task_delivery_qualified"] and q["task_complete_play_count"] == 0
    assert q["requires_new_binding"] and q["required_play_count"] is None


def test_cancelled_first_binding_cannot_hide_a_new_active_binding(media):
    original = complete(media)
    control(media, original, "cancel", "cancel-first")
    arguments = dict(prompt_id=media[3]["prompt_id"], item_version=media[3]["item_version"], artifact_id="audio")
    new = media[1].begin_listening(media[2], client_action_id="new-binding", **arguments)
    assert new["qualification"]["task_complete_play_count"] == 0
    with pytest.raises(PetMediaEvidenceError, match="binding_exists"):
        media[1].begin_listening(media[2], client_action_id="third-binding", **arguments)
