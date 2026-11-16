"""Optional prewarming must not repeatedly evaluate incomplete certificates."""
import json
import sqlite3

import pytest

from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_transaction import _warm_score_protocols


@pytest.fixture
def connection():
    c = sqlite3.connect(':memory:')
    c.row_factory = sqlite3.Row
    c.executescript('''
        CREATE TABLE rubric_assessment(assessment_id TEXT, child_id TEXT, payload_json TEXT);
        CREATE TABLE rubric_review_event(assessment_id TEXT);
        CREATE TABLE rubric_calibration_protocol(protocol_id TEXT, child_id TEXT);
        CREATE TABLE rubric_calibration_event(protocol_id TEXT, operation TEXT);
    ''')
    try:
        yield c
    finally:
        c.close()


def add(c, protocol, child='child', *, qualified=False, reviewed=False):
    c.execute('INSERT INTO rubric_calibration_protocol VALUES (?,?)', (protocol, child))
    c.execute('INSERT INTO rubric_assessment VALUES (?,?,?)', (protocol, child,
        json.dumps({'calibration_binding': {'protocol_id': protocol}})))
    if qualified:
        c.execute("INSERT INTO rubric_calibration_event VALUES (?,'qualify')", (protocol,))
    if reviewed:
        c.execute('INSERT INTO rubric_review_event VALUES (?)', (protocol,))


def test_only_qualified_unreviewed_owned_protocols_are_optional_warm_candidates(connection, monkeypatch):
    add(connection, 'incomplete')
    add(connection, 'qualified', qualified=True)
    add(connection, 'already-reviewed', qualified=True, reviewed=True)
    add(connection, 'another-child', child='other', qualified=True)
    seen = []
    monkeypatch.setattr(RubricCalibrationService, '_read',
        lambda self, c, row, now: seen.append(row['protocol_id']))
    before = connection.total_changes
    assert _warm_score_protocols(connection, None, 'child') is False
    assert seen == ['qualified']
    assert connection.total_changes == before


def test_missing_calibration_event_journal_is_not_a_warm_authority(connection, monkeypatch):
    add(connection, 'unsealed')
    connection.execute('DROP TABLE rubric_calibration_event')
    monkeypatch.setattr(RubricCalibrationService, '_read',
        lambda *args: pytest.fail('incomplete journal must not be prewarmed'))
    assert _warm_score_protocols(connection, None, 'child') is False


def test_existing_qualification_does_not_bypass_live_revalidation(connection, monkeypatch):
    add(connection, 'expired-or-revoked', qualified=True)
    connection.execute("INSERT INTO rubric_calibration_event VALUES ('expired-or-revoked','revoke')")
    seen = []
    def stale(self, c, row, now):
        seen.append(row['protocol_id'])
        raise ValueError('RUBRIC_CALIBRATION_EXECUTION_AUTHORITY_CHANGED')
    monkeypatch.setattr(RubricCalibrationService, '_read', stale)
    assert _warm_score_protocols(connection, None, 'child') is False
    assert seen == ['expired-or-revoked']


def test_new_qualification_is_visible_without_cached_unqualified_state(connection, monkeypatch):
    add(connection, 'newly-qualified')
    seen = []
    monkeypatch.setattr(RubricCalibrationService, '_read',
        lambda self, c, row, now: seen.append(row['protocol_id']))
    assert _warm_score_protocols(connection, None, 'child') is False
    assert seen == []
    connection.execute("INSERT INTO rubric_calibration_event VALUES ('newly-qualified','qualify')")
    connection.execute('INSERT INTO rubric_assessment VALUES (?,?,?)', ('same-protocol-second-answer',
        'child', json.dumps({'calibration_binding': {'protocol_id': 'newly-qualified'}})))
    assert _warm_score_protocols(connection, None, 'child') is False
    assert seen == ['newly-qualified']
