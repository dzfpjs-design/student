"""Playback-only durability on synthetic, disposable databases."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import subprocess
import sys

import pytest

from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.tutor_blackboard import parse_tutor_blackboard_response
from home_study_agent.tutor_playback import TutorPlaybackError, TutorPlaybackService


CHILD = "child-primary-4"
OTHER = "child-junior-3"


def playback_flow(tmp_path, *, pending_question=False):
    db = Database(tmp_path / "playback.db")
    db.initialize()
    repo = StudyRepository(db)
    repo.seed_family()
    sid = repo.start_session(CHILD, "playback-session")["session_id"]
    if pending_question:
        from home_study_agent.diagnostic_prompt import DiagnosticPromptService
        from home_study_agent.learning_state import LearningStateService
        LearningStateService(db).seed_catalog()
        prepared = DiagnosticPromptService(db).prepare(sid, track='school',
            preferred_item_id='diag-p4-arithmetic-closed-1', client_action_id='polite-original-question')
        assert prepared['prompt'], prepared
    answer, board = parse_tutor_blackboard_response(json.dumps({"blackboard": {
        "title": "Synthetic explanation", "segments": [
            {"narration": "First step", "blocks": [{"kind": "text", "text": "One"}]},
            {"narration": "Second step", "blocks": [{"kind": "question", "text": "Why?"}]},
        ]}}), "math", 1)
    turn = repo.save_tutor_turn(sid, None, "math", "Synthetic question", answer,
        "test", "test", "synthetic-tutor-turn", output_envelope={"result": {"blackboard": board}})
    return db, TutorPlaybackService(db), sid, turn, board


@pytest.fixture
def flow(tmp_path):
    return playback_flow(tmp_path)


def begin(flow, **kwargs):
    _, service, sid, turn, _ = flow
    return service.begin(sid, turn_id=turn["turn_id"], client_action_id="begin-playback", **kwargs)


def binding(view):
    return {"playback_id": view["playback_id"], "source_version": view["source_version"],
            "generation": view["generation"], "expected_revision": view["revision"]}


def control(flow, view, command, action=None):
    return flow[1].control(flow[2], **binding(view), command=command,
                           client_action_id=action or "control-" + command)


def segment(flow, view, event, action=None, segment_id=None):
    return flow[1].record_segment(flow[2], **binding(view), event=event,
        segment_id=segment_id or view["presentation"]["segments"][view["segment_index"]]["segment_id"],
        client_action_id=action or f"segment-{event}-{view['segment_index']}")


def change(db, sql, args=()):
    with db.connect() as c:
        c.execute(sql, args)
        c.commit()


def count(db, table):
    with db.connect() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def appended(board):
    result = deepcopy(board)
    result["source"] = "model_plus_local_diagnostic"
    result["segments"].extend([
        {"segment_id": "local-course-example", "narration": "Local reference",
         "duration_hint_ms": 9000, "blocks": [{"kind": "text", "text": "Private local excerpt", "tone": "chalk"}]},
        {"segment_id": "local-diagnostic-example", "narration": "Explain in your own words",
         "duration_hint_ms": 5000, "blocks": [{"kind": "question", "text": "Why?", "tone": "yellow"}]},
    ])
    return result


def test_begin_paused_and_exact_replay_persists_controlled_snapshot(flow):
    board = appended(flow[4])
    first = begin(flow, presentation=board)
    assert first["status"] == "paused" and first["segment_index"] == 0
    assert first["segment_started"] is False and first["evidence_kind"] == "playback_only"
    assert first["source_version"] != first["presentation_version"]
    assert first["presentation"] == board
    duplicate = begin(flow, presentation=board)
    assert duplicate["revision"] == first["revision"] and duplicate["replayed"]
    restarted = TutorPlaybackService(Database(flow[0].path))
    assert restarted.read(flow[2])["presentation"] == board
    assert count(flow[0], "tutor_playback_checkpoint") == 1
    assert count(flow[0], "tutor_playback_event") == 1
    with flow[0].connect() as c:
        canonical = json.loads(c.execute("SELECT output_envelope_json FROM tutor_turn").fetchone()[0])
    assert canonical["result"]["blackboard"] == flow[4]


def test_callbacks_are_sequential_and_never_learning_evidence(flow):
    tables = ("diagnostic_attempt", "tutor_outcome", "audit_event", "session_event")
    with flow[0].connect() as c:
        available = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    before = {t: count(flow[0], t) for t in tables if t in available}
    view = begin(flow)
    with pytest.raises(TutorPlaybackError, match="NOT_PLAYING"):
        segment(flow, view, "started")
    view = control(flow, view, "resume")
    with pytest.raises(TutorPlaybackError, match="SEGMENT_ORDER"):
        segment(flow, view, "ended")
    with pytest.raises(TutorPlaybackError, match="SEGMENT_ORDER"):
        segment(flow, view, "started", segment_id="segment-2")
    for index in range(2):
        view = segment(flow, view, "started")
        view = segment(flow, view, "ended")
        assert view["segment_index"] == index + 1
    assert view["status"] == "playback_finished"
    assert {t: count(flow[0], t) for t in before} == before
    with flow[0].connect() as c:
        assert c.execute("SELECT state FROM daily_session WHERE session_id=?", (flow[2],)).fetchone()[0] != "completed"


def test_lost_end_receipt_replays_once_but_payload_conflicts(flow):
    view = segment(flow, control(flow, begin(flow), "resume"), "started")
    first = segment(flow, view, "ended", action="end-receipt")
    replay = segment(flow, view, "ended", action="end-receipt")
    assert replay["replayed"] and replay["revision"] == first["revision"]
    assert replay["segment_index"] == 1
    with pytest.raises(TutorPlaybackError, match="PAYLOAD_CONFLICT"):
        segment(flow, view, "started", action="end-receipt")
    with pytest.raises(TutorPlaybackError, match="PAYLOAD_CONFLICT"):
        begin(flow, presentation=appended(flow[4]))


@pytest.mark.parametrize("command,index,status", [
    ("pause", 1, "paused"), ("resume", 1, "playing"), ("previous", 0, "paused"),
    ("restart", 0, "paused"), ("cancel", 1, "cancelled"),
])
def test_controls_fence_old_end_and_clear_inflight_segment(flow, command, index, status):
    view = segment(flow, control(flow, begin(flow), "resume"), "started")
    view = segment(flow, view, "ended")
    old = segment(flow, view, "started")
    new = control(flow, old, command, action="new-generation")
    assert new["generation"] == old["generation"] + 1
    assert (new["segment_index"], new["status"], new["segment_started"]) == (index, status, False)
    with pytest.raises(TutorPlaybackError, match="STALE_GENERATION"):
        segment(flow, old, "ended")
    replay = control(flow, old, command, action="new-generation")
    assert replay["replayed"] and replay["revision"] == new["revision"]


def test_old_successful_end_replay_cannot_advance_or_restart_new_generation(flow):
    started = segment(flow, control(flow, begin(flow), "resume"), "started")
    ended = segment(flow, started, "ended")
    fresh = control(flow, ended, "restart")
    with pytest.raises(TutorPlaybackError, match="STALE_GENERATION"):
        segment(flow, started, "ended")
    assert flow[1].read(flow[2])["revision"] == fresh["revision"]


def test_real_new_process_read_pauses_inflight_and_requires_new_start(flow):
    old = segment(flow, control(flow, begin(flow), "resume"), "started")
    code = """import json, sys
from home_study_agent.database import Database
from home_study_agent.tutor_playback import TutorPlaybackService
print(json.dumps(TutorPlaybackService(Database(sys.argv[1])).read(sys.argv[2])))
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run([sys.executable, "-c", code, str(flow[0].path), flow[2]],
                            env=env, capture_output=True, text=True, check=True)
    recovered = json.loads(result.stdout)
    assert recovered["status"] == "paused" and recovered["reason"] == "runtime_restarted"
    assert recovered["segment_index"] == old["segment_index"]
    assert not recovered["segment_started"] and recovered["generation"] > old["generation"]
    with pytest.raises(TutorPlaybackError, match="STALE_GENERATION"):
        segment(flow, old, "ended")


def test_runtime_epoch_recovery_commits_even_when_first_access_is_stale_callback(flow):
    old = segment(flow, control(flow, begin(flow), "resume"), "started")
    new_service = TutorPlaybackService(flow[0], runtime_epoch="new-boot")
    new_flow = (flow[0], new_service, *flow[2:])
    with pytest.raises(TutorPlaybackError, match="STALE_GENERATION"):
        segment(new_flow, old, "ended")
    with flow[0].connect() as c:
        row = c.execute("SELECT * FROM tutor_playback_checkpoint").fetchone()
    assert row["status"] == "paused" and row["generation"] > old["generation"]
    read = new_service.read(flow[2])
    resumed = control(new_flow, read, "resume", action="wake-after-restart")
    with pytest.raises(TutorPlaybackError, match="SEGMENT_ORDER"):
        segment(new_flow, resumed, "ended", action="missing-restart-start")
    assert segment(new_flow, segment(new_flow, resumed, "started", action="fresh-start"),
                   "ended", action="fresh-end")["segment_index"] == 1


def test_simultaneous_events_compare_and_swap_only_once(flow):
    view = control(flow, begin(flow), "resume")
    def start(key):
        try:
            return segment(flow, view, "started", action=key)
        except TutorPlaybackError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, ["concurrent-a", "concurrent-b"]))
    assert len([r for r in results if isinstance(r, dict)]) == 1
    assert "REVISION_CONFLICT" in results
    assert flow[1].read(flow[2])["revision"] == view["revision"] + 1


@pytest.mark.parametrize("sql", [
    "UPDATE tutor_turn SET status='withdrawn'",
    "UPDATE tutor_turn SET answer='Changed source'",
    "UPDATE tutor_turn SET safety_status='blocked'",
    "UPDATE tutor_turn SET output_envelope_json='{}'",
])
def test_source_revision_or_withdrawal_invalidates_even_receipt_replay(flow, sql):
    old = begin(flow)
    change(flow[0], sql)
    read = flow[1].read(flow[2], playback_id=old["playback_id"])
    assert read["status"] == "invalidated" and read["presentation"] is None
    with pytest.raises(TutorPlaybackError, match="SOURCE_INVALIDATED"):
        control(flow, old, "resume")
    with pytest.raises(TutorPlaybackError, match="SOURCE_INVALIDATED"):
        begin(flow)


@pytest.mark.parametrize("mutation", ["other_child", "other_session", "inactive_child", "ended", "terminal"])
def test_child_session_liveness_and_receipt_isolation(flow, mutation):
    view = begin(flow)
    db, service, sid, turn, _ = flow
    if mutation in {"other_child", "other_session"}:
        change(db, "UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
        sid = StudyRepository(db).start_session(OTHER if mutation == "other_child" else CHILD,
                                                "other-playback-session")["session_id"]
    elif mutation == "inactive_child":
        change(db, "UPDATE child_profile SET status='archived' WHERE child_id=?", (CHILD,))
    elif mutation == "ended":
        change(db, "UPDATE daily_session SET status='completed' WHERE session_id=?", (sid,))
    else:
        change(db, "UPDATE daily_session SET state='evolving' WHERE session_id=?", (sid,))
    for call in (
        lambda: service.read(sid, playback_id=view["playback_id"]),
        lambda: service.begin(sid, turn_id=turn["turn_id"], client_action_id="begin-playback"),
        lambda: service.control(sid, **binding(view), command="resume", client_action_id="foreign-resume"),
    ):
        with pytest.raises(TutorPlaybackError):
            call()


@pytest.mark.parametrize("mutation", ["prefix", "insert", "duplicate", "source", "url", "kind", "huge", "extra", "empty"])
def test_untrusted_presentation_does_not_modify_canonical_or_create_checkpoint(flow, mutation):
    board = appended(flow[4])
    if mutation == "prefix":
        board["segments"][0]["narration"] = "Forged canonical"
    elif mutation == "insert":
        board["segments"].insert(0, board["segments"].pop())
    elif mutation == "duplicate":
        board["segments"][-1]["segment_id"] = board["segments"][0]["segment_id"]
    elif mutation == "source":
        board["source"] = "trusted_diagnostic_evidence"
    elif mutation == "url":
        board["segments"][-1]["audio_url"] = "https://invalid.example/audio"
    elif mutation == "kind":
        board["segments"][-1]["blocks"][0]["kind"] = "html"
    elif mutation == "huge":
        board["segments"][-1]["narration"] = "x" * 9000
    elif mutation == "extra":
        board["raw_frames"] = ["no"]
    else:
        board["segments"] = []
    with pytest.raises(TutorPlaybackError, match="PRESENTATION"):
        begin(flow, presentation=board)
    assert count(flow[0], "tutor_playback_checkpoint") == 0


def test_missing_board_does_not_invent_waiting_or_segments(flow):
    change(flow[0], "UPDATE tutor_turn SET output_envelope_json=NULL")
    assert flow[1].read(flow[2]) is None
    with pytest.raises(TutorPlaybackError, match="SOURCE"):
        begin(flow)
    assert count(flow[0], "tutor_playback_checkpoint") == 0


def test_replacement_requires_explicit_cancel_and_old_id_stays_cancelled(flow):
    first = begin(flow)
    with pytest.raises(TutorPlaybackError, match="ACTIVE_PLAYBACK_EXISTS"):
        flow[1].begin(flow[2], turn_id=flow[3]["turn_id"], client_action_id="other-begin")
    cancelled = control(flow, first, "cancel")
    later = flow[1].begin(flow[2], turn_id=flow[3]["turn_id"], client_action_id="other-begin")
    assert later["playback_id"] != first["playback_id"]
    assert flow[1].read(flow[2])["playback_id"] == later["playback_id"]
    assert flow[1].read(flow[2], playback_id=first["playback_id"])["status"] == "cancelled"
    with pytest.raises(TutorPlaybackError, match="TERMINAL_PLAYBACK"):
        control(flow, cancelled, "resume")


def test_cascade_removes_playback_only_tables_with_source(flow):
    begin(flow)
    change(flow[0], "DELETE FROM tutor_turn")
    assert count(flow[0], "tutor_playback_checkpoint") == 0
    assert count(flow[0], "tutor_playback_event") == 0


@pytest.mark.parametrize("field,value", [("generation", True), ("expected_revision", -1),
                                         ("generation", 1.0), ("source_version", "forged")])
def test_forged_or_weak_binding_cannot_start(flow, field, value):
    view = begin(flow)
    args = binding(view)
    args[field] = value
    with pytest.raises(TutorPlaybackError):
        flow[1].control(flow[2], **args, command="resume", client_action_id="bad-binding")
    assert flow[1].read(flow[2])["revision"] == view["revision"]


@pytest.mark.parametrize("field,value", [("raw_frames", ["not retained"]), ("audio_url", "private"),
                                         ("unexpected", {"payload": "not retained"})])
def test_server_board_is_still_a_controlled_snapshot_not_arbitrary_json(flow, field, value):
    board = deepcopy(flow[4])
    board[field] = value
    change(flow[0], "UPDATE tutor_turn SET output_envelope_json=?",
           (json.dumps({"result": {"blackboard": board}}),))
    with pytest.raises(TutorPlaybackError, match="SOURCE_PRESENTATION"):
        begin(flow)
    assert count(flow[0], "tutor_playback_checkpoint") == 0


def test_delivery_event_write_failure_rolls_back_checkpoint(flow):
    import sqlite3

    view = control(flow, begin(flow), "resume")
    change(flow[0], """CREATE TRIGGER fail_playback_event BEFORE INSERT ON tutor_playback_event
        BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="synthetic disk failure"):
        segment(flow, view, "started")
    assert flow[1].read(flow[2])["revision"] == view["revision"]
    assert flow[1].read(flow[2])["segment_started"] is False
    change(flow[0], "DROP TRIGGER fail_playback_event")
    assert segment(flow, view, "started")["segment_started"] is True


def test_duplicate_concurrent_end_has_one_durable_receipt(flow):
    view = segment(flow, control(flow, begin(flow), "resume"), "started")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: segment(flow, view, "ended", action="same-end"), range(2)))
    assert sorted(r["replayed"] for r in results) == [False, True]
    assert {r["revision"] for r in results} == {view["revision"] + 1}
    assert {r["segment_index"] for r in results} == {1}


@pytest.mark.parametrize("field,value", [("kind", []), ("tone", {}), ("text", 42)])
def test_malformed_local_blocks_are_contract_errors_not_server_errors(flow, field, value):
    board = appended(flow[4])
    board["segments"][-1]["blocks"][0][field] = value
    with pytest.raises(TutorPlaybackError, match="PRESENTATION"):
        begin(flow, presentation=board)


def test_read_does_not_infer_progress_or_start_playback_from_duration_hints(flow):
    first = begin(flow)
    for _ in range(3):
        assert flow[1].read(flow[2]) == first
    playing = control(flow, first, "resume")
    for _ in range(3):
        assert flow[1].read(flow[2]) == playing
    assert playing["segment_index"] == 0 and not playing["segment_started"]


def test_finished_playback_can_only_replay_through_explicit_control(flow):
    view = control(flow, begin(flow), "resume")
    for _ in range(2):
        view = segment(flow, segment(flow, view, "started"), "ended")
    with pytest.raises(TutorPlaybackError, match="PLAYBACK_FINISHED"):
        control(flow, view, "resume", action="resume-finished")
    previous = control(flow, view, "previous")
    assert (previous["status"], previous["segment_index"]) == ("paused", 1)
    restarted = control(flow, previous, "restart")
    assert (restarted["status"], restarted["segment_index"]) == ("paused", 0)


def test_same_boot_service_instances_share_generation_but_new_tab_resume_fences_old(flow):
    old = segment(flow, control(flow, begin(flow), "resume"), "started")
    other = TutorPlaybackService(flow[0])
    assert other.read(flow[2])["generation"] == old["generation"]
    new = other.control(flow[2], **binding(old), command="resume", client_action_id="other-tab-resume")
    assert new["generation"] == old["generation"] + 1 and not new["segment_started"]
    with pytest.raises(TutorPlaybackError, match="STALE_GENERATION"):
        segment(flow, old, "ended")


def test_finished_historical_checkpoint_cannot_replace_another_open_playback(flow):
    old = control(flow, begin(flow), "resume")
    for _ in range(2):
        old = segment(flow, segment(flow, old, "started"), "ended")
    newer = flow[1].begin(flow[2], turn_id=flow[3]["turn_id"], client_action_id="new-presentation")
    with pytest.raises(TutorPlaybackError, match="ACTIVE_PLAYBACK_EXISTS"):
        control(flow, old, "previous")
    assert flow[1].read(flow[2])["playback_id"] == newer["playback_id"]


@pytest.mark.parametrize("model_output", [
    "A degraded explanation.",
    '{"blackboard":{"segments":[{"speech":"Repaired speech","blocks":[{"kind":"bullet","items":["First"]}]}]}}',
    json.dumps({"blackboard": {"title": "All blocks", "segments": [
        {"narration": "All shapes", "blocks": [
            {"kind": "formula", "latex": "a+b=c"}, {"kind": "steps", "items": ["First", "Next"]},
            {"kind": "table", "columns": ["A"], "rows": [["B"]]},
            {"kind": "relation", "nodes": ["A", "B"]},
        ]}]}}),
])
def test_existing_parser_degraded_repaired_and_rich_boards_are_compatible(flow, model_output):
    _, board = parse_tutor_blackboard_response(model_output, "math", 1)
    change(flow[0], "UPDATE tutor_turn SET output_envelope_json=?",
           (json.dumps({"result": {"blackboard": board}}),))
    assert begin(flow)["presentation"] == board


def test_read_invalidated_source_never_revives_when_original_bytes_return(flow):
    original = begin(flow)
    change(flow[0], "UPDATE tutor_turn SET status='withdrawn'")
    assert flow[1].read(flow[2])["status"] == "invalidated"
    change(flow[0], "UPDATE tutor_turn SET status='completed'")
    assert flow[1].read(flow[2])["status"] == "invalidated"
    with pytest.raises(TutorPlaybackError, match="SOURCE_INVALIDATED"):
        control(flow, original, "resume")
    new = flow[1].begin(flow[2], turn_id=flow[3]["turn_id"], client_action_id="explicit-new-source-presentation")
    assert new["status"] == "paused" and new["playback_id"] != original["playback_id"]
