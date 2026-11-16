from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import pytest

from home_study_agent.document_artifact_replay import replay_document_artifacts

from test_answer_attempt_ledger import setup_case
from test_legacy_document_question_blocks import legacy_details


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('runtime_mode', ['formal', 'trial'])
def test_replay_is_isolated_redacted_and_idempotent(tmp_path: Path, runtime_mode: str) -> None:
    database, _ = setup_case(tmp_path / "source", runtime_mode=runtime_mode)
    legacy_details(database, trusted_engine=True)
    source_hash = digest(database.path)

    result = replay_document_artifacts(
        database.path,
        session_id="study",
        output_directory=tmp_path / "replay",
    )

    assert digest(database.path) == source_hash
    with sqlite3.connect(result.isolated_database) as snapshot:
        assert snapshot.execute('SELECT mode FROM runtime_data_scope').fetchone()[0] == runtime_mode
    assert result.report == {
        "format": "hsc_document_artifact_replay_v1",
        "source_unchanged": True,
        "artifacts": 1,
        "revisions_processed": 1,
        "counts": {
            "document_page_revision": 1,
            "observed_question": 1,
            "observed_question_occurrence": 1,
            "observed_answer_evidence": 1,
            "mistake_capture_candidate": 1,
        },
        "candidate_statuses": {"pending": 1},
        "raw_media_available": False,
        "cloud_requests": 0,
        "contains_child_content": False,
    }
    serialized = json.dumps(result.report, ensure_ascii=False)
    assert "计算" not in serialized
    assert "学生" not in serialized

    # Running against the already replayed disposable database remains
    # idempotent and never duplicates a revision or question.
    replay_again = replay_document_artifacts(
        result.isolated_database,
        session_id="study",
        output_directory=tmp_path / "replay-again",
    )
    assert replay_again.report["counts"] == result.report["counts"]


def test_replay_requires_an_active_study_session(tmp_path: Path) -> None:
    database, _ = setup_case(tmp_path / "source")
    with database.connect() as connection:
        connection.execute(
            "UPDATE daily_session SET status='completed',state='completed',ended_at=started_at"
        )
        connection.commit()

    try:
        replay_document_artifacts(database.path, output_directory=tmp_path / "replay")
    except ValueError as error:
        assert str(error) == "no active study session available for isolated replay"
    else:
        raise AssertionError("replay unexpectedly selected a completed session")
