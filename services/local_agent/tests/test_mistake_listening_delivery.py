"""A listening mistake remains an audio task inside the canonical mistake loop."""
from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
from home_study_agent.agent_runtime import AgentRuntimeStore, InvalidAgentPlanError
from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.mistake_book import MistakeBookService
from home_study_agent.mistake_dialogue import MistakeDialogueService
from home_study_agent.mistake_listening_delivery import prepare
from home_study_agent.pet_media_evidence import project_pet_media_evidence
from test_nonseed_listening_cross_day_http import play
from test_pet_listening_resource_admission import bind_audio
from test_pet_reviewed_source_contract import packet
from test_pet_reviewed_source_publication import CHILD, prepared, publish
from test_writing_stage_review_consumers import advance_clock


@pytest.mark.parametrize('track', ['pet', 'school'])
def test_listening_mistake_hides_transcript_and_requires_fresh_review_playback(tmp_path, monkeypatch, track):
    CHILD = 'child-junior-3' if track == 'school' else 'child-primary-4'
    source = packet(
        prompt="What is the main message?",
        stimulus_text="Hello everyone. The science club meeting will be next Tuesday instead.",
        expected_answer={"local_rule": "local_exact_v1", "exact_answers": ["the meeting is postponed"]},
    )
    if track == 'school':
        from test_school_listening_modality import prepared_school, publish_school, school_packet
        source = school_packet(**{key: value for key, value in source.items() if key != 'schema'})
        db, supply, spec, _, _ = prepared_school(tmp_path, source)
        seed_id = spec['seed_item_id']
        published = publish_school(supply, spec)
    else:
        db, supply, spec, _ = prepared(tmp_path, source)
        quote = "听懂简短通知的主要话题和结果。"
        with db.connect() as connection:
            connection.execute("UPDATE curriculum_standard_page SET content_text=? WHERE source_id='pet-std'", (quote,))
            connection.execute("""UPDATE curriculum_standard_requirement
                SET title='Listening gist',requirement_text=?,observable_outcome='Main message'
                WHERE requirement_id='pet-req'""", (quote,))
            connection.commit()
        seed_id = 'diag-pet-listening-gist-baseline'
        published = publish(supply, {**spec, 'seed_item_id': seed_id,
            "standard_quote": quote, "explanation": "The meeting is postponed until next Tuesday."})
    item_id = published["diagnostic_item_id"]
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        session_token="mistake-listening-delivery")
    clock = advance_clock(monkeypatch, datetime.now(timezone.utc).replace(microsecond=0))
    mono = [1000]
    monkeypatch.setattr("home_study_agent.pet_media_evidence._clock_ms", lambda: mono[0])

    with TestClient(create_app(settings), headers={"X-HSC-Session": settings.session_token}) as client:
        assert client.post("/v1/parent/setup", json={"pin": "2468"}).status_code == 200
        first_sid = client.post("/v1/sessions", json={
            "child_id": CHILD, "client_action_id": "listening-source-session"}).json()["session_id"]
        original = client.post(f"/v1/sessions/{first_sid}/diagnostic-prompt/prepare", json={
            "track": track, "preferred_item_id": item_id, "client_action_id": "listening-source-prompt"})
        assert original.status_code == 200, original.text
        old_prompt = original.json()["prompt"]
        bind_audio(client, db, tmp_path, first_sid, old_prompt, key="mistake-reviewed-source")
        with db.connect() as connection:
            connection.execute("UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?",
                               (first_sid,))
            connection.commit()

        sid = client.post("/v1/sessions", json={
            "child_id": CHILD, "client_action_id": "listening-mistake-session"}).json()["session_id"]
        app_db = client.app.state.repository.database
        with app_db.connect() as connection:
            connection.execute("UPDATE daily_session SET state='study' WHERE session_id=?", (sid,))
            node_id = connection.execute(
                "SELECT knowledge_node_id FROM diagnostic_item WHERE diagnostic_item_id=?", (item_id,)).fetchone()[0]
            connection.commit()
        book = MistakeBookService(app_db)
        mistake_id = book.capture(
            child_id=CHILD, knowledge_node_id=node_id, subject="英语", title="听力通知",
            prompt_summary="听力通知原题", submitted_answer="wrong", source_type="diagnostic",
            source_ref=None, confidence=1, client_action_id="capture-listening-mistake",
            diagnostic_item_id=seed_id,
        )["mistake_id"]
        book.confirm(mistake_id, CHILD, "confirm", "confirm-listening-mistake")
        book.diagnose(mistake_id, CHILD, "incidental", "原听力作答未通过", 1,
                      "diagnose-listening-mistake")
        book.add_remediation(mistake_id, CHILD, "active_recall", "completed", None,
                             "remediate-listening-mistake")
        dialogue = MistakeDialogueService(app_db)
        focus = dialogue.open_review(sid, "open-listening-mistake", mistake_id=mistake_id)
        assert focus["phase"] == "attempt"
        assert focus["state"]["next_activity"]["item"]["answer_type"] == "listening_text"
        assert focus["state"]["next_activity"]["item"]["diagnostic_item_id"] == item_id
        visible = AgentCapabilityExecutor._mistake_activity_prompt({
            **book.get(mistake_id, CHILD), "next_activity": focus["state"]["next_activity"]})
        assert "先播放" in visible and source["stimulus_text"] not in visible

        with app_db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            delivery = prepare(connection, focus, "listening-mistake-plan", "prepare-listening-mistake")
            connection.commit()
        assert delivery["status"] == "ready", (delivery.get("reason"),
            (delivery.get("prompt") or {}).get("prompt", {}).get("diagnostic_item_id"))
        prompt = delivery["prompt"]["prompt"]
        with app_db.connect() as connection:
            request = json.loads(connection.execute(
                "SELECT request_json FROM diagnostic_prompt WHERE prompt_id=?", (prompt["prompt_id"],)).fetchone()[0])
            assert request["mistake_listening_delivery"]["mistake_id"] == mistake_id
            generic = connection.execute(
                """SELECT prompt_id FROM diagnostic_prompt
                   WHERE origin_session_id=? AND child_id=?
                     AND status IN ('waiting_answer','answer_saved')
                     AND COALESCE(json_extract(request_json,'$.mistake_listening_delivery.version'),'')
                         != 'mistake_listening_delivery_v1'
                   ORDER BY rowid DESC LIMIT 1""",
                (sid, CHILD),
            ).fetchone()
            assert generic is None
            mistake_target = AgentRuntimeStore.local_mistake_answer_target(connection, sid, CHILD)
            assert mistake_target and mistake_target["binding"]["mistake_id"] == mistake_id
            audio = project_pet_media_evidence(connection, sid,
                media_id=delivery["resource"]["media_id"], media_root=tmp_path)

        with pytest.raises(InvalidAgentPlanError, match="ANSWER_LISTENING_PLAYBACK_REQUIRED"):
            dialogue.respond(sid, "the meeting is postponed", "answer-before-playback")
        audio = play(client, sid, audio, "mistake-fresh-playback", mono, clock)
        assert audio["qualification"]["task_delivery_qualified"]
        result = dialogue.respond(sid, "the meeting is postponed", "answer-after-playback")
        assert result["state"]["last_mastery_gate"]
        with app_db.connect() as connection:
            attempt = connection.execute(
                "SELECT correctness,independent FROM mistake_attempt WHERE mistake_id=?", (mistake_id,)).fetchone()
            prompt_state = connection.execute(
                "SELECT status,reason FROM diagnostic_prompt WHERE prompt_id=?", (prompt["prompt_id"],)).fetchone()
        assert tuple(attempt) == (1.0, 1)
        assert tuple(prompt_state) == ("cancelled", "mistake_dialogue_scored")
        persisted_status = book.get(mistake_id, CHILD)["status"]

    restarted = Database(db.path)
    restored = MistakeBookService(restarted).get(mistake_id, CHILD)
    assert restored["status"] == persisted_status
    with restarted.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mistake_attempt WHERE mistake_id=? AND independent=1",
            (mistake_id,),
        ).fetchone()[0] == 1
        assert tuple(connection.execute(
            "SELECT status,reason FROM diagnostic_prompt WHERE prompt_id=?",
            (prompt["prompt_id"],),
        ).fetchone()) == ("cancelled", "mistake_dialogue_scored")
