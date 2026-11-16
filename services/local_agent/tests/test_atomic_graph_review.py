from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atomic_graph_review_server import AtomicGraphRepository  # noqa: E402


BUILD_DB = (Path.home() / '.home-study-companion/knowledge-build/atomic-full-v4.db')


def test_real_build_graph_is_paginated_and_has_evidence(tmp_path: Path) -> None:
    repository = AtomicGraphRepository(BUILD_DB, tmp_path / "application.db")
    page = repository.graph_page(limit=7)

    assert 1 <= len(page["nodes"]) <= 7
    assert page["next_cursor"] is not None
    selected = page["nodes"][0]
    detail = repository.node_detail(selected["key"])

    assert detail["node"]["title"]
    assert isinstance(detail["evidence"]["video_transcript"], list)
    assert set(detail["node"]["diagnostics"]) == {"recognition", "independent", "transfer"}


def test_atomic_review_is_persisted_in_application_database(tmp_path: Path) -> None:
    application_db = tmp_path / "application.db"
    repository = AtomicGraphRepository(BUILD_DB, application_db)
    target = repository.graph_page(limit=1)["nodes"][0]

    state = repository.record_decision(
        entity_type="draft", entity_key=target["key"], action="approved",
        reviewer="开发审核", note="real persistence test", payload={}, client_action_id="test-approval-0001",
    )
    assert state["effective_status"] == "approved"

    reopened = AtomicGraphRepository(BUILD_DB, application_db)
    assert reopened.decision_for("draft", target["key"])["effective_status"] == "approved"
    assert sqlite3.connect(application_db).execute("SELECT COUNT(*) FROM kg_atomic_review_event").fetchone()[0] == 1


def test_quality_queue_exposes_real_queue_categories(tmp_path: Path) -> None:
    repository = AtomicGraphRepository(BUILD_DB, tmp_path / "application.db")
    categories = repository.quality_categories()
    assert {"duplicates", "isolated", "cycles", "missing_evidence", "pending"}.issubset(categories)
    for category in ("isolated", "missing_evidence", "pending"):
        assert "items" in repository.quality_queue(category=category, limit=5)
