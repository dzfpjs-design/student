"""Local daily reports use the same final source barrier as cloud coaching."""
import json

import pytest
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.repository import StudyRepository
from test_report_delivery import _pending_report_job


@pytest.mark.parametrize('timing', ['during_read', 'before_persist'])
def test_local_report_rejects_stale_reflection_and_recovers_same_job(tmp_path, monkeypatch, timing):
    db = Database(tmp_path / 'local-report.db')
    db.initialize()
    _pending_report_job(db)
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        data_encryption_key=b'L' * 32)
    gateway = type('LocalOnly', (), {'configured': False})()
    revised = '今天发现需要先核对单位，再列式。'
    changed = False

    def change_once():
        nonlocal changed
        if not changed:
            with db.connect() as c:
                c.execute('UPDATE daily_reflection SET learned_text=? WHERE session_id=?',
                          (revised, 'pending-report-session'))
                c.commit()
            changed = True

    if timing == 'during_read':
        original = StudyRepository.report_evidence

        def mutate_after_read(self, *args, **kwargs):
            evidence = original(self, *args, **kwargs)
            change_once()
            return evidence

        monkeypatch.setattr(StudyRepository, 'report_evidence', mutate_after_read)
    else:
        original = StudyRepository.save_daily_report

        def mutate_before_save(self, *args, **kwargs):
            change_once()
            return original(self, *args, **kwargs)

        monkeypatch.setattr(StudyRepository, 'save_daily_report', mutate_before_save)

    with TestClient(create_app(settings, model_gateway=gateway)):
        pass
    with db.connect() as c:
        assert changed
        assert c.execute('SELECT COUNT(*) FROM daily_report').fetchone()[0] == 0
        assert c.execute("SELECT status FROM report_job WHERE job_id='pending-report-job'").fetchone()[0] == 'pending'
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0

    with TestClient(create_app(settings, model_gateway=gateway)):
        pass
    with db.connect() as c:
        rows = c.execute('SELECT source,summary_json FROM daily_report').fetchall()
        assert len(rows) == 1 and rows[0]['source'] == 'local_rules_v2'
        assert revised in json.dumps(json.loads(rows[0]['summary_json']), ensure_ascii=False)
        assert c.execute("SELECT status FROM report_job WHERE job_id='pending-report-job'").fetchone()[0] == 'completed'
        assert c.execute('SELECT COUNT(*) FROM report_job').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM report_delivery_outbox').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM model_call').fetchone()[0] == 0
