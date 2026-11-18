"""Report time claims use the stored end time, not a public session projection."""
import pytest
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from home_study_agent.reports import build_daily_report
from test_report_delivery import _pending_report_job


@pytest.mark.parametrize('ended_at', ['2026-09-08T12:30:00+00:00', None])
def test_completed_session_report_marks_time_and_supply_from_database_snapshot(tmp_path, ended_at):
    db = Database(tmp_path / 'session-end.db')
    db.initialize()
    _pending_report_job(db)
    with db.connect() as c:
        c.execute('UPDATE daily_session SET ended_at=? WHERE session_id=?', (ended_at,'pending-report-session'))
        c.commit()
    evidence = StudyRepository(db).report_evidence('pending-report-session')
    source = next(item for item in evidence['coaching_evidence_sources'] if item['source'] == '本次已确认学习会话')
    assert source['occurred_at'] == ended_at
    assert evidence['supply_obligations_as_of_status'] == ('available' if ended_at else 'unavailable')
    summary, _ = build_daily_report(evidence)
    assert summary['supply_obligations_as_of_status'] == ('available' if ended_at else 'unavailable')
    assert any('日终时点不完整' in fact for fact in summary['facts']) is (ended_at is None)
    reopened = StudyRepository(Database(db.path)).report_evidence('pending-report-session')
    repeated_source = next(item for item in reopened['coaching_evidence_sources'] if item['source'] == '本次已确认学习会话')
    assert repeated_source['occurred_at'] == ended_at
    assert reopened['supply_obligations_as_of_status'] == evidence['supply_obligations_as_of_status']
