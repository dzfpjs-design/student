from __future__ import annotations

from pathlib import Path

from home_study_agent.longitudinal_replay_gate import run_longitudinal_replay_gate


def test_two_child_thirty_day_replay_is_isolated_durable_and_offline_safe(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "longitudinal-replay.db"

    result = run_longitudinal_replay_gate(database_path)

    assert result["status"] == "passed", result
    assert result["days"] == 30
    assert result["sessions"] == {"child-junior-3": 30, "child-primary-4": 30}
    assert result["snapshots"] == {"child-junior-3": 30, "child-primary-4": 30}
    assert result["revocation"] == "passed"
    assert result["expiry"] == "passed"
    assert result["child_refusal"] == "passed"
    assert result["cloud_offline"] == "passed"
    assert result["restart"] == "passed"
    assert result["cloud_requests"] == 0
    assert result["cross_child_leaks"] == []
    assert result["failures"] == []
    assert database_path.exists()
