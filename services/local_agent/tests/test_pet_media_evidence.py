"""Synthetic PCM proves local persistence/transport, never a child's PET outcome."""

import math
import json
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4
import wave

import pytest

from home_study_agent.database import Database
from home_study_agent.diagnostic_prompt import DiagnosticPromptService
from home_study_agent.learning_state import LearningStateService
from home_study_agent.learning_outcome_evaluator import LearningOutcomeEvaluator
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.pet_media_evidence import PetMediaEvidenceError, PetMediaEvidenceService, project_pet_media_evidence
from home_study_agent.repository import StudyRepository


CHILD = "child-primary-4"
ITEM = "diag-pet-listening-1"


def change(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def wav_file(path, kind="tone"):
    samples = [0 if kind == "silence" else (32767 if kind == "clipped" else
               int(7000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000)]
    with wave.open(str(path), "wb") as w:
        w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        w.writeframes(struct.pack("<16000h", *samples))


@pytest.fixture
def media(tmp_path, monkeypatch):
    db = Database(tmp_path / "media.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    LearningStateService(db).seed_catalog()
    sid = repo.start_session(CHILD, "start")["session_id"]
    prompt = DiagnosticPromptService(db).prepare(
        sid, track="pet", preferred_item_id=ITEM, client_action_id="ask")["prompt"]
    root = tmp_path / "media"
    root.mkdir()
    path = root / "synthetic.wav"
    wav_file(path)
    change(db, """INSERT INTO capture_artifact VALUES
        ('audio',?,?, 'audio',?,NULL,NULL,'{}','completed',?, 'audio-action')""",
        (CHILD, sid, str(path), prompt["created_at"]))
    clock = [1000]
    monkeypatch.setattr("home_study_agent.pet_media_evidence._clock_ms", lambda: clock[0])
    service = PetMediaEvidenceService(db, media_root=root, runtime_epoch="process-1")
    return db, service, sid, prompt, path, clock


def begin(media, **kwargs):
    _, service, sid, prompt, _, _ = media
    return service.begin_listening(sid, prompt_id=prompt["prompt_id"],
        item_version=prompt["item_version"], artifact_id="audio", client_action_id="begin", **kwargs)


def control(media, row, command="resume", key="resume"):
    return media[1].control_listening(media[2], media_id=row["media_id"],
        generation=row["generation"], expected_revision=row["revision"],
        command=command, client_action_id=key)


def record(media, row, event="started", frame=0, mono=1000, key="start"):
    return media[1].record_playback(media[2], media_id=row["media_id"],
        generation=row["generation"], expected_revision=row["revision"], event=event,
        audio_sha256=row["audio"]["sha256"], frame_position=frame, monotonic_ms=mono, client_action_id=key)


def review(media, row, operation="approve", key="review"):
    return media[1].review_listening_source(media[2], media_id=row["media_id"],
        audio_sha256=row["audio"]["sha256"], operation=operation,
        reviewer_id="synthetic-fixture-parent", reviewer_role="parent", client_action_id=key)


def complete(media):
    row = review(media, begin(media))
    row = record(media, control(media, row))
    media[5][0] += 1000
    return record(media, row, "ended", 16000, 2000, "end")


def test_real_pcm_and_complete_receipt_survive_restart_but_are_not_learning(media):
    row = complete(media)
    assert row["status"] == "playback_finished"
    assert row["qualification"]["listening_delivery_qualified"] is True
    assert row["qualification"]["skill_qualified"] is False
    assert row["audio"]["frames"] == 16000
    assert row["audio"]["quality_scope"] == "signal_only_not_speech_or_identity"
    reopened = PetMediaEvidenceService(Database(media[0].path), media_root=media[4].parent, runtime_epoch="process-2")
    assert reopened.read(media[2], media_id=row["media_id"]) == row
    with media[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0


def test_begin_and_receipt_exact_retry_do_not_duplicate_or_rebind(media):
    first = begin(media)
    assert begin(media) == first
    playing = control(media, first)
    started = record(media, playing)
    assert record(media, playing) == started
    with pytest.raises(PetMediaEvidenceError, match="action_conflict"):
        record(media, playing, mono=1001)
    with pytest.raises(PetMediaEvidenceError, match="action_conflict"):
        media[1].begin_listening(media[2], prompt_id=media[3]["prompt_id"],
            item_version="changed", artifact_id="audio", client_action_id="begin")


@pytest.mark.parametrize("fault", ["no_start", "short", "fast_client", "paused", "old_generation", "disconnected"])
def test_incomplete_or_stale_playback_cannot_count(media, fault):
    row = control(media, review(media, begin(media)))
    old = row
    if fault != "no_start":
        row = record(media, row)
    if fault in {"paused", "old_generation"}:
        paused = control(media, row, "pause", "pause")
        if fault == "old_generation":
            control(media, paused, "resume", "new-resume")
    media[5][0] += 30000 if fault == "disconnected" else 1000
    with pytest.raises(PetMediaEvidenceError):
        record(media, row, "ended", 15999 if fault == "short" else 16000,
            1999 if fault == "fast_client" else 2000, "end")
    assert media[1].read(media[2], media_id=old["media_id"])["qualification"]["listening_delivery_qualified"] is False


def test_restart_requires_new_generation_and_whole_replay(media):
    started = record(media, control(media, begin(media)))
    newer = PetMediaEvidenceService(Database(media[0].path), media_root=media[4].parent, runtime_epoch="process-2")
    view = newer.read(media[2], media_id=started["media_id"])
    assert view["status"] == "paused" and view["resume_required"] is True
    with pytest.raises(PetMediaEvidenceError, match="restart_requires_resume"):
        newer.record_playback(media[2], media_id=started["media_id"], generation=started["generation"],
            expected_revision=started["revision"], event="ended", frame_position=16000,
            audio_sha256=started["audio"]["sha256"], monotonic_ms=2000, client_action_id="late-end")
    resumed = newer.control_listening(media[2], media_id=view["media_id"], generation=view["generation"],
        expected_revision=view["revision"], command="resume", client_action_id="new-process-resume")
    assert resumed["generation"] > started["generation"]
    with pytest.raises(PetMediaEvidenceError):
        record(media, started, "ended", 16000, 2000, "old-process-end")


@pytest.mark.parametrize("kind", ["silence", "clipped", "truncated", "missing"])
def test_actual_audio_quality_not_metadata(media, kind):
    if kind in {"silence", "clipped"}:
        wav_file(media[4], kind)
    elif kind == "truncated":
        media[4].write_bytes(b"RIFFbad")
    else:
        media[4].unlink()
    row = begin(media)
    assert row["qualification"]["skill_qualified"] is False
    assert row["audio"]["status"] != "usable_signal"
    with pytest.raises(PetMediaEvidenceError):
        control(media, row)


@pytest.mark.parametrize("mutation", ["source", "audio", "prompt", "review"])
def test_current_projection_downgrades_withdrawal_or_revision(media, mutation):
    row = complete(media)
    if mutation == "source":
        change(media[0], "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
    elif mutation == "audio":
        wav_file(media[4], "silence")
    elif mutation == "prompt":
        change(media[0], "UPDATE diagnostic_prompt SET status='cancelled' WHERE prompt_id=?", (media[3]["prompt_id"],))
    else:
        review(media, row, "retract", "retract")
    current = media[1].read(media[2], media_id=row["media_id"])
    assert current["qualification"]["listening_delivery_qualified"] is False
    assert current["qualification"]["skill_qualified"] is False


def test_cross_child_and_same_child_other_session_cannot_read_or_bind(media):
    row = begin(media)
    repo = StudyRepository(media[0])
    change(media[0], "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (media[2],))
    other = repo.start_session("child-junior-3", "other")["session_id"]
    with pytest.raises(PetMediaEvidenceError, match="ownership"):
        media[1].read(other, media_id=row["media_id"])
    with pytest.raises(PetMediaEvidenceError, match="ownership"):
        media[1].begin_listening(other, prompt_id=media[3]["prompt_id"],
            item_version=media[3]["item_version"], artifact_id="audio", client_action_id="other-begin")
    change(media[0], "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (other,))
    later = repo.start_session(CHILD, "later")["session_id"]
    with pytest.raises(PetMediaEvidenceError, match="ownership"):
        media[1].read(later, media_id=row["media_id"])


def test_source_review_never_accepts_child_role(media):
    row = begin(media)
    with pytest.raises(PetMediaEvidenceError, match="parent_review_required"):
        media[1].review_listening_source(media[2], media_id=row["media_id"],
            audio_sha256=row["audio"]["sha256"], operation="approve", reviewer_id=CHILD,
            reviewer_role="child", client_action_id="forged-review")


def speaking(media, *, transcript="I like reading in the library because it is quiet. I read story books there after school with my friends."):
    db, service, sid, original, path, _ = media
    prompts = DiagnosticPromptService(db)
    prompts.cancel(sid, prompt_id=original["prompt_id"], item_version=original["item_version"])
    prompt = prompts.prepare(sid, track="pet", preferred_item_id="diag-pet-speaking-2", client_action_id="speaking-ask")["prompt"]
    from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
    captures = VoiceCaptureBindingService(db)
    captures.begin(sid, 'voice-action', path.read_bytes(), 'pet_speaking',
                   {'child_id': CHILD, 'question_reference': {k: prompt[k] for k in ('prompt_id', 'item_version')}})
    change(db, """INSERT INTO voice_input VALUES
        ('voice',?,?,'pet_speaking',?,?,'synthetic-no-recognizer',999999,'completed',?,'voice-action')""",
        (CHILD, sid, transcript, str(path), prompt["created_at"]))
    change(db, "INSERT INTO voice_transcription_action VALUES('voice-action',?,?,'completed',?)",
           (sid, CHILD, prompt['created_at']))
    captures.complete(sid, 'voice-action', 'voice')
    return prompt, transcript


def bind_speaking(media, prompt):
    return media[1].bind_speaking(media[2], prompt_id=prompt["prompt_id"], item_version=prompt["item_version"],
        voice_input_id="voice", client_action_id="bind-voice")


def local_review(media, prompt, answer, *, approve=True, scored=None):
    """Existing no-model evaluator/reviewer; not an audio assessment substitute."""
    db, _, sid, _, _, _ = media
    if scored is None:
        import asyncio
        from test_agent_continuation import orchestrator_for
        agent, gateway = orchestrator_for(db, media[4].parent, {})
        heard = asyncio.run(agent.interpret(sid, answer, 'spoken-original-turn', False, voice_input_id='voice'))
        if heard['intent'] == 'pending_answer_clarification':
            from home_study_agent.agent_capability_executor import AgentCapabilityExecutor
            receipt = AgentCapabilityExecutor(db, StudyRepository(db)).execute(heard['agent_turn_id'])
            agent.complete_turn(heard['agent_turn_id'], receipt['response_text'], 'completed', None, receipt)
            heard = asyncio.run(agent.interpret(sid, '刚才那句就是我的答案', 'spoken-confirmation-turn', False))
        assert heard['action'].get('source_voice_input_id') == 'voice', (heard['intent'], heard['action'])
        assert not gateway.requests
        scored = DiagnosticPromptService(db).answer(sid, prompt_id=prompt["prompt_id"],
            item_version=prompt["item_version"], answer=answer, client_action_id="spoken-answer", authorized_turn_id=heard['agent_turn_id'])
    rubric = RubricEvidenceService(db)
    rubric.initialize()
    candidate = rubric.diagnostic_candidate_input(CHILD, scored["attempt"]["evidence_id"])
    evaluator = LearningOutcomeEvaluator(db, StudyRepository(db), None,
        SimpleNamespace(model_provider="unused", model_name="unused"))
    objective, turn, plan, step = [uuid4().hex for _ in range(4)]
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as c:
        c.execute("""INSERT INTO agent_objective VALUES (?,?,?,'learning','PET fixture',?,
            'diagnose','active',?,?,NULL,NULL)""",
            (objective, CHILD, sid, json.dumps(["Answer the question independently"]), prompt["created_at"], now))
        c.execute("""INSERT INTO agent_turn (agent_turn_id,child_id,session_id,transcript,intent,
            response_text,action_json,orchestrator_version,created_at,client_action_id)
            VALUES (?,?,?,?,'learning.evaluate_response','','{"local_only":true,"cloud_transmission_allowed":false}',
            'test',?,?)""", (turn, CHILD, sid, answer, now, turn))
        c.execute("INSERT INTO agent_plan VALUES (?,?,?,?,?,'test','{}','{}','planned',?,?)",
                  (plan, objective, CHILD, sid, turn, now, now))
        c.execute("""INSERT INTO agent_step VALUES (?,?,0,'learning.evaluate_response','1',
            'local_model','medium',?,'[]','planned',?,NULL,NULL,?,?)""",
            (step, plan, json.dumps(candidate["arguments"]), step, now, now))
        c.commit()
    snapshot = evaluator.prepare_local_evaluation(turn)["evaluation"]["rubric_assessment"]
    if approve:
        review_rubric(rubric, snapshot)
    return snapshot


def review_rubric(rubric, snapshot, operation="approve", revision=0):
    return rubric.review(CHILD, snapshot["assessment_id"], expected_revision=revision,
        client_action_id=uuid4().hex, reviewer_id="synthetic-fixture-parent", reviewer_role="parent",
        operation=operation, reason="Synthetic fixture verifies reference plumbing, not a child outcome",
        dimensions=[dict(dimension_id=d["dimension_id"], score=3, response_quotes=[snapshot["response"]],
                         rationale="Local text criterion only") for d in snapshot["dimensions"]] if operation == "approve" else None,
        criterion_evidence=[dict(criterion=x, response_quote=snapshot["response"]) for x in snapshot["criteria"]]
            if operation == "approve" else None,
        independent=True if operation == "approve" else None, hint_level=0 if operation == "approve" else None)


def test_speaking_actual_audio_and_reviewed_text_do_not_certify_audio_skill(media):
    prompt, transcript = speaking(media)
    row = bind_speaking(media, prompt)
    assert row["audio"]["frames"] == 16000  # Deliberately false duration_ms is ignored.
    assert row["qualification"]["audio_signal_usable"] is True
    snapshot = local_review(media, prompt, transcript)
    current = media[1].attach_assessment(media[2], media_id=row["media_id"],
        assessment_id=snapshot["assessment_id"], client_action_id="attach")
    assert current["qualification"]["content_review_qualified"] is True
    assert current["qualification"]["assessment_receipt"]["assessment_id"] == snapshot["assessment_id"]
    assert current["qualification"]["skill_qualified"] is False
    assert media[1].attach_assessment(media[2], media_id=row["media_id"],
        assessment_id=snapshot["assessment_id"], client_action_id="attach") == current
    review_rubric(RubricEvidenceService(media[0]), snapshot, "retract", 1)
    reopened = PetMediaEvidenceService(Database(media[0].path), media_root=media[4].parent)
    withdrawn = reopened.read(media[2], media_id=row["media_id"])
    assert withdrawn["qualification"]["content_review_qualified"] is False
    assert withdrawn["evidence_version"] != current["evidence_version"]
    with media[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM diagnostic_attempt").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM model_call").fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["missing_audio", "voice_revision", "voice_cancelled", "action_cancelled", "source_changed"])
def test_speaking_missing_and_revised_sources_do_not_use_transcript_as_audio(media, fault):
    prompt, transcript = speaking(media)
    if fault == "missing_audio":
        change(media[0], "UPDATE voice_input SET local_path=NULL")
    row = bind_speaking(media, prompt)
    if fault == "voice_revision":
        change(media[0], "UPDATE voice_input SET transcript='a correction'")
    elif fault == "voice_cancelled":
        change(media[0], "UPDATE voice_input SET status='ignored'")
    elif fault == "action_cancelled":
        change(media[0], "UPDATE voice_transcription_action SET status='cancelled' WHERE client_action_id='voice-action'")
    elif fault == "source_changed":
        change(media[0], "UPDATE diagnostic_item SET prompt='changed source' WHERE diagnostic_item_id='diag-pet-speaking-2'")
    current = media[1].read(media[2], media_id=row["media_id"])
    assert current["qualification"]["audio_signal_usable"] is False
    assert current["qualification"]["skill_qualified"] is False


def test_canonical_realtime_transcript_never_becomes_pet_audio_or_pronunciation_evidence(media):
    from home_study_agent.voice_capture_binding import VoiceCaptureBindingService
    prompt, text = speaking(media)
    captures = VoiceCaptureBindingService(media[0])
    captures.begin_realtime(media[2], 'realtime-speaking-input', 'pet_speaking',
        {'child_id': CHILD, 'question_reference': {k: prompt[k] for k in ('prompt_id', 'item_version')}},
        model_version='synthetic-realtime-asr')
    original = captures.complete_realtime(media[2], 'realtime-speaking-input', text)
    row = media[1].bind_speaking(media[2], prompt_id=prompt['prompt_id'], item_version=prompt['item_version'],
        voice_input_id=original['voice_input_id'], client_action_id='bind-realtime-no-audio')
    assert row['audio']['status'] == 'missing_audio' and row['audio']['sha256'] is None
    assert row['qualification']['audio_signal_usable'] is False
    assert row['qualification']['skill_qualified'] is False
    reopened = PetMediaEvidenceService(Database(media[0].path), media_root=media[4].parent, runtime_epoch='restarted')
    recovered = reopened.read(media[2], media_id=row['media_id'])
    assert recovered['qualification']['skill_qualified'] is False
    with media[0].connect() as c:
        voice = c.execute('SELECT local_path,duration_ms FROM voice_input WHERE voice_input_id=?', (original['voice_input_id'],)).fetchone()
        assert tuple(voice) == (None, None)
        assert c.execute('SELECT wav_sha256 FROM voice_capture_binding WHERE voice_input_id=?', (original['voice_input_id'],)).fetchone()[0] is None
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0


@pytest.mark.parametrize("fault", ["unreviewed", "different_answer", "missing_assessment"])
def test_assessment_must_reference_current_original_answer_not_another_score(media, fault):
    prompt, transcript = speaking(media)
    row = bind_speaking(media, prompt)
    scored = None
    if fault == "different_answer":
        # A separate text answer remains legal but cannot adopt this recording.
        scored = DiagnosticPromptService(media[0]).answer(media[2], prompt_id=prompt['prompt_id'],
            item_version=prompt['item_version'], answer='I read at home.', client_action_id='different-text-answer')
    snapshot = local_review(media, prompt, "I read at home." if fault == "different_answer" else transcript,
                            approve=fault != "unreviewed", scored=scored)
    with pytest.raises(PetMediaEvidenceError):
        media[1].attach_assessment(media[2], media_id=row["media_id"],
            assessment_id="missing" if fault == "missing_assessment" else snapshot["assessment_id"], client_action_id="attach")
    with media[0].connect() as c:
        assert c.execute("SELECT assessment_id FROM pet_media_binding").fetchone()[0] is None


def test_file_escape_and_symlink_are_rejected_even_with_persisted_path(media):
    outside = media[4].parent.parent / "outside.wav"
    wav_file(outside)
    change(media[0], "UPDATE capture_artifact SET local_path=?", (str(outside),))
    assert begin(media)["qualification"]["audio_signal_usable"] is False
    media[4].unlink()
    media[4].symlink_to(outside)
    from home_study_agent.pet_media_evidence import inspect_local_pcm
    assert inspect_local_pcm(str(media[4]), media_root=media[4].parent)["status"] != "usable_signal"


@pytest.mark.parametrize("field,value", [("frame_position", True), ("frame_position", -1),
    ("monotonic_ms", float("nan")), ("monotonic_ms", "1000"), ("generation", True), ("audio_sha256", "other-audio")])
def test_playback_rejects_forged_numbers_and_other_audio(media, field, value):
    row = control(media, begin(media))
    args = dict(media_id=row["media_id"], generation=row["generation"], expected_revision=row["revision"],
        event="started", audio_sha256=row["audio"]["sha256"], frame_position=0, monotonic_ms=1000, client_action_id="event")
    args[field] = value
    with pytest.raises((PetMediaEvidenceError, ValueError)):
        media[1].record_playback(media[2], **args)


def test_same_connection_projection_does_not_write_and_sees_uncommitted_withdrawal(media):
    row = complete(media)
    with media[0].connect() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
        before = c.total_changes
        current = project_pet_media_evidence(c, media[2], media_id=row["media_id"], media_root=media[4].parent)
        assert current["qualification"]["listening_delivery_qualified"] is False
        assert c.in_transaction and c.total_changes == before
        c.rollback()
    assert media[1].read(media[2], media_id=row["media_id"])["qualification"]["listening_delivery_qualified"] is True


def test_subprocess_read_is_paused_and_contains_no_raw_media_or_paths(media):
    row = record(media, control(media, begin(media)))
    script = """
import json, sys
from home_study_agent.database import Database
from home_study_agent.pet_media_evidence import PetMediaEvidenceService
s = PetMediaEvidenceService(Database(sys.argv[1]), media_root=sys.argv[2])
print(json.dumps(s.read(sys.argv[3], media_id=sys.argv[4])))
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    output = subprocess.check_output([sys.executable, "-c", script, str(media[0].path),
        str(media[4].parent), media[2], row["media_id"]], env=env, text=True)
    current = json.loads(output)
    assert current["status"] == "paused" and current["resume_required"] is True
    assert str(media[4]) not in output and "stimulus_text" not in output
    assert current["cloud_transmission_allowed"] is False


def test_completed_session_preserves_actual_history_but_forbids_new_playback(media):
    row = complete(media)
    change(media[0], "UPDATE daily_session SET status='completed',state='completed' WHERE session_id=?", (media[2],))
    assert media[1].read(media[2], media_id=row["media_id"])["qualification"]["listening_delivery_qualified"] is True
    with pytest.raises(PetMediaEvidenceError, match="inactive_session"):
        control(media, row, "restart", "later-restart")


def test_competing_resume_uses_cas_and_old_end_never_advances_new_generation(media):
    row = record(media, control(media, review(media, begin(media))))

    def race(key):
        try:
            return control(media, row, "resume", key)
        except PetMediaEvidenceError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, ["tab-one", "tab-two"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "stale_generation_or_revision" in results
    media[5][0] += 1000
    with pytest.raises(PetMediaEvidenceError):
        record(media, row, "ended", 16000, 2000, "delayed-previous-end")
    current = media[1].read(media[2], media_id=row["media_id"])
    assert current["status"] == "playing" and not current["qualification"]["listening_delivery_qualified"]


def test_simultaneous_exact_begin_is_one_durable_binding(media):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: begin(media), range(2)))
    assert results[0] == results[1]
    with media[0].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM pet_media_binding").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM pet_media_event").fetchone()[0] == 1


def test_repeat_whole_play_is_counted_only_once_per_completed_generation(media):
    first = complete(media)
    replay = control(media, first, "restart", "replay")
    started = record(media, replay, "started", 0, 3000, "second-start")
    media[5][0] += 1000
    second = record(media, started, "ended", 16000, 4000, "second-end")
    assert second["qualification"]["delivery_receipt"]["complete_play_count"] == 2
    assert record(media, started, "ended", 16000, 4000, "second-end") == second
    assert second["qualification"]["skill_qualified"] is False


@pytest.mark.parametrize("phase", ["check_in", "classroom_recall", "homework_capture", "planning", "study", "break", "reflection"])
def test_nonterminal_phases_need_no_study_page_or_state_change(media, phase):
    change(media[0], "UPDATE daily_session SET state=? WHERE session_id=?", (phase, media[2]))
    assert complete(media)["qualification"]["listening_delivery_qualified"] is True
    with media[0].connect() as c:
        assert c.execute("SELECT state FROM daily_session WHERE session_id=?", (media[2],)).fetchone()[0] == phase


def test_foreign_artifact_cannot_be_reused_under_owned_prompt(media):
    change(media[0], "UPDATE capture_artifact SET child_id='child-junior-3'")
    with pytest.raises(PetMediaEvidenceError, match="ownership"):
        begin(media)


def test_reference_answer_and_full_audio_receipts_cannot_promote_pet_stage(media):
    row = complete(media)
    DiagnosticPromptService(media[0]).answer(media[2], prompt_id=media[3]["prompt_id"],
        item_version=media[3]["item_version"], answer="in front of the library", client_action_id="reference-answer")
    current = media[1].read(media[2], media_id=row["media_id"])
    assert current["qualification"]["listening_delivery_qualified"] is True
    assert current["qualification"]["answer_evidence_qualified"] is False
    assert current["qualification"]["stage_advance_allowed"] is False
    from home_study_agent.specialized_stages import project_specialized_stages
    with media[0].connect() as c:
        stages = project_specialized_stages(c, CHILD, "pet")
        assert stages["mastery_claim_allowed"] is False
        assert "listening_delivery_receipt_unavailable" in json.dumps(stages)


def pending_end(media):
    row = record(media, control(media, review(media, begin(media))))
    media[5][0] += 850
    pending = record(media, row, "ended", 16000, 2000, "asymmetric-end")
    return row, pending


def test_asymmetric_rpc_latency_saves_pending_end_then_confirms_without_replay(media):
    original, pending = pending_end(media)
    assert pending["status"] == "pending_receipt" and pending["receipt_retry_after_ms"] == 150
    assert pending["qualification"]["clip_delivery_qualified"] is False
    assert pending["pending_playback_receipt"]["expected_revision"] == original["revision"]
    assert pending["resume_required"] is False
    assert record(media, original, "ended", 16000, 2000, "asymmetric-end")["status"] == "pending_receipt"
    media[5][0] += 150
    # A GET never settles the durable report or implicitly starts another clip.
    assert media[1].read(media[2], media_id=pending["media_id"])["status"] == "pending_receipt"
    settled = media[1].record_playback(media[2], **pending["pending_playback_receipt"])
    assert settled["status"] == "playback_finished"
    assert settled["generation"] == original["generation"]
    assert settled["qualification"]["delivery_receipt"]["complete_play_count"] == 1
    assert media[1].record_playback(media[2], **pending["pending_playback_receipt"]) == settled
    with media[0].connect() as c:
        events = [json.loads(row[0]) for row in c.execute("SELECT payload_json FROM pet_media_event WHERE operation IN ('playback','playback_settled')")]
        assert sum(e["event"] == "started" for e in events) == 1
        assert sum(e["event"] == "ended" and e["receipt_state"] == "accepted" for e in events) == 1


def test_pending_end_survives_restart_without_another_started_event(media, monkeypatch):
    _, pending = pending_end(media)
    newer = PetMediaEvidenceService(Database(media[0].path), media_root=media[4].parent, runtime_epoch="after-crash")
    recovered = newer.read(media[2], media_id=pending["media_id"])
    assert recovered["pending_playback_receipt"] == pending["pending_playback_receipt"]
    later = datetime.now(timezone.utc) + timedelta(seconds=1)
    monkeypatch.setattr("home_study_agent.pet_media_evidence._now", lambda: later)
    settled = newer.record_playback(media[2], **recovered["pending_playback_receipt"])
    assert settled["status"] == "playback_finished" and settled["qualification"]["clip_delivery_qualified"]


@pytest.mark.parametrize("fault", ["generation", "withdrawal", "changed_payload"])
def test_pending_end_does_not_bypass_new_generation_source_or_exact_retry(media, fault):
    _, pending = pending_end(media)
    payload = pending["pending_playback_receipt"]
    if fault == "generation":
        control(media, pending, "resume", "new-generation")
    elif fault == "withdrawal":
        change(media[0], "UPDATE diagnostic_item SET status='inactive' WHERE diagnostic_item_id=?", (ITEM,))
    else:
        payload = {**payload, "monotonic_ms": 2001}
    media[5][0] += 150
    with pytest.raises(PetMediaEvidenceError):
        media[1].record_playback(media[2], **payload)


def test_slow_source_verification_is_not_miscounted_as_late_playback(media, monkeypatch):
    row = record(media, control(media, review(media, begin(media))))
    media[5][0] += 1000
    from home_study_agent import pet_media_evidence as module
    original = module._current

    def slow_verification(*args, **kwargs):
        media[5][0] += 4000
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_current", slow_verification)
    ended = record(media, row, "ended", 16000, 2000, "end")
    assert ended["status"] == "playback_finished"
    with media[0].connect() as c:
        payload = json.loads(c.execute("SELECT payload_json FROM pet_media_event WHERE client_action_id='end'").fetchone()[0])
        assert payload["received_monotonic_ms"] == 2000
