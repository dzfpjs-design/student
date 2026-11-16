"""Executable boundary: subjective ratings cannot bypass period source review."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.report_followup import ReportFollowupConflict, ReportFollowupService
from test_period_report_reliable_evidence import CHILD, answer, flow, learning_clock, report


def read_views(db, snapshot_id, day):
    with db.connect() as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        row = c.execute('SELECT * FROM parent_report_snapshot WHERE snapshot_id=?', (snapshot_id,)).fetchone()
        result = {
            'source_current': ParentReportService.snapshot_source_current(c, row),
            'feedback': ReportFollowupService.current_feedback(c, row),
            'experiences': ReportFollowupService.coaching_experiences(c, CHILD),
            'plan': ReportFollowupService(db).project_for_plan(CHILD, day, connection=c),
        }
        assert c.total_changes == 0
        assert c.in_transaction
        return result


def restart_views(db, snapshot_id, day, clock, tmp_path):
    workspace = Path(__file__).resolve().parents[1]
    import_root = tmp_path / 'fresh-interpreter-imports'
    import_root.mkdir()
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(import_root),
           'HSC_DATA_DIR': str(import_root), 'HSC_DATABASE_PATH': str(import_root / 'import.db'),
           'HSC_MODEL_PROVIDER': 'disabled', 'HSC_ASR_PROVIDER': 'disabled',
           'HSC_TTS_PROVIDER': 'disabled', 'HSC_MAINTENANCE_ENABLED': '0',
           'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': os.pathsep.join([str(workspace / 'src'), str(workspace / 'tests')])}
    program = '''
import socket, sys
def deny(*args, **kwargs):
    raise AssertionError('Restart verification forbids network access')
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
socket.getaddrinfo = deny
import json, pytest
from test_due_review_plan_completion import freeze_clock
patch = pytest.MonkeyPatch()
freeze_clock(patch, [sys.argv[4]])
from home_study_agent.database import Database
from test_v16_period_feedback_dependency_boundary import read_views
print(json.dumps(read_views(Database(sys.argv[1]), sys.argv[2], sys.argv[3])))
'''
    run = subprocess.run([sys.executable, '-B', '-c', program, str(db.path), snapshot_id, day, clock],
                         env=env, cwd=workspace, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.parametrize('kind', ['weekly', 'monthly'])
def test_rating_refresh_reapproval_still_requires_a_separate_plan_authority(flow, kind, tmp_path):
    f = flow
    answer(f)
    parents = ParentReportService(f.db)
    period = report(f, kind)
    original = parents.create_from_period(period)
    parents.approve(original['snapshot_id'])
    with f.db.connect() as c:
        journal = [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')]
        source_text = c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',
                               (period['period_report_id'],)).fetchone()[0]
    followup = ReportFollowupService(f.db)
    parents.record_feedback(original['snapshot_id'], 2, 5, 'Parent reports pressure', 'first-rating')
    feedback = followup.feedback_history(CHILD, original['snapshot_id'])[0]
    day = (datetime.fromisoformat(feedback['created_at']).date() + timedelta(days=1)).isoformat()
    before = read_views(f.db, original['snapshot_id'], day)
    assert not before['source_current']
    assert before['feedback']['feedback_current_status'] == 'source_invalid'
    assert len(before['experiences']) == 1
    assert before['plan']['items'] == before['plan']['constraints'] == []
    assert before == restart_views(f.db, original['snapshot_id'], day, f.clock[0], tmp_path)
    with pytest.raises(ReportFollowupConflict, match='PLAN_ADJUSTMENT_SOURCE_CHANGED'):
        followup.propose_plan_adjustment(CHILD, original['snapshot_id'], 1,
            'daily_total_minutes_cap', 20, day, day, 'blocked-old-cap')
    with f.db.connect() as c:
        assert c.execute('SELECT content_json FROM period_report WHERE period_report_id=?',
                         (period['period_report_id'],)).fetchone()[0] == source_text
    refreshed = PeriodReportService(Database(f.db.path)).get(period['period_report_id'], persist_refresh=True)
    assert refreshed['content']['parent_coaching']['adaptation']['feedback_count'] == 1
    replacement = parents.create_from_period(refreshed)
    assert replacement['status'] == 'draft'
    assert not parents.validate_current_for_delivery(replacement['snapshot_id'])
    parents.approve(replacement['snapshot_id'])
    reviewed = read_views(f.db, replacement['snapshot_id'], day)
    assert reviewed['source_current']
    assert reviewed['feedback']['feedback_current_status'] == 'none'
    assert reviewed['plan']['items'] == reviewed['plan']['constraints'] == []
    with pytest.raises(ReportFollowupConflict, match='FEEDBACK_REVISION_CONFLICT'):
        followup.propose_plan_adjustment(CHILD, replacement['snapshot_id'], 1,
            'daily_total_minutes_cap', 20, day, day, 'blocked-new-cap')
    parents.record_feedback(replacement['snapshot_id'], 2, 5, 'Parent confirms the new wording', 'second-rating')
    after = read_views(f.db, replacement['snapshot_id'], day)
    assert not after['source_current']
    assert len(after['experiences']) == 2
    assert after['plan']['items'] == after['plan']['constraints'] == []
    with pytest.raises(ReportFollowupConflict, match='PLAN_ADJUSTMENT_SOURCE_CHANGED'):
        followup.propose_plan_adjustment(CHILD, replacement['snapshot_id'], 1,
            'daily_total_minutes_cap', 20, day, day, 'blocked-rerated-cap')
    with f.db.connect() as c:
        assert not c.execute('SELECT 1 FROM report_plan_adjustment_event').fetchone()
        assert [tuple(row) for row in c.execute('SELECT * FROM learning_evidence ORDER BY rowid')] == journal
    assert parents.get(original['snapshot_id'])['content'] == original['content']
    assert parents.get(replacement['snapshot_id'])['content'] == replacement['content']
    assert parents.get(original['snapshot_id'])['status'] == 'revoked'
    for item in after['experiences']:
        assert not item['execution_verified'] and not item['learning_effect_verified']
