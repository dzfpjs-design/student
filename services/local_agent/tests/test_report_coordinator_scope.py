"""The real app's synchronous report coordinator owns one short proof scope."""
import asyncio
import inspect
import json
from datetime import datetime, timezone

import httpx
import pytest

from home_study_agent.app import create_app
from home_study_agent.config import Settings
from home_study_agent.model_gateway import ModelGateway
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_transaction import _SESSION
from home_study_agent.report_source_reads import report_source_connection
from home_study_agent.parent_reports import SnapshotStateError
from test_report_source_signed_scope import signed, observations, original_artifact


def find_coordinator(function, seen=None):
    seen = set() if seen is None else seen
    if not inspect.isfunction(function) or id(function) in seen:
        return None
    seen.add(id(function))
    if function.__name__ == 'refresh_parent_period_snapshots':
        return function
    for cell in function.__closure__ or ():
        found = find_coordinator(cell.cell_contents, seen)
        if found is not None:
            return found
    return None


@pytest.fixture
def coordinator(signed):
    db, row, trust = signed
    settings = Settings(data_dir=db.path.parent, database_path=db.path,
        maintenance_enabled=False, model_provider='qwen', model_name='isolated-model',
        model_api_key='synthetic-69c92b8a1f26c7ac5e4763bd', model_base_url='https://isolated.invalid/v1')
    calls = []
    async def no_network(request):
        calls.append(request.method)
        raise AssertionError('No model request allowed')
    gateway = ModelGateway(settings, transport=httpx.MockTransport(no_network))
    app = create_app(settings, model_gateway=gateway)
    function = find_coordinator(app.router.lifespan_context)
    assert function is not None and not inspect.iscoroutinefunction(function)
    try:
        yield function, app, db, row, trust
    finally:
        asyncio.run(app.state.voice_synthesis.close())
        asyncio.run(gateway.close())
        assert calls == []
        assert _SESSION.get() is None


def test_coordinator_shares_one_scope_and_repeats_no_cold_proof_within_it(coordinator, monkeypatch):
    function, app, db, row, _ = coordinator
    original = RubricCalibrationService._read_rows
    calls, scopes = [], []
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(RubricCalibrationService, '_read_rows', counted)
    for service, method in [(app.state.period_reports, 'refresh_all_current'),
                            (app.state.parent_reports, 'create_from_period')]:
        original_method = getattr(service, method)
        def observed(*args, _original=original_method, **kwargs):
            scopes.append(_SESSION.get())
            return _original(*args, **kwargs)
        monkeypatch.setattr(service, method, observed)
    result = function()
    assert len(result['reports']) == 2 and result['failures'] == []
    assert len(calls) == 1, 'Separate refresh/freeze scopes repeat the signed cold proof'
    assert len(scopes) == 3 and scopes[0] is not None
    assert all(scope is scopes[0] for scope in scopes)
    assert _SESSION.get() is None and scopes[0] == {}
    with db.connect() as c:
        snapshots = c.execute('SELECT child_id,status FROM parent_report_snapshot').fetchall()
        assert len(snapshots) == 2
        assert all(r['child_id'] == row['child_id'] and r['status'] == 'draft' for r in snapshots)
    function()
    assert len(calls) == 2, 'A completed call must not retain its proof into the next call'
    assert scopes[3] is not scopes[0] and _SESSION.get() is None


@pytest.mark.parametrize('change', ['withdrawal', 'delete_artifact', 'delete_protocol', 'trust', 'expiry'])
def test_coordinator_rechecks_live_authority_before_first_freeze(coordinator, monkeypatch, change):
    function, app, db, row, trust = coordinator
    refresh = app.state.period_reports.refresh_all_current
    seen = []
    def changed():
        reports = refresh()
        assert len(reports['reports']) == 2
        seen.append(_SESSION.get())
        if change == 'trust':
            trust.write_text('{}')
        elif change == 'expiry':
            from home_study_agent import parent_reports
            expires = datetime.fromisoformat(row['expires_at'])
            monkeypatch.setattr(parent_reports, 'utc_now', lambda: expires.isoformat())
        else:
            with db.connect() as writer:
                writer.execute('BEGIN IMMEDIATE')
                if change == 'withdrawal':
                    evidence = json.loads(row['payload_json'])['source_evidence_id']
                    writer.execute('INSERT INTO period_report_evidence_state VALUES(?,?,1,?,?)',
                        (evidence, row['child_id'], 'isolated withdrawal', datetime.now(timezone.utc).isoformat()))
                elif change == 'delete_artifact':
                    writer.execute('DELETE FROM capture_artifact WHERE artifact_id=?',
                        (original_artifact(writer, row),))
                else:
                    writer.execute('DELETE FROM rubric_calibration_protocol WHERE protocol_id=? AND child_id=?',
                        (row['protocol_id'], row['child_id']))
                writer.commit()
        return reports
    monkeypatch.setattr(app.state.period_reports, 'refresh_all_current', changed)
    with pytest.raises(SnapshotStateError):
        function()
    assert len(seen) == 1 and seen[0] is not None and seen[0] == {}
    assert _SESSION.get() is None
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM parent_report_snapshot').fetchone()[0] == 0


def test_coordinator_transaction_aba_rollback_and_other_child_stay_fresh(coordinator, monkeypatch):
    function, app, db, row, _ = coordinator
    refresh = app.state.period_reports.refresh_all_current
    checks = []
    def changed():
        reports = refresh()
        assert _SESSION.get() is not None
        with report_source_connection(db) as c:
            c.execute('BEGIN IMMEDIATE')
            assert len(observations(c, row['child_id'])) == 1
            c.rollback()
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM capture_artifact WHERE artifact_id=?', (original_artifact(c, row),))
            assert observations(c, row['child_id']) == []
            other = 'child-middle-3' if row['child_id'] != 'child-middle-3' else 'child-junior-3'
            assert observations(c, other) == []
            c.rollback()
            c.execute('BEGIN')
            assert len(observations(c, row['child_id'])) == 1
            assert observations(c, other) == []
            c.rollback()
        checks.append(True)
        return reports
    monkeypatch.setattr(app.state.period_reports, 'refresh_all_current', changed)
    assert len(function()['reports']) == 2 and checks == [True]
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM parent_report_snapshot WHERE child_id=?', (row['child_id'],)).fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM parent_report_snapshot WHERE child_id!=?', (row['child_id'],)).fetchone()[0] == 0


@pytest.mark.parametrize('error_type', [RuntimeError, asyncio.CancelledError])
def test_coordinator_exception_clears_scope_and_next_call_is_cold(coordinator, monkeypatch, error_type):
    function, app, db, _, _ = coordinator
    original = RubricCalibrationService._read_rows
    calls, scopes = [], []
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(RubricCalibrationService, '_read_rows', counted)
    create = app.state.parent_reports.create_from_period
    def failed(report):
        scopes.append(_SESSION.get())
        raise error_type('isolated cancellation or failure')
    monkeypatch.setattr(app.state.parent_reports, 'create_from_period', failed)
    with pytest.raises(error_type):
        function()
    assert len(calls) == 1 and scopes[0] is not None and scopes[0] == {}
    assert _SESSION.get() is None
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM parent_report_snapshot').fetchone()[0] == 0
    monkeypatch.setattr(app.state.parent_reports, 'create_from_period', create)
    assert len(function()['reports']) == 2 and len(calls) == 2
    assert _SESSION.get() is None


def test_coordinator_rejects_other_child_report_dict(coordinator, monkeypatch):
    function, app, db, row, _ = coordinator
    refresh = app.state.period_reports.refresh_all_current
    def other_child():
        result = refresh()
        other = 'child-middle-3' if row['child_id'] != 'child-middle-3' else 'child-junior-3'
        result['reports'][0] = {**result['reports'][0], 'child_id': other}
        return result
    monkeypatch.setattr(app.state.period_reports, 'refresh_all_current', other_child)
    with pytest.raises(SnapshotStateError):
        function()
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM parent_report_snapshot').fetchone()[0] == 0


def test_coordinator_final_writer_rechecks_after_old_fingerprint(coordinator, monkeypatch):
    from home_study_agent.parent_reports import ParentReportService
    function, app, db, row, _ = coordinator
    refresh = app.state.period_reports.refresh_all_current
    fingerprint = ParentReportService.source_qualification_fingerprint
    armed, changed = [], []
    def refreshed():
        value = refresh()
        armed.append(True)
        return value
    def after_old_fingerprint(c, child, kind, source):
        value = fingerprint(c, child, kind, source)
        if armed and not changed and kind == 'period_report':
            with db.connect() as writer:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute("UPDATE capture_artifact SET extracted_text=extracted_text || ' changed' WHERE artifact_id=?",
                    (original_artifact(writer, row),))
                writer.commit()
            changed.append(True)
        return value
    monkeypatch.setattr(app.state.period_reports, 'refresh_all_current', refreshed)
    monkeypatch.setattr(ParentReportService, 'source_qualification_fingerprint', staticmethod(after_old_fingerprint))
    with pytest.raises(SnapshotStateError):
        function()
    assert changed == [True] and _SESSION.get() is None
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM parent_report_snapshot').fetchone()[0] == 0


def test_coordinator_maintenance_thread_scope_ends_before_caller_await_resumes(coordinator, monkeypatch):
    function, app, _, _, _ = coordinator
    refresh = app.state.period_reports.refresh_all_current
    scopes = []
    def observed():
        scopes.append(_SESSION.get())
        return refresh()
    monkeypatch.setattr(app.state.period_reports, 'refresh_all_current', observed)
    def worker():
        assert _SESSION.get() is None
        result = function()
        assert _SESSION.get() is None
        return result
    async def maintenance():
        for _ in range(2):
            assert _SESSION.get() is None
            result = await asyncio.to_thread(worker)
            assert len(result['reports']) == 2 and _SESSION.get() is None
    asyncio.run(maintenance())
    assert len(scopes) == 2 and all(scope is not None and scope == {} for scope in scopes)
    assert scopes[0] is not scopes[1]
