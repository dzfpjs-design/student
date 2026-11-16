from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import json

import pytest

from home_study_agent.database import Database
from home_study_agent.document_monitoring import DocumentMonitoringService
from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
from home_study_agent.repository import utc_now


def _service(tmp_path: Path) -> tuple[Database, DocumentMonitoringService]:
    database = Database(tmp_path / "study.db")
    database.initialize()
    now = utc_now()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at) VALUES('child','学生','初三','江苏南京','active',?,?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id) VALUES('study','child','active','study',?,?,?)",
            (now, now, "document-monitor-session"),
        )
        connection.commit()
    return database, DocumentMonitoringService(database)


def _artifact(database: Database, artifact_id: str, text: str, confidence: float = .91) -> None:
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO capture_artifact(
                 artifact_id,child_id,session_id,artifact_type,local_path,extracted_text,
                 confidence,details_json,status,created_at,client_action_id
               ) VALUES(?,?,?,'document_ocr',NULL,?,?,'{}','completed',?,?)""",
            (artifact_id, "child", "study", text, confidence, utc_now(), f"ocr-{artifact_id}"),
        )
        connection.commit()


def test_question_backfill_tolerates_first_start_before_core_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "first-start.db")

    DocumentMonitoringService(database)

    with database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observed_question'"
        ).fetchone()


def test_preview_can_start_during_check_in_but_learning_evidence_waits_for_study(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET state='check_in' WHERE session_id='study'")
        connection.commit()

    started = service.start('study', 'preview-during-check-in')

    assert started['status'] == 'active'
    with pytest.raises(ValueError, match='active study session'):
        service.record_event('study', 'stable', .9, {}, 'no-early-learning-evidence')


def test_preview_cannot_resume_after_daily_session_completed(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    service.start('study', 'start-preview')
    service.pause('study', 'pause-preview')
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed',state='completed',ended_at=? WHERE session_id='study'", (utc_now(),))
        connection.commit()

    with pytest.raises(ValueError, match='active study session'):
        service.resume('study', 'resume-after-completion')


@pytest.mark.parametrize('kind,expected', [('cross_like', 1), ('tick_like', 0), ('unbounded', 0), ('ungrounded', 0)])
def test_revision_preserves_only_grounded_marked_review_count(tmp_path, kind, expected):
    database, service = _service(tmp_path)
    service.start('study', 'start-marked')
    _artifact(database, 'marked', '1. First question\n2. Second question')
    candidate = {'text': '2. Second question', 'mark_kind': kind, 'confidence': .65,
                 'requires_child_confirmation': True, 'correctness': 'unverified'}
    if kind == 'unbounded':
        candidate['needs_manual_boundary_check'] = True
    elif kind == 'ungrounded':
        candidate['text'] = '3. A question from another page'
    with database.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?',
            (json.dumps({'local_pipeline': {'wrong_question_candidates': [candidate]}}), 'marked'))
        c.commit()
    revision = service.record_revision('study', 'marked', 'marked-page', 'marked-revision')
    assert revision['marked_question_count'] == expected
    assert DocumentMonitoringService(database).status('study')['latest_revision']['marked_question_count'] == expected
    assert 'details_json' not in revision and 'artifact_details' not in revision


@pytest.mark.parametrize('change', ['text', 'owner', 'json', 'box'])
def test_stale_or_malformed_marker_metadata_does_not_open_mistake_review(tmp_path, change):
    database, service = _service(tmp_path)
    service.start('study', 'start-stale-marked')
    _artifact(database, 'marked', '2. Original question')
    candidate = {'text': '2. Original question', 'mark_kind': 'cross_like', 'confidence': .6}
    details = json.dumps({'local_pipeline': {'wrong_question_candidates': [candidate]}})
    with database.connect() as c:
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (details, 'marked'))
        c.commit()
    assert service.record_revision('study', 'marked', 'page', 'revision')['marked_question_count'] == 1
    with database.connect() as c:
        if change == 'text':
            c.execute("UPDATE capture_artifact SET extracted_text='different page' WHERE artifact_id='marked'")
        elif change == 'owner':
            c.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
                SELECT 'other','Synthetic other',grade,curriculum_region,status,created_at,updated_at FROM child_profile LIMIT 1""")
            c.execute("UPDATE capture_artifact SET child_id='other' WHERE artifact_id='marked'")
        else:
            if change == 'box':
                candidate['bbox_xywh'] = [float('inf'), 0, 30, 20]
                details = json.dumps({'local_pipeline': {'wrong_question_candidates': [candidate]}})
            else:
                details = '{broken'
            c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (details, 'marked'))
        c.commit()
    revision = service.status('study')['latest_revision']
    assert revision['marked_question_count'] == 0
    assert 'artifact_child' not in revision and 'artifact_session' not in revision


def _vision_requests(database: Database) -> None:
    with database.connect() as connection:
        connection.execute("""CREATE TABLE document_vision_request (
            child_id TEXT NOT NULL, request_id TEXT NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY(child_id, request_id))""")
        connection.commit()


def _vision_artifact(database: Database, artifact_id: str, request_id: str) -> None:
    _artifact(database, artifact_id, "Local OCR")
    with database.connect() as connection:
        connection.execute(
            "UPDATE capture_artifact SET details_json=? WHERE artifact_id=?",
            (json.dumps({"cloud_vision_fallback": {"request_id": request_id}}), artifact_id),
        )
        connection.commit()


@pytest.mark.parametrize("request_status", ["reserved", "succeeded", "failed"])
def test_status_counts_linked_vision_requests_once_and_after_restart(tmp_path, request_status):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _vision_requests(database)
    for artifact_id in ("page-1", "page-2"):
        _vision_artifact(database, artifact_id, "vision-request")
    with database.connect() as connection:
        connection.execute("INSERT INTO document_vision_request VALUES('child', 'vision-request', ?)",
                           (request_status,))
        connection.commit()
    for current in (service, DocumentMonitoringService(database)):
        status = current.status("study")
        assert status["cloud_image_calls"] == 1
        assert status["raw_frames_stored"] is False


@pytest.mark.parametrize("with_table", [False, True])
def test_status_does_not_count_vision_fallback_without_request_row(tmp_path, with_table):
    database, service = _service(tmp_path)
    service.start("study", "start")
    if with_table:
        _vision_requests(database)
    _vision_artifact(database, "blocked-page", "blocked-before-reservation")
    status = service.status("study")
    assert status["cloud_image_calls"] == 0
    assert status["raw_frames_stored"] is False


@pytest.mark.parametrize("same_child", [False, True])
def test_status_vision_count_is_bound_to_artifact_child_and_session(tmp_path, same_child):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _vision_requests(database)
    _vision_artifact(database, "original-page", "shared-request")
    _other_session(database, service, same_child)
    child_id = "child" if same_child else "other-child"
    _vision_artifact(database, "other-page", "other-request")
    with database.connect() as connection:
        connection.execute("UPDATE capture_artifact SET child_id=?,session_id='other-study' WHERE artifact_id='other-page'",
                           (child_id,))
        connection.executemany("INSERT INTO document_vision_request VALUES(?, ?, 'succeeded')",
                               [("child", "shared-request"), (child_id, "other-request"),
                                (child_id, "unlinked-request")])
        if not same_child:
            connection.execute("INSERT INTO document_vision_request VALUES('other-child', 'shared-request', 'failed')")
        connection.commit()
    assert service.status("study")["cloud_image_calls"] == 1
    assert service.status("other-study")["cloud_image_calls"] == 1


def test_status_counts_vision_requests_with_legacy_or_unrelated_artifact_details(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _vision_requests(database)
    for index, details in enumerate(("{}", "null", "[]", "not-json", '{"cloud_vision_fallback":null}')):
        artifact_id = f"legacy-{index}"
        _artifact(database, artifact_id, "Local OCR")
        with database.connect() as connection:
            connection.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id=?", (details, artifact_id))
            connection.commit()
    for request_status in ("reserved", "succeeded", "failed"):
        _vision_artifact(database, request_status, request_status)
        with database.connect() as connection:
            connection.execute("INSERT INTO document_vision_request VALUES('child', ?, ?)",
                               (request_status, request_status))
            connection.commit()
    assert service.status("study")["cloud_image_calls"] == 3


def test_document_monitor_persists_state_events_and_resumes_after_restart(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    started = service.start("study", "monitor-start")
    event = service.record_event(
        "study", "writing", .84,
        {"motion_score": 12.4, "skin_ratio": .08, "raw_frame_stored": False},
        "monitor-event-writing",
    )
    assert event["event_type"] == "writing"
    assert event["metrics"]["raw_frame_stored"] is False

    reopened = DocumentMonitoringService(database).start("study", "monitor-start-replay")
    assert reopened["monitor_id"] == started["monitor_id"]
    assert reopened["status"] == "active"
    assert reopened["latest_event"]["event_type"] == "writing"


def test_page_revisions_reference_local_ocr_and_compute_text_delta(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    service.start("study", "monitor-start")
    _artifact(database, "page-1", "第一题 12 + 8 = 20\n第二题")
    _artifact(database, "page-2", "第一题 12 + 8 = 20\n第二题 36 ÷ 4 = 9")

    first = service.record_revision("study", "page-1", "sig-1", "revision-1")
    second = service.record_revision("study", "page-2", "sig-2", "revision-2")
    assert first["revision_index"] == 1
    assert second["revision_index"] == 2
    assert second["previous_revision_id"] == first["revision_id"]
    assert second["change_summary"]["added_lines"] == ["第二题 36 ÷ 4 = 9"]
    assert second["raw_image_transmitted"] is False


def test_continuous_questions_are_deduplicated_and_marked_items_queue_once_across_restart(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    service.start("study", "monitor-start")
    text = "1. 光在空气中怎样传播？\n2. 下列说法正确的是（ ）"
    details = {
        "local_pipeline": {
            "question_blocks": [
                {"text": "1. 光在空气中怎样传播？", "block_kind": "question",
                 "needs_manual_boundary_check": False, "confidence": .93, "bbox_xywh": [10, 10, 300, 80]},
                {"text": "2. 下列说法正确的是（ ）", "block_kind": "question",
                 "needs_manual_boundary_check": False, "confidence": .91, "bbox_xywh": [10, 100, 300, 80]},
            ],
            "wrong_question_candidates": [
                {"text": "2. 下列说法正确的是（ ）", "mark_kind": "cross_like",
                 "confidence": .82, "bbox_xywh": [10, 100, 300, 80],
                 "requires_child_confirmation": True, "correctness": "unverified"},
            ],
        }
    }
    for artifact_id in ("physics-page-1", "physics-page-2"):
        _artifact(database, artifact_id, text)
        with database.connect() as connection:
            connection.execute("UPDATE capture_artifact SET details_json=? WHERE artifact_id=?",
                               (json.dumps(details, ensure_ascii=False), artifact_id))
            connection.commit()

    first = service.record_revision("study", "physics-page-1", "same-page-a", "revision-physics-a")
    second = DocumentMonitoringService(database).record_revision(
        "study", "physics-page-2", "same-page-b", "revision-physics-b")

    assert first["observed_question_count"] == second["observed_question_count"] == 2
    assert first["marked_question_count"] == second["marked_question_count"] == 1
    assert first["queued_mistake_candidate_count"] == second["queued_mistake_candidate_count"] == 1
    with database.connect() as connection:
        questions = connection.execute(
            "SELECT occurrence_count,marked_review_count FROM observed_question ORDER BY canonical_text"
        ).fetchall()
        candidates = connection.execute(
            "SELECT question_text,status FROM mistake_capture_candidate WHERE child_id='child'"
        ).fetchall()
    assert len(questions) == 2
    assert [row["occurrence_count"] for row in questions] == [2, 2]
    assert sorted(row["marked_review_count"] for row in questions) == [0, 2]
    assert [(row["question_text"], row["status"]) for row in candidates] == [
        ("2. 下列说法正确的是（ ）", "pending")
    ]
    assert DocumentMonitoringService(database).status("study")["latest_revision"]["observed_question_count"] == 2


def test_restart_backfills_preexisting_ocr_revisions_without_waiting_for_page_change(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    service.start("study", "monitor-start")
    text = "1. 光在空气中怎样传播？\n2. 下列说法正确的是（ ）"
    details = {
        "local_pipeline": {
            "question_blocks": [
                {"text": line, "block_kind": "question", "needs_manual_boundary_check": False,
                 "confidence": .92, "bbox_xywh": [10, 10 + index * 90, 300, 80]}
                for index, line in enumerate(text.splitlines())
            ],
            "wrong_question_candidates": [
                {"text": "2. 下列说法正确的是（ ）", "mark_kind": "cross_like",
                 "confidence": .82, "bbox_xywh": [10, 100, 300, 80],
                 "requires_child_confirmation": True, "correctness": "unverified"},
            ],
        }
    }
    _artifact(database, "legacy-physics-page", text)
    with database.connect() as connection:
        connection.execute(
            "UPDATE capture_artifact SET details_json=? WHERE artifact_id=?",
            (json.dumps(details, ensure_ascii=False), "legacy-physics-page"),
        )
        connection.commit()
    revision = service.record_revision(
        "study", "legacy-physics-page", "legacy-signature", "legacy-revision"
    )
    with database.connect() as connection:
        connection.execute("DELETE FROM observed_question WHERE child_id='child'")
        connection.execute("DELETE FROM mistake_capture_candidate WHERE child_id='child'")
        connection.commit()

    restarted = DocumentMonitoringService(database)
    recovered = restarted.status("study")["latest_revision"]
    assert recovered["revision_id"] == revision["revision_id"]
    assert recovered["observed_question_count"] == 2
    assert recovered["marked_question_count"] == 1
    assert recovered["queued_mistake_candidate_count"] == 1

    DocumentMonitoringService(database)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM observed_question").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM observed_question_occurrence").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM mistake_capture_candidate").fetchone()[0] == 1
    privacy = PrivacyLifecycleService(database, tmp_path)
    privacy.initialize()
    exported = json.loads(Path(privacy.export_child("child")["path"]).read_text(encoding="utf-8"))
    assert len(exported["observed_questions"]) == 2
    assert len(exported["observed_question_occurrences"]) == 2


def test_privacy_gated_cloud_blocks_are_grouped_by_question_number_not_as_fragments(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    service.start("study", "monitor-start")
    _artifact(database, "cloud-page", "unreliable local OCR", confidence=.32)
    cloud = {
        "status": "needs_review",
        "privacy": {"original_frame_uploaded": False, "uploaded": "page_region_binary_ink"},
        "blocks": [
            {"kind": "text", "text": "1. 第一题题干（ ）", "confidence": .62},
            {"kind": "formula", "text": "A. x=1", "confidence": .55},
            {"kind": "formula", "text": "B. x=2", "confidence": .55},
            {"kind": "formula", "text": "2. 第二题题干（ ）", "confidence": .58},
            {"kind": "formula", "text": "A. y=1", "confidence": .51},
            {"kind": "formula", "text": "B. y=2", "confidence": .51},
        ],
    }
    with database.connect() as connection:
        connection.execute(
            "UPDATE capture_artifact SET details_json=? WHERE artifact_id='cloud-page'",
            (json.dumps({"local_pipeline": {"question_blocks": []}, "cloud_vision_fallback": cloud}, ensure_ascii=False),),
        )
        connection.commit()

    revision = service.record_revision("study", "cloud-page", "cloud-signature", "cloud-revision")
    assert revision["observed_question_count"] == 2
    with database.connect() as connection:
        questions = [row[0] for row in connection.execute(
            "SELECT canonical_text FROM observed_question ORDER BY canonical_text"
        )]
    assert questions == [
        "1. 第一题题干（ ）\nA. x=1\nB. x=2",
        "2. 第二题题干（ ）\nA. y=1\nB. y=2",
    ]


def test_cloud_blocks_are_rejected_when_original_frame_privacy_gate_is_not_proven(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    service.start("study", "monitor-start")
    _artifact(database, "unsafe-cloud-page", "unreliable local OCR", confidence=.32)
    with database.connect() as connection:
        connection.execute(
            "UPDATE capture_artifact SET details_json=? WHERE artifact_id='unsafe-cloud-page'",
            (json.dumps({"cloud_vision_fallback": {
                "status": "needs_review", "privacy": {"original_frame_uploaded": True},
                "blocks": [{"kind": "text", "text": "1. 不应进入题库", "confidence": .9}],
            }}, ensure_ascii=False),),
        )
        connection.commit()
    revision = service.record_revision(
        "study", "unsafe-cloud-page", "unsafe-signature", "unsafe-cloud-revision"
    )
    assert revision["observed_question_count"] == 0


@pytest.mark.parametrize('interruption', ['no_observation', 'occluded', 'pause_resume', 'different_page'])
def test_unchanged_ocr_does_not_prove_continuous_page_observation(tmp_path: Path, interruption: str) -> None:
    database, service = _service(tmp_path)
    service.start("study", "monitor-start")
    _artifact(database, "page-baseline", "第3题 x + 4 = 9")
    _artifact(database, "page-unchanged", "第3题 x + 4 = 9")

    first = service.record_revision("study", "page-baseline", "sig-baseline", "revision-baseline")
    assert first["study_signal"]["kind"] == "baseline"
    with database.connect() as connection:
        connection.execute(
            "UPDATE document_page_revision SET created_at = datetime('now', '-10 minutes') WHERE revision_id = ?",
            (first["revision_id"],),
        )
        connection.commit()

    if interruption == 'occluded':
        service.record_event('study', 'occluded', .9, {}, 'occluded-event')
    elif interruption == 'pause_resume':
        service.pause('study', 'pause-monitor')
        service.resume('study', 'resume-monitor')
    signature = 'sig-other-page' if interruption == 'different_page' else 'sig-baseline'
    second = service.record_revision("study", "page-unchanged", signature, "revision-unchanged")
    assert second["change_summary"]["changed"] is False
    assert second["study_signal"]["kind"] == "observation_incomplete"
    assert second["study_signal"]["may_prompt_child"] is False
    assert "stagnant_seconds" not in second["study_signal"]
    assert "无法" in second["study_signal"]["reason"]


def test_monitor_rejects_cross_session_or_non_ocr_artifacts(tmp_path: Path) -> None:
    database, service = _service(tmp_path)
    service.start("study", "monitor-start")
    with database.connect() as connection:
        connection.execute(
            """INSERT INTO capture_artifact(
                 artifact_id,child_id,session_id,artifact_type,local_path,extracted_text,
                 confidence,details_json,status,created_at,client_action_id
               ) VALUES('audio','child','study','voice_input',NULL,'text',.9,'{}','completed',?,'audio-action')""",
            (utc_now(),),
        )
        connection.commit()
    try:
        service.record_revision("study", "audio", "sig", "bad-revision")
    except ValueError as error:
        assert "OCR artifact" in str(error)
    else:
        raise AssertionError("non-OCR artifact was accepted")


def test_pause_and_stop_are_durable_and_events_require_active_monitor(tmp_path: Path) -> None:
    _, service = _service(tmp_path)
    service.start("study", "monitor-start")
    assert service.pause("study", "monitor-pause")["status"] == "paused"
    try:
        service.record_event("study", "stable", .9, {}, "event-while-paused")
    except ValueError as error:
        assert "not active" in str(error)
    else:
        raise AssertionError("paused monitor accepted an event")
    assert service.resume("study", "monitor-resume")["status"] == "active"
    assert service.stop("study", "monitor-stop")["status"] == "stopped"


def _history(database):
    with database.connect() as connection:
        return {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("document_monitor_session", "document_monitor_event", "document_page_revision", "document_monitor_request")
        }


def _other_session(database, service, same_child=False):
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed' WHERE session_id='study'")
        if not same_child:
            connection.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
                SELECT 'other-child','Other',grade,curriculum_region,status,created_at,updated_at FROM child_profile LIMIT 1""")
        connection.execute("""INSERT INTO daily_session(session_id,child_id,status,state,started_at,last_checkpoint_at,client_action_id)
            VALUES ('other-study',?,'active','study',?,?,'other-start')""",
            ("child" if same_child else "other-child", utc_now(), utc_now()))
        connection.commit()
    service.start("other-study", "other-monitor")


@pytest.mark.parametrize("kind", ["event", "revision"])
@pytest.mark.parametrize("same_child", [False, True])
def test_action_replay_cannot_cross_children_or_sessions(tmp_path, kind, same_child):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page", "Private original")
    if kind == "event":
        service.record_event("study", "stable", .9, {}, "shared-action")
    else:
        service.record_revision("study", "page", "sig", "shared-action")
    _other_session(database, service, same_child)
    before = _history(database)
    with pytest.raises(ValueError):
        if kind == "event":
            service.record_event("other-study", "stable", .9, {}, "shared-action")
        else:
            service.record_revision("other-study", "page", "sig", "shared-action")
    assert _history(database) == before


@pytest.mark.parametrize("field", ["event_type", "confidence", "metrics", "observed_at"])
def test_event_action_id_is_bound_to_payload(tmp_path, field):
    database, service = _service(tmp_path)
    service.start("study", "start")
    payload = dict(session_id="study", event_type="stable", confidence=.9,
                   metrics={"motion_score": 1}, client_action_id="event-key", observed_at=utc_now())
    original = service.record_event(**payload)
    assert DocumentMonitoringService(database).record_event(**payload) == original
    changed = {**payload, field: {"event_type": "writing", "confidence": .8,
               "metrics": {"motion_score": 2}, "observed_at": "2000-01-01T00:00:00+00:00"}[field]}
    before = _history(database)
    with pytest.raises(ValueError):
        service.record_event(**changed)
    assert _history(database) == before


def test_event_without_client_timestamp_replays_without_new_event(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    first = service.record_event("study", "stable", .9, {"contrast": 1, "brightness": 2}, "key")
    replay = DocumentMonitoringService(database).record_event("study", "stable", .9, {"brightness": 2, "contrast": 1}, "key")
    assert first == replay
    assert len(_history(database)["document_monitor_event"]) == 1


@pytest.mark.parametrize("field", ["artifact_id", "content_signature"])
def test_revision_action_id_is_bound_to_payload(tmp_path, field):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page", "Original")
    _artifact(database, "other-page", "Other")
    payload = dict(session_id="study", artifact_id="page", content_signature="sig", client_action_id="revision-key")
    first = service.record_revision(**payload)
    assert DocumentMonitoringService(database).record_revision(**payload) == first
    before = _history(database)
    with pytest.raises(ValueError):
        service.record_revision(**{**payload, field: "other-page" if field == "artifact_id" else "other-sig"})
    assert _history(database) == before


@pytest.mark.parametrize("mutation", ["break", "reflection", "completed", "cancelled", "ended"])
@pytest.mark.parametrize("operation", ["event", "revision", "resume", "start"])
def test_actual_daily_session_blocks_late_writes_and_resume(tmp_path, mutation, operation):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page", "Original")
    service.record_event("study", "stable", .9, {}, "old-event")
    service.record_revision("study", "page", "sig", "old-revision")
    with database.connect() as connection:
        if mutation in {"break", "reflection"}:
            connection.execute("UPDATE daily_session SET state=?", (mutation,))
        elif mutation == "ended":
            connection.execute("UPDATE daily_session SET ended_at=?", (utc_now(),))
        else:
            connection.execute("UPDATE daily_session SET status=?", (mutation,))
        connection.commit()
    before = _history(database)
    # Both a new completion and an exact replay must respect the current session gate.
    for suffix in ("old", "new"):
        with pytest.raises(ValueError, match="active study session"):
            if operation == "event":
                service.record_event("study", "stable", .9, {}, f"{suffix}-event")
            elif operation == "revision":
                service.record_revision("study", "page", "sig", f"{suffix}-revision")
            else:
                getattr(service, operation)("study", f"{suffix}-control")
    assert _history(database) == before
    assert DocumentMonitoringService(database).status("study")["latest_revision"]["extracted_text"] == "Original"


@pytest.mark.parametrize("stop", ["pause", "stop"])
@pytest.mark.parametrize("operation", ["event", "revision"])
def test_paused_monitor_blocks_even_exact_replays(tmp_path, stop, operation):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page", "Original")
    service.record_event("study", "stable", .9, {}, "event-key")
    service.record_revision("study", "page", "sig", "revision-key")
    getattr(service, stop)("study", "stop-key")
    before = _history(database)
    with pytest.raises(ValueError, match="not active"):
        if operation == "event":
            service.record_event("study", "stable", .9, {}, "event-key")
        else:
            service.record_revision("study", "page", "sig", "revision-key")
    assert _history(database) == before


def test_duplicate_artifact_alias_is_idempotent_and_reserved(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page", "Original")
    _artifact(database, "page-2", "Next")
    first = service.record_revision("study", "page", "sig", "first-key")
    assert service.record_revision("study", "page", "sig", "alias-key") == first
    reopened = DocumentMonitoringService(database)
    assert reopened.record_revision("study", "page", "sig", "alias-key") == first
    before = _history(database)
    with pytest.raises(ValueError):
        reopened.record_revision("study", "page-2", "sig-2", "alias-key")
    with pytest.raises(ValueError):
        reopened.record_revision("study", "page", "changed-sig", "third-key")
    assert _history(database) == before
    second = reopened.record_revision("study", "page-2", "sig-2", "second-key")
    assert second["revision_index"] == 2
    assert second["previous_revision_id"] == first["revision_id"]


@pytest.mark.parametrize("kind", ["event", "revision"])
def test_concurrent_identical_requests_have_one_durable_result(tmp_path, kind):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page", "Original")
    barrier = Barrier(2)
    def write(_):
        barrier.wait(timeout=5)
        if kind == "event":
            return service.record_event("study", "stable", .9, {}, "concurrent-key")
        return service.record_revision("study", "page", "sig", "concurrent-key")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert results[0] == results[1]
    assert len(_history(database)["document_monitor_event" if kind == "event" else "document_page_revision"]) == 1


def test_monitor_owner_must_match_daily_session(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    with database.connect() as connection:
        connection.execute("""INSERT INTO child_profile(child_id,nickname,grade,curriculum_region,status,created_at,updated_at)
            SELECT 'other-child','Other',grade,curriculum_region,status,created_at,updated_at FROM child_profile LIMIT 1""")
        connection.execute("UPDATE document_monitor_session SET child_id='other-child'")
        connection.commit()
    with pytest.raises(ValueError):
        service.record_event("study", "stable", .9, {}, "event-key")


@pytest.mark.parametrize("kind", ["event", "revision"])
def test_legacy_history_is_preserved_and_replays_are_checked_after_upgrade(tmp_path, kind):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page", "Original")
    first = (service.record_event("study", "stable", .9, {}, "old-key") if kind == "event"
             else service.record_revision("study", "page", "sig", "old-key"))
    with database.connect() as connection:
        connection.execute("DROP TABLE document_monitor_request")
        connection.commit()
    upgraded = DocumentMonitoringService(database)
    with pytest.raises(ValueError, match="payload conflict"):
        if kind == "event":
            upgraded.record_event("study", "writing", .9, {}, "old-key")
        else:
            upgraded.record_revision("study", "page", "different", "old-key")
    replay = (upgraded.record_event("study", "stable", .9, {}, "old-key") if kind == "event"
              else upgraded.record_revision("study", "page", "sig", "old-key"))
    assert replay == first


def test_action_id_cannot_be_reused_for_another_operation(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page", "Original")
    service.record_event("study", "stable", .9, {}, "shared-key")
    with pytest.raises(ValueError):
        service.record_revision("study", "page", "sig", "shared-key")


@pytest.mark.parametrize("same_artifact", [True, False])
def test_concurrent_revisions_deduplicate_artifacts_and_allocate_serial_indices(tmp_path, same_artifact):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page-0", "Original")
    _artifact(database, "page-1", "Next")
    barrier = Barrier(2)
    def write(index):
        barrier.wait(timeout=5)
        return service.record_revision("study", f"page-{0 if same_artifact else index}", "sig", f"key-{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    if same_artifact:
        assert results[0] == results[1]
    else:
        first, second = sorted(results, key=lambda row: row["revision_index"])
        assert second["previous_revision_id"] == first["revision_id"]
        assert [first["revision_index"], second["revision_index"]] == [1, 2]
    assert len(_history(database)["document_page_revision"]) == (1 if same_artifact else 2)


def test_pause_and_stop_remain_available_after_session_ends(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    service.record_event("study", "stable", .9, {}, "event")
    with database.connect() as connection:
        connection.execute("UPDATE daily_session SET status='completed',ended_at=?", (utc_now(),))
        connection.commit()
    assert service.pause("study", "pause")["status"] == "paused"
    assert service.stop("study", "stop")["status"] == "stopped"
    assert service.status("study")["latest_event"]["event_type"] == "stable"


def test_request_aliases_cascade_with_monitor_history_and_do_not_retain_raw_inputs(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "page", "Original")
    service.record_revision("study", "page", "sig", "revision")
    service.record_revision("study", "page", "sig", "alias")
    service.record_event("study", "stable", .9, {"raw_image": "private-frame"}, "event")
    with database.connect() as connection:
        rows = [tuple(row) for row in connection.execute("SELECT * FROM document_monitor_request")]
        assert len(rows) == 3
        assert "private-frame" not in str(rows)
        connection.execute("DELETE FROM document_monitor_session WHERE session_id='study'")
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM document_monitor_request").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_omitted_timestamp_and_discarded_metric_changes_still_conflict_for_new_requests(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    first = service.record_event("study", "stable", .9, {"raw_image": "private-frame"}, "event")
    with pytest.raises(ValueError):
        service.record_event("study", "stable", .9, {"raw_image": "different"}, "event")
    with pytest.raises(ValueError):
        service.record_event("study", "stable", .9, {"raw_image": "private-frame"}, "event", first["observed_at"])


def test_generation_survives_restart_and_changes_on_each_real_transition(tmp_path):
    database, service = _service(tmp_path)
    assert service.status("study")["generation"] == 0
    first = service.start("study", "start")
    assert first["generation"] == 0
    assert service.pause("study", "pause")["generation"] == 1
    assert service.pause("study", "pause-again")["generation"] == 1
    assert service.resume("study", "resume")["generation"] == 2
    assert service.resume("study", "resume-again")["generation"] == 2
    assert service.stop("study", "stop")["generation"] == 3
    assert service.stop("study", "stop-again")["generation"] == 3
    assert service.start("study", "restart")["generation"] == 4
    assert service.start("study", "restart-active")["generation"] == 5
    assert DocumentMonitoringService(database).status("study")["generation"] == 5


def test_generation_additive_migration_keeps_legacy_monitor_and_history(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    first = service.record_event("study", "stable", .9, {}, "event")
    with database.connect() as connection:
        # Reconstruct the v92 boundary from a current database: later tables
        # and migration receipts must not survive and masquerade as newer schemas.
        for table in ('smtp_owned_worker_member','smtp_owned_worker_binding','smtp_owned_worker_journal','combined_daily_delivery','combined_daily_snapshot','combined_diagnostic_member',
                      'pilot_test_digest_outbox','combined_diagnostic_batch','pilot_diagnostic_recipient_consent',
                      'combined_daily_mode','daily_visual_schedule','daily_observation_report_snapshot',
                      'daily_report_email_verification','daily_observation_delivery_ledger',
                      'daily_observation_delivery_barrier','daily_observation_report_outbox','daily_observation_report_source'):
            connection.execute(f'DROP TABLE IF EXISTS {table}')
        connection.execute("DROP TABLE document_observation_segment")
        connection.execute("DELETE FROM schema_migration WHERE version>=93")
        connection.execute("ALTER TABLE document_monitor_session DROP COLUMN generation_started_at")
        connection.execute("ALTER TABLE document_monitor_session DROP COLUMN generation")
        old = dict(connection.execute("SELECT * FROM document_monitor_session").fetchone())
        connection.commit()
    Database(database.path).initialize()
    assert database.latest_upgrade_status()['source_schema_version'] == 92
    assert database.latest_upgrade_status()['snapshot_available'] is True
    upgraded = DocumentMonitoringService(database).status("study")
    assert upgraded["generation"] == 0
    assert upgraded["latest_event"] == first
    assert {key: upgraded[key] for key in old} == old
    with database.connect() as connection:
        column = next(row for row in connection.execute("PRAGMA table_info(document_monitor_session)") if row["name"] == "generation")
        assert column["type"] == "INTEGER"
        assert column["dflt_value"] == "0"


def test_pause_resume_invalidates_old_ocr_even_with_identical_timestamps(tmp_path, monkeypatch):
    from home_study_agent import document_monitoring
    database, service = _service(tmp_path)
    timestamp = utc_now()
    monkeypatch.setattr(document_monitoring, "utc_now", lambda: timestamp)
    started = service.start("study", "start")
    generation = started["generation"]
    service.pause("study", "pause")
    resumed = service.resume("study", "resume")
    _artifact(database, "late-page", "Old OCR finished after resume")
    assert resumed["status"] == started["status"] == "active"
    assert resumed["updated_at"] == started["updated_at"]
    assert resumed["generation"] != generation
    before = _history(database)
    with pytest.raises(ValueError, match="DOCUMENT_MONITOR_GENERATION_CONFLICT"):
        service.record_revision("study", "late-page", "sig", "late-result", expected_generation=generation)
    assert _history(database) == before


def test_revision_generation_gate_does_not_reject_valid_single_capture_artifacts(tmp_path):
    database, service = _service(tmp_path)
    service.start("study", "start")
    _artifact(database, "single-page", "Single capture before monitor restart")
    generation = service.start("study", "restart")["generation"]
    first = service.record_revision("study", "single-page", "sig", "revision", expected_generation=generation)
    assert service.record_revision("study", "single-page", "sig", "revision", expected_generation=generation) == first
    assert service.record_revision("study", "single-page", "sig", "legacy-alias") == first
    service.pause("study", "pause")
    service.resume("study", "resume")
    with pytest.raises(ValueError, match="DOCUMENT_MONITOR_GENERATION_CONFLICT"):
        service.record_revision("study", "single-page", "sig", "revision", expected_generation=generation)


def test_concurrent_restarts_increment_generation_without_lost_updates(tmp_path):
    _, service = _service(tmp_path)
    service.start("study", "start")
    barrier = Barrier(2)
    def restart(index):
        barrier.wait(timeout=5)
        service.start("study", f"restart-{index}")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(restart, range(2)))
    assert service.status("study")["generation"] == 2
