from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_observation_quality_benchmark.py"
SPEC = importlib.util.spec_from_file_location("observation_quality_benchmark", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def test_synthetic_observation_api_acceptance_keeps_dark_to_bright_transition_out_of_writing_signal(tmp_path: Path) -> None:
    report = RUNNER.run_synthetic_observation_acceptance(tmp_path)
    (tmp_path / 'benchmark-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    assert all(report["safety_checks"].values()), (report["safety_checks"], report["metrics"]["reminder_budget_statuses"])
    assert report["metrics"]["misclassification_rate"] == 0.0
    assert report["metrics"]["confusion_matrix"]["present"]["present"] == 2
    assert report["metrics"]["confusion_matrix"]["present"]["writing_like"] == 0
    assert report["metrics"]["unknown_rate"] == 0.25
    assert report["metrics"]["raw_media_artifact_count"] == 0
    assert report["metrics"]["reminder_budget_statuses"] == ["suggested", "suggested", "suggested", "daily_frequency_cap"]

    with sqlite3.connect((tmp_path / 'observation-eval.db').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        policy = db.execute("SELECT session_id,started_at FROM daily_session WHERE child_id='child-junior-3' AND status='active'").fetchone()
        assert policy is not None
        events = db.execute("SELECT child_id,session_id,occurred_at FROM observable_event WHERE source='synthetic_evaluation' ORDER BY occurred_at").fetchall()
        assert len(events) == 4
        assert all(row['child_id'] == 'child-junior-3' and row['session_id'] == policy['session_id'] for row in events)
        assert len({row['occurred_at'][:10] for row in events}) == 1
        assert all(row['occurred_at'] >= policy['started_at'] for row in events)
        assert db.execute('SELECT COUNT(*) FROM intervention WHERE session_id=?', (policy['session_id'],)).fetchone()[0] == 3
