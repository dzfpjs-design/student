"""Historical report failures must stay local to their source, not stop the host."""
from synthetic_native_restore_fixture import KEY, signed_local_restore_post
import json
import sqlite3

import pytest
from privacy_restore_operation import restore_operation
from fastapi.testclient import TestClient

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.repository import StudyRepository
from home_study_agent.report_delivery import ReportDeliveryService


CHILD = 'child-junior-3'
OTHER = 'child-primary-4'


def test_history_check_bounds_materialization(source_host, monkeypatch):
    _, db, _, _, sessions, reports, _, _, _ = source_host
    remove_reflection(db, sessions[0])
    calls = []
    original = StudyRepository.list_daily_reports
    def bounded(self, child_id, limit=30, **kwargs):
        calls.append(limit)
        assert limit is not None and limit <= 30
        return original(self, child_id, limit, **kwargs)
    monkeypatch.setattr(StudyRepository, 'list_daily_reports', bounded)
    issues = ParentReportService(db).reconcile_daily_sources(CHILD)
    assert calls == [30]
    assert any(item['source_id'] == reports[0]['report_id'] for item in issues)


def test_structural_pass_scans_issue_history_once_and_never_materializes_old_rows(source_host, monkeypatch):
    from contextlib import contextmanager
    _, db, _, _, sessions, _, snapshot, _, original = source_host
    remove_reflection(db, sessions[0])
    original_connect = db.connect
    statements = []
    @contextmanager
    def traced():
        with original_connect() as c:
            c.set_trace_callback(statements.append)
            yield c
    monkeypatch.setattr(db, 'connect', traced)
    monkeypatch.setattr(StudyRepository, 'list_daily_reports', lambda *args, **kwargs: [])
    ParentReportService(db).reconcile_daily_sources(CHILD)
    scans = [sql for sql in statements if 'SELECT' in sql.upper() and
             'FROM audit_event' in sql and 'parent_report_source_issue' in sql]
    assert len(scans) == 2  # One batch state read, one final public projection.
    assert not any('json_extract(summary_json' in sql for sql in scans)
    with db.connect() as c:
        c.execute(f"INSERT INTO daily_reflection ({','.join(original)}) VALUES ({','.join('?' for _ in original)})", tuple(original.values()))
        c.commit()
    assert ParentReportService(db).reconcile_daily_sources(CHILD) == []
    with db.connect() as c:
        assert c.execute('SELECT status FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()[0] == 'revoked'


@pytest.mark.parametrize('column', ['facts_json', 'content_json', 'parent_edit_json'])
def test_corrupt_snapshot_does_not_hide_valid_reports(source_host, column):
    client, db, _, headers, _, reports, snapshot, queued, _ = source_host
    with db.connect() as c:
        original = c.execute(f'SELECT {column} FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()[0]
        c.execute(f"UPDATE parent_report_snapshot SET {column}='{{' WHERE snapshot_id=?", (snapshot['snapshot_id'],))
        c.commit()
    detail = client.get(f"/v1/parent/report-snapshots/{snapshot['snapshot_id']}", headers=headers)
    assert detail.status_code == 409 and detail.headers['cache-control'] == 'no-store'
    response = client.get(f'/v1/parent/children/{CHILD}/report-snapshots', headers=headers)
    assert response.status_code == 200, response.text
    assert any(row['report_id'] == reports[1]['report_id'] for row in response.json()['items'])
    assert not any(row['snapshot_id'] == snapshot['snapshot_id'] for row in response.json()['items'])
    issues = response.json()['source_issues']
    assert any(row['source_id'] == snapshot['snapshot_id'] and row['error_code'] == 'REPORT_SNAPSHOT_INVALID_JSON' for row in issues)
    with db.connect() as c:
        assert c.execute('SELECT status FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()[0] == 'revoked'
        assert c.execute('SELECT status FROM report_delivery_outbox WHERE delivery_id=?', (queued['delivery_id'],)).fetchone()[0] == 'cancelled'
        c.execute(f'UPDATE parent_report_snapshot SET {column}=? WHERE snapshot_id=?', (original, snapshot['snapshot_id']))
        c.commit()
    issues = ParentReportService(db).reconcile_daily_sources(CHILD)
    assert all(row['source_id'] != snapshot['snapshot_id'] for row in issues)
    assert ParentReportService(db).get(snapshot['snapshot_id'])['status'] == 'revoked'


def post(client, path, **body):
    response = client.post(path, json=body)
    assert response.status_code in (200, 201), response.text
    return response.json()


def complete_empty_session(client, child, action):
    sid = post(client, '/v1/sessions', child_id=child, client_action_id=action)['session_id']
    def move(before, after):
        return post(client, f'/v1/sessions/{sid}/transition', expected_state=before,
                    target_state=after, reason='synthetic_no_learning', client_action_id=f'{action}:{after}')
    move('check_in', 'classroom_recall')
    post(client, f'/v1/sessions/{sid}/recalls/none', client_action_id=f'{action}:none-recall')
    move('classroom_recall', 'homework_capture')
    post(client, f'/v1/sessions/{sid}/homework/none', client_action_id=f'{action}:none-homework')
    post(client, f'/v1/sessions/{sid}/plan/generate', client_action_id=f'{action}:plan')
    move('plan_review', 'study')
    move('study', 'reflection')
    post(client, f'/v1/sessions/{sid}/reflection/complete', learned_text='本机合成验证，尚未开始学习任务。',
         difficult_text='暂时没有回想记录。', feeling='平静', next_action='下次先核对任务。',
         skipped=False, client_action_id=f'{action}:reflection')
    return sid


@pytest.fixture
def source_host(tmp_path, request):
    db = Database(tmp_path / 'source-recovery.db')
    settings = Settings(data_dir=tmp_path, database_path=db.path, maintenance_enabled=False,
                        data_encryption_key=b'S' * 32, native_launch_attestation_key=getattr(request, 'param', None))
    with TestClient(create_app(settings)) as client:
        token = post(client, '/v1/parent/setup', pin='2468')['parent_token']
        headers = {'X-HSC-Parent': token}
        sessions = [complete_empty_session(client, child, f'source-recovery-{index}')
                    for index, child in enumerate((CHILD, CHILD, OTHER))]
        repo = StudyRepository(db)
        reports = [repo.get_report_for_session(sid) for sid in sessions]
        current = client.get(f'/v1/parent/children/{CHILD}/report-snapshots', headers=headers).json()['items']
        snapshot = next(item for item in current if item['report_id'] == reports[0]['report_id'] and item['status'] == 'draft')
        approved = client.post(f"/v1/parent/report-snapshots/{snapshot['snapshot_id']}/approve", headers=headers, json={})
        assert approved.status_code == 200, approved.text
        delivery = ReportDeliveryService(db, b'S' * 32)
        recipient = delivery.add_recipient(CHILD, 'email', 'synthetic@local.test', '合成本机家长', True)
        queued = delivery.enqueue(snapshot['snapshot_id'], recipient['recipient_id'], '2099-01-01T12:00:00+00:00')
        with db.connect() as c:
            original = dict(c.execute('SELECT * FROM daily_reflection WHERE session_id=?', (sessions[0],)).fetchone())
            assert c.execute("SELECT COUNT(*) FROM plan_item WHERE status='completed'").fetchone()[0] == 0
        yield client, db, settings, headers, sessions, reports, snapshot, queued, original


def remove_reflection(db, session_id, stale=False):
    with db.connect() as c:
        c.execute('DELETE FROM daily_reflection WHERE session_id=?', (session_id,))
        if stale:
            c.execute("UPDATE daily_report SET evidence_json='{}' WHERE session_id=?", (session_id,))
        c.commit()


@pytest.mark.parametrize('scope', ['reports', 'learning', 'all_history'])
def test_issue_projection_follows_actual_privacy_scope_and_restart(source_host, scope):
    client, db, settings, headers, sessions, reports, _, _, _ = source_host
    remove_reflection(db, sessions[0])
    service = ParentReportService(db)
    service.reconcile_daily_sources(CHILD)
    assert service.source_issues(CHILD)
    response = client.post(f'/v1/parent/children/{CHILD}/delete', headers=headers,
        json={'scope': scope, 'confirmation': '确认删除', 'client_action_id': f'issue-delete-{scope}'})
    assert response.status_code == 200 and response.json()['status'] == 'completed', response.text
    # Learning alone deliberately retains reports: their still-missing source is
    # a real issue, not a deleted report's ghost. Reports/all_history erase both.
    expected = scope == 'learning'
    assert bool(service.source_issues(CHILD)) == expected
    with db.connect() as c:
        issue_count = c.execute("SELECT COUNT(*) FROM audit_event WHERE child_id=? AND action='parent_report_source_issue'", (CHILD,)).fetchone()[0]
        assert (issue_count > 0) if scope == 'learning' else issue_count == 0
        assert c.execute('SELECT 1 FROM daily_report WHERE report_id=?', (reports[2]['report_id'],)).fetchone()
    client.__exit__(None, None, None)
    with TestClient(create_app(settings)) as restarted:
        token = post(restarted, '/v1/parent/unlock', pin='2468')['parent_token']
        result = restarted.get(f'/v1/parent/children/{CHILD}/report-source-issues', headers={'X-HSC-Parent': token})
        assert result.status_code == 200 and bool(result.json()['items']) == expected
        assert service.source_issues(OTHER) == []


@pytest.mark.parametrize('source_host', [pytest.param(KEY, id='synthetic-native')], indirect=True)
def test_backup_restore_projects_restored_issue_identity_without_approval(source_host):
    client, db, settings, headers, sessions, reports, snapshot, _, original = source_host
    service = ParentReportService(db)
    remove_reflection(db, sessions[0])
    service.reconcile_daily_sources(CHILD)
    issue_id = service.source_issues(CHILD)[0]['issue_id']
    backup = client.post('/v1/parent/backups', headers=headers)
    assert backup.status_code == 200, backup.text
    with db.connect() as c:
        columns = ','.join(original)
        placeholders = ','.join('?' for _ in original)
        c.execute(f'INSERT INTO daily_reflection ({columns}) VALUES ({placeholders})', tuple(original.values()))
        c.commit()
    StudyRepository(db).list_daily_reports(CHILD)
    assert service.source_issues(CHILD) == []
    restored = signed_local_restore_post(client, headers=headers,
        json={'backup_id': backup.json()['backup_id'], 'confirmation': '确认恢复',
              'operation': restore_operation(client, headers=headers)})
    assert restored.status_code == 200 and restored.json()['status'] == 'restored', restored.text
    client.__exit__(None, None, None)
    with TestClient(create_app(settings)) as restarted:
        token = post(restarted, '/v1/parent/unlock', pin='2468')['parent_token']
        response = restarted.get(f'/v1/parent/children/{CHILD}/report-source-issues', headers={'X-HSC-Parent': token})
        assert response.status_code == 200
        assert response.json()['items'][0]['issue_id'] == issue_id
        assert service.get(snapshot['snapshot_id'])['status'] == 'revoked'
        assert service.source_issues(OTHER) == []


def test_period_dependency_excludes_corrupt_daily_without_hiding_valid_dashboard(source_host):
    client, db, _, headers, _, reports, _, _, _ = source_host
    with db.connect() as c:
        c.execute("UPDATE daily_report SET summary_json='{' WHERE report_id=?", (reports[0]['report_id'],))
        c.commit()
    response = client.get('/v1/parent/dashboard', headers=headers)
    assert response.status_code == 200, response.text
    child = next(row for row in response.json()['children'] if row['child_id'] == CHILD)
    assert child['source_issues']
    assert child['period_reports']
    for period in child['period_reports']:
        assert period['content']['coverage']['recorded_sessions'] == 1
        assert period['content']['coverage']['excluded_daily_reports'] == 1
        assert period['content']['source_warnings']


@pytest.mark.parametrize('payload', ['{', '[]', '{"as_of_date":"not-a-date"}'])
def test_corrupt_period_is_retained_and_revoked_without_poisoning_restart(source_host, payload):
    client, db, settings, headers, _, _, _, _, _ = source_host
    before = client.get(f'/v1/children/{CHILD}/period-reports', headers=headers).json()['items']
    weekly = next(row for row in before if row['report_type'] == 'weekly')
    pid = weekly['period_report_id']
    snapshots = client.get(f'/v1/parent/children/{CHILD}/report-snapshots', headers=headers).json()['items']
    frozen = next(row for row in snapshots if row['source_kind'] == 'period_report' and row['source_id'].startswith(pid+'@') and row['status'] == 'draft')
    approved = client.post(f"/v1/parent/report-snapshots/{frozen['snapshot_id']}/approve", headers=headers, json={})
    assert approved.status_code == 200, approved.text
    with db.connect() as c:
        c.execute('UPDATE period_report SET content_json=? WHERE period_report_id=?', (payload, pid))
        c.commit()
    response = client.get(f'/v1/children/{CHILD}/period-reports', headers=headers)
    assert response.status_code == 200, response.text
    assert any(row['report_type'] == 'monthly' for row in response.json()['items'])
    assert all(row['period_report_id'] != pid for row in response.json()['items'])
    regeneration = client.post(f'/v1/children/{CHILD}/period-reports/generate?report_type=weekly', headers=headers)
    assert regeneration.status_code == 409 and regeneration.headers['cache-control'] == 'no-store'
    assert client.get('/v1/parent/dashboard', headers=headers).status_code == 200
    with db.connect() as c:
        assert c.execute('SELECT content_json FROM period_report WHERE period_report_id=?', (pid,)).fetchone()[0] == payload
        assert not c.execute("SELECT 1 FROM parent_report_snapshot WHERE child_id=? AND source_kind='period_report' AND source_id LIKE ? AND status='approved'", (CHILD, pid+'@%')).fetchone()
        assert c.execute('SELECT status FROM parent_report_snapshot WHERE snapshot_id=?', (frozen['snapshot_id'],)).fetchone()[0] == 'revoked'
        events = c.execute("SELECT COUNT(*) FROM audit_event WHERE child_id=? AND action='parent_report_source_issue'", (CHILD,)).fetchone()[0]
    client.__exit__(None, None, None)
    with TestClient(create_app(settings)) as restarted:
        token = post(restarted, '/v1/parent/unlock', pin='2468')['parent_token']
        fresh = restarted.get(f'/v1/parent/children/{CHILD}/report-source-issues', headers={'X-HSC-Parent': token})
        assert fresh.status_code == 200 and any(row['source_id'] == pid for row in fresh.json()['items'])
        assert restarted.get('/v1/health').status_code == 200
    with db.connect() as c:
        assert c.execute('SELECT content_json FROM period_report WHERE period_report_id=?', (pid,)).fetchone()[0] == payload
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE child_id=? AND action='parent_report_source_issue'", (CHILD,)).fetchone()[0] == events


def test_period_list_and_refresh_propagate_database_faults(source_host, monkeypatch):
    from home_study_agent.period_reports import PeriodReportService
    _, db, _, _, _, _, _, _, _ = source_host
    service = PeriodReportService(db)
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic database failure')
    monkeypatch.setattr(service, 'get', broken)
    with pytest.raises(sqlite3.OperationalError, match='synthetic database failure'):
        service.list(CHILD)
    monkeypatch.setattr(service, 'generate', broken)
    with pytest.raises(sqlite3.OperationalError, match='synthetic database failure'):
        service.refresh_current(CHILD, require_evidence=False)


def test_restored_issue_projection_is_read_only_and_does_not_fake_resolution(source_host):
    _, db, _, _, sessions, _, _, _, original = source_host
    service = ParentReportService(db)
    remove_reflection(db, sessions[0])
    service.reconcile_daily_sources(CHILD)
    with db.connect() as c:
        c.execute(f"INSERT INTO daily_reflection ({','.join(original)}) VALUES ({','.join('?' for _ in original)})", tuple(original.values()))
        c.commit()
        c.execute('BEGIN')
        before = c.total_changes
        assert service.source_issues(CHILD, connection=c) == []
        assert service.source_issues(CHILD, include_resolved=True, connection=c)[0]['status'] == 'open'
        assert c.total_changes == before
        assert c.in_transaction


def assert_parent_reads(client, db, headers, reports, snapshot, queued):
    assert client.get('/v1/health').status_code == 200
    assert client.get(f'/v1/children/{CHILD}/reports').status_code == 401
    daily = client.get(f'/v1/children/{CHILD}/reports', headers=headers)
    assert daily.status_code == 200, daily.text
    assert [row['report_id'] for row in daily.json()['items']] == [reports[1]['report_id']]
    issues = daily.json()['source_issues']
    assert len(issues) == 1 and issues[0]['source_id'] == reports[0]['report_id']
    assert issues[0]['child_id'] == CHILD and issues[0]['status'] == 'open'
    assert issues[0]['automatic_delivery'] is False
    issue_path = f'/v1/parent/children/{CHILD}/report-source-issues'
    assert client.get(issue_path).status_code == 401
    listing = client.get(issue_path, headers=headers)
    assert listing.status_code == 200 and listing.headers['cache-control'] == 'no-store'
    assert listing.json()['items'][0]['snapshot_id'] == snapshot['snapshot_id']
    assert client.get(f'/v1/parent/children/{OTHER}/report-source-issues', headers=headers).json()['items'] == []
    assert client.get('/v1/parent/children/no-child/report-source-issues', headers=headers).status_code == 404
    snapshots = client.get(f'/v1/parent/children/{CHILD}/report-snapshots', headers=headers)
    assert snapshots.status_code == 200, snapshots.text
    old = next(row for row in snapshots.json()['items'] if row['snapshot_id'] == snapshot['snapshot_id'])
    assert old['status'] == 'revoked' and old['correction_status']['source_current'] is False
    assert any(row['source_id'] == reports[0]['report_id'] for row in snapshots.json()['source_issues'])
    other = client.get(f'/v1/children/{OTHER}/reports', headers=headers)
    assert other.status_code == 200 and other.json()['source_issues'] == []
    assert other.json()['items'][0]['report_id'] == reports[2]['report_id']
    dashboard = client.get('/v1/parent/dashboard', headers=headers)
    assert dashboard.status_code == 200, dashboard.text
    assert next(row for row in dashboard.json()['children'] if row['child_id'] == CHILD)['source_issues']
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM daily_report').fetchone()[0] == 3
        assert c.execute('SELECT status FROM report_delivery_outbox WHERE delivery_id=?', (queued['delivery_id'],)).fetchone()[0] == 'cancelled'
        assert c.execute('SELECT COUNT(*) FROM report_delivery_attempt').fetchone()[0] == 0


def test_live_lists_isolate_missing_reflection_even_when_learning_fingerprint_unchanged(source_host):
    client, db, settings, headers, sessions, reports, snapshot, queued, original = source_host
    remove_reflection(db, sessions[0])
    assert_parent_reads(client, db, headers, reports, snapshot, queued)
    with db.connect() as c:
        count = c.execute("SELECT COUNT(*) FROM audit_event WHERE action='parent_report_source_issue'").fetchone()[0]
    assert_parent_reads(client, db, headers, reports, snapshot, queued)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='parent_report_source_issue'").fetchone()[0] == count


def test_actual_restart_retains_invalid_legacy_report_but_health_and_both_children_work(source_host):
    client, db, settings, headers, sessions, reports, snapshot, queued, original = source_host
    remove_reflection(db, sessions[0], stale=True)
    # Explicitly stop the original lifespan before starting the recovered host.
    client.__exit__(None, None, None)
    for _ in range(2):
        with TestClient(create_app(settings)) as restarted:
            token = post(restarted, '/v1/parent/unlock', pin='2468')['parent_token']
            assert_parent_reads(restarted, db, {'X-HSC-Parent': token}, reports, snapshot, queued)


def test_repository_does_not_hide_database_failures(source_host, monkeypatch):
    _, db, _, _, _, _, _, _, _ = source_host
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic database read failure')
    monkeypatch.setattr(StudyRepository, 'get_daily_report', broken)
    with pytest.raises(sqlite3.OperationalError, match='synthetic database read failure'):
        StudyRepository(db).list_daily_reports(CHILD)


def test_direct_snapshot_read_revokes_missing_source_and_exact_original_restore_does_not_reapprove(source_host):
    client, db, settings, headers, sessions, reports, snapshot, queued, original = source_host
    remove_reflection(db, sessions[0])
    path = f"/v1/parent/report-snapshots/{snapshot['snapshot_id']}"
    direct = client.get(path, headers=headers)
    assert direct.status_code == 200, direct.text
    assert direct.json()['status'] == 'revoked'
    assert direct.json()['correction_status']['source_current'] is False
    with db.connect() as c:
        # Restore the exact original fact, never invent reflection or learning results.
        columns = ','.join(original)
        c.execute(f"INSERT INTO daily_reflection({columns}) VALUES ({','.join('?' for _ in original)})", tuple(original.values()))
        c.commit()
    repaired = client.get(f'/v1/children/{CHILD}/reports', headers=headers)
    assert repaired.status_code == 200 and repaired.json()['source_issues'] == []
    assert {item['report_id'] for item in repaired.json()['items']} == {item['report_id'] for item in reports[:2]}
    assert client.get(path, headers=headers).json()['status'] == 'revoked'
    assert client.post(path + '/approve', headers=headers, json={}).status_code == 409
    with db.connect() as c:
        history = [json.loads(row[0]) for row in c.execute("SELECT summary_json FROM audit_event WHERE child_id=? AND action='parent_report_source_issue' ORDER BY rowid", (CHILD,))]
        assert [item['status'] for item in history] == ['open', 'resolved']
        assert c.execute('SELECT status FROM report_delivery_outbox WHERE delivery_id=?', (queued['delivery_id'],)).fetchone()[0] == 'cancelled'
        assert c.execute('SELECT COUNT(*) FROM report_delivery_attempt').fetchone()[0] == 0


@pytest.mark.parametrize('entry', ['list', 'reconcile'])
def test_issue_persistence_failure_rolls_back_snapshot_and_queue_instead_of_hiding_database_error(source_host, entry):
    _, db, _, _, sessions, _, snapshot, queued, _ = source_host
    remove_reflection(db, sessions[0])
    with db.connect() as c:
        c.execute("""CREATE TRIGGER fail_source_issue BEFORE INSERT ON audit_event
            WHEN NEW.action='parent_report_source_issue'
            BEGIN SELECT RAISE(ABORT,'synthetic issue storage failure'); END""")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic issue storage failure'):
        if entry == 'reconcile':
            ParentReportService(db).reconcile_daily_sources(CHILD)
        else:
            StudyRepository(db).list_daily_reports(CHILD)
    with db.connect() as c:
        assert c.execute('SELECT status FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot['snapshot_id'],)).fetchone()[0] == 'approved'
        assert c.execute('SELECT status FROM report_delivery_outbox WHERE delivery_id=?', (queued['delivery_id'],)).fetchone()[0] == 'scheduled'
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='parent_report_source_issue'").fetchone()[0] == 0
        c.execute('DROP TRIGGER fail_source_issue')
        c.commit()
    StudyRepository(db).list_daily_reports(CHILD)
    assert ParentReportService(db).get(snapshot['snapshot_id'])['status'] == 'revoked'
