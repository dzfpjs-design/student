from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from home_study_agent.database import Database, LATEST_SCHEMA_VERSION
from historical_knowledge_graph_fixture import seed_public_historical_v66
from services.local_agent.scripts.verify_knowledge_graph_upgrade_recovery import verify_upgrade_recovery
from services.local_agent.scripts.verify_knowledge_graph_vertical_slice import (
    SYNTHETIC_SOURCE_KIND,
    verify_vertical_slice,
)


def _snapshot(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(target) as target_connection:
            source_connection.backup(target_connection)


def test_upgrade_recovery_replays_latest_snapshot_to_current_schema(tmp_path: Path) -> None:
    app_db = tmp_path / "home-study.db"
    Database(app_db).initialize()
    snapshot_dir = tmp_path / "upgrade-backups"; snapshot_dir.mkdir()
    snapshot = snapshot_dir / "pre-upgrade-v66.db"
    seed_public_historical_v66(snapshot)
    (snapshot_dir / "pre-upgrade-v66-to-v67-test.json").write_text(json.dumps({
        "backup_file": snapshot.name, "source_schema_version": 66,
    }), encoding="utf-8")

    result = verify_upgrade_recovery(app_db)

    assert result["passed"] is True
    assert result["production_database_written"] is False
    assert result["replayed_target_schema_version"] == LATEST_SCHEMA_VERSION
    assert result["replayed_integrity"] == "ok"
    assert result["snapshot_source_schema_version"] == result["metadata_source_schema_version"] == 66


def test_vertical_slice_can_use_a_labelled_approved_fixture_without_writing_production(tmp_path: Path) -> None:
    app_db = tmp_path / "home-study.db"
    Database(app_db).initialize()

    result = verify_vertical_slice(app_db, synthetic_fixture=True)

    assert result["passed"] is True
    assert result["synthetic_fixture"] is True
    assert result["source"] == SYNTHETIC_SOURCE_KIND
    assert result["production_database_written"] is False
    assert result["production_evidence_rows_before_after"] == [0, 0]
    assert result["idempotent_replay_evidence_count"] == 1
