"""Real report HTTP/storage boundaries with local synthetic input and no provider."""
import json
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.repository import StudyRepository, UnknownChildError
from test_app import client_for
from test_report_source_recovery_host import complete_empty_session


CHILD = 'child-primary-4'


class NoCloud:
    configured = True

    def __init__(self):
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        raise AssertionError('This report recovery must remain local')


@pytest.mark.parametrize('failed_types', [('weekly', 'monthly'), ('weekly',), ('monthly',)])
def test_period_failure_is_its_own_job_and_recovers_without_regenerating_daily(tmp_path, monkeypatch, failed_types):
    gateway = NoCloud()
    database = Database(tmp_path / 'test.db')
    with client_for(tmp_path, model_gateway=gateway) as client:
        service = client.app.state.period_reports
        original_build = service._build_content
        original_begin = service._begin_refresh
        calls = []

        def begin(child_id, report_type, *args, **kwargs):
            calls.append((child_id, report_type))
            return original_begin(child_id, report_type, *args, **kwargs)

        def fail_selected(child_id, report_type, *args, **kwargs):
            if report_type in failed_types:
                raise RuntimeError('private details: synthetic family note')
            return original_build(child_id, report_type, *args, **kwargs)

        monkeypatch.setattr(service, '_build_content', fail_selected)
        monkeypatch.setattr(service, '_begin_refresh', begin)
        sid = complete_empty_session(client, CHILD, 'period-isolation')
        token = client.post('/v1/parent/setup', json={'pin': '9513'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        daily_response = client.get(f'/v1/sessions/{sid}/report', headers=headers)
        assert daily_response.status_code == 200, daily_response.text
        daily_id = daily_response.json()['report_id']
        health = client.get('/v1/parent/system-health', headers=headers).json()
        assert calls == [(CHILD, 'weekly'), (CHILD, 'monthly')]
        assert health['pending_report_jobs'] == len(failed_types)
        assert {row['report_type'] for row in health['recent_report_failures']} == set(failed_types)
        assert all(row['last_error'] == 'RuntimeError' for row in health['recent_report_failures'])
        periods = client.get(f'/v1/children/{CHILD}/period-reports', headers=headers).json()['items']
        assert {row['report_type'] for row in periods} == {'weekly', 'monthly'} - set(failed_types)
        assert 'private details' not in json.dumps([health, daily_response.json(), periods])
        with database.connect() as c:
            job = c.execute("SELECT * FROM report_job WHERE session_id=? AND report_type='daily'", (sid,)).fetchone()
            assert job['status'] == 'completed' and job['last_error'] is None
            original_daily = dict(c.execute('SELECT * FROM daily_report WHERE report_id=?', (daily_id,)).fetchone())
            rows = c.execute('SELECT report_type,status,last_error FROM period_report_refresh_state WHERE child_id=?', (CHILD,)).fetchall()
            assert {row['report_type'] for row in rows if row['status'] == 'failed'} == set(failed_types)
            assert 'private details' not in json.dumps([dict(row) for row in rows])
        assert gateway.calls == 0

    with client_for(tmp_path, model_gateway=gateway) as restarted:
        token = restarted.post('/v1/parent/unlock', json={'pin': '9513'}).json()['parent_token']
        headers = {'X-HSC-Parent': token}
        health = restarted.get('/v1/parent/system-health', headers=headers).json()
        assert health['pending_report_jobs'] == 0
        assert health['recent_report_failures'] == []
        periods = restarted.get(f'/v1/children/{CHILD}/period-reports', headers=headers).json()['items']
        assert {row['report_type'] for row in periods} == {'weekly', 'monthly'}
        with database.connect() as c:
            assert dict(c.execute('SELECT * FROM daily_report WHERE report_id=?', (daily_id,)).fetchone()) == original_daily
            assert c.execute('SELECT COUNT(*) FROM daily_report WHERE session_id=?', (sid,)).fetchone()[0] == 1
            assert all(row[0] == 'completed' and row[1] is None for row in c.execute(
                'SELECT status,last_error FROM period_report_refresh_state WHERE child_id=?', (CHILD,)).fetchall())
        assert gateway.calls == 0


@pytest.mark.parametrize('error', [sqlite3.OperationalError, sqlite3.IntegrityError, UnknownChildError])
def test_refresh_does_not_hide_storage_or_ownership_errors(tmp_path, monkeypatch, error):
    db = Database(tmp_path / 'gate.db')
    db.initialize()
    StudyRepository(db).seed_family()
    service = PeriodReportService(db)

    def fail(*args, **kwargs):
        raise error('isolated fault')

    monkeypatch.setattr(service, 'generate', fail)
    with pytest.raises(error):
        service.refresh_current(CHILD, require_evidence=False)
