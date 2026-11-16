"""Optional existing signed sample. No signing, corpus creation or model calls."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from home_study_agent.database import Database
from home_study_agent.parent_reports import ParentReportService, SnapshotStateError
from home_study_agent.period_reports import PeriodReportService
from home_study_agent.period_written_observations import observations_for_period
from home_study_agent.report_source_reads import report_source_connection
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_transaction import validation_session


@pytest.fixture
def signed(tmp_path, monkeypatch):
    configured = os.environ.get('HSC_SIGNED_REPORT_SEED')
    if not configured:
        pytest.skip('Existing approved signed sample required; never generate one here')
    seed = Path(configured)
    source = seed / 'case.db'
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / 'case.db'
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as c:
        with closing(sqlite3.connect(target)) as copy:
            c.backup(copy)
    trust = tmp_path / 'public-trust.json'
    shutil.copy2(seed / 'public-trust.json', trust)
    monkeypatch.setenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE', str(trust))
    db = Database(target)
    PeriodReportService(db).initialize()
    with db.connect() as c:
        row = c.execute('''SELECT a.assessment_id,a.payload_json,p.* FROM rubric_assessment a
            JOIN rubric_calibration_protocol p ON p.child_id=a.child_id AND p.protocol_id=
              json_extract(a.payload_json,'$.calibration_binding.protocol_id')
            WHERE NOT EXISTS(SELECT 1 FROM rubric_review_event r WHERE r.assessment_id=a.assessment_id)''').fetchone()
        assert row is not None
        data = dict(row)
    yield db, data, trust
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def observations(c, child, at=None):
    return observations_for_period(c, child, '2026-09-01', '2026-09-30', {},
        now=at or datetime.now(timezone.utc))


def original_artifact(c, row):
    evidence = json.loads(row['manifest_json'])['cases'][0]['evidence_id']
    return c.execute('''SELECT artifact_id FROM capture_artifact WHERE
        artifact_type='writing_original' AND json_extract(details_json,'$.attempt_binding.evidence_id')=?''',
        (evidence,)).fetchone()[0]


def test_signed_period_reads_replay_dependencies_but_not_full_proof(signed, monkeypatch):
    db, row, _ = signed
    calls = []
    original = RubricCalibrationService._read_rows
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(RubricCalibrationService, '_read_rows', counted)
    with validation_session():
        for _ in range(2):
            with report_source_connection(db) as c:
                c.execute('BEGIN')
                found = observations(c, row['child_id'])
                assert len(found) == 1 and found[0]['score_status'] == 'calibrated_model'
                assert found[0]['mastery_claim_allowed'] is False
                assert observations(c, 'child-middle-3' if row['child_id'] != 'child-middle-3' else 'child-junior-3') == []
        assert len(calls) == 1
    with report_source_connection(db) as c:
        assert len(observations(c, row['child_id'])) == 1
    assert len(calls) == 2, 'No proof may survive the completed synchronous scope'


def test_signed_source_write_rollback_and_restart_are_fresh(signed):
    db, row, _ = signed
    with validation_session(), report_source_connection(db) as c:
        c.execute('BEGIN IMMEDIATE')
        assert len(observations(c, row['child_id'])) == 1
        artifact = original_artifact(c, row)
        c.execute('SAVEPOINT original_change')
        c.execute("UPDATE capture_artifact SET extracted_text=extracted_text || ' changed' WHERE artifact_id=?", (artifact,))
        assert observations(c, row['child_id']) == []
        c.execute('ROLLBACK TO original_change')
        c.execute('RELEASE original_change')
        assert len(observations(c, row['child_id'])) == 1
        c.rollback()
    with report_source_connection(Database(db.path)) as c:
        assert len(observations(c, row['child_id'])) == 1


@pytest.mark.parametrize('change', ['trust', 'expiry', 'withdrawal'])
def test_signed_report_never_reuses_changed_authority(signed, change):
    db, row, trust = signed
    with validation_session(), report_source_connection(db) as c:
        c.execute('BEGIN IMMEDIATE')
        assert len(observations(c, row['child_id'])) == 1
        at = None
        if change == 'trust':
            trust.write_text('{}')
        elif change == 'expiry':
            scorer = json.loads(row['scorer_json'])
            revision = RubricCalibrationService(db)._resolve(c, row['child_id'], 'scorer_revision', scorer['revision_receipt_id'])
            at = datetime.fromisoformat(revision['expires_at'])
        else:
            evidence = json.loads(row['payload_json'])['source_evidence_id']
            c.execute('INSERT INTO period_report_evidence_state VALUES(?,?,1,?,?)',
                (evidence, row['child_id'], 'isolated withdrawal', datetime.now(timezone.utc).isoformat()))
        assert observations(c, row['child_id'], at) == []
        c.rollback()


def test_signed_concurrent_source_change_cannot_freeze_old_period(signed, monkeypatch):
    db, row, _ = signed
    period = PeriodReportService(db).generate(row['child_id'], 'monthly')
    service = ParentReportService(db)
    fingerprint = ParentReportService.source_qualification_fingerprint
    changed = []
    def after_read(c, child, kind, source):
        value = fingerprint(c, child, kind, source)
        if kind == 'period_report' and not changed:
            with db.connect() as writer:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute("UPDATE capture_artifact SET extracted_text=extracted_text || ' changed' WHERE artifact_id=?",
                    (original_artifact(writer, row),))
                writer.commit()
            changed.append(True)
        return value
    monkeypatch.setattr(ParentReportService, 'source_qualification_fingerprint', staticmethod(after_read))
    with pytest.raises(SnapshotStateError):
        service.create_from_period(period)
    assert changed == [True]
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM parent_report_snapshot WHERE child_id=?', (row['child_id'],)).fetchone()[0] == 0


def test_signed_low_human_correction_and_retraction_never_fall_back(signed):
    from home_study_agent.learning_rubric import RubricEvidenceService
    from home_study_agent.writing_rubric import prepare_writing_review
    db, row, _ = signed
    service = RubricEvidenceService(db)
    with validation_session():
        with report_source_connection(db) as c:
            assert len(observations(c, row['child_id'])) == 1
        assessment = service.get(row['child_id'], row['assessment_id'])
        with db.connect() as c:
            binding = prepare_writing_review(c, row['child_id'], row['assessment_id'])
        text = assessment['response']
        service.review(row['child_id'], row['assessment_id'], expected_revision=0,
            client_action_id='isolated-report-low-human', reviewer_id='parent', reviewer_role='parent',
            operation='approve', reason='Synthetic original review for consumer source isolation',
            independent=True, hint_level=0, review_profile=binding['profile'],
            writing_evidence={'binding_version': binding['binding_version'],
                'task_point_evidence': [{'task_point': point, 'response_quote': text} for point in binding['task_points']]},
            dimensions=[{'dimension_id': d['dimension_id'], 'score': 0, 'response_quotes': [text],
                         'rationale': 'Synthetic low original-response score'} for d in assessment['dimensions']],
            criterion_evidence=[{'criterion': p, 'response_quote': text} for p in assessment['criteria']])
        with report_source_connection(db) as c:
            values = observations(c, row['child_id'])
            assert len(values) == 1 and values[0]['score_status'] == 'reviewed_human'
            assert values[0]['passed'] is False and values[0]['mastery_claim_allowed'] is False
        service.review(row['child_id'], row['assessment_id'], expected_revision=1,
            client_action_id='isolated-report-withdraw-human', reviewer_id='parent', reviewer_role='parent',
            operation='retract', reason='Withdraw this synthetic human review')
        with report_source_connection(db) as c:
            assert observations(c, row['child_id']) == []
    with report_source_connection(Database(db.path)) as c:
        assert observations(c, row['child_id']) == []
