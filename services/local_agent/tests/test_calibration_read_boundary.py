"""Small read-boundary probes, not a certificate or a quality benchmark."""
from datetime import timedelta

import pytest

from home_study_agent.diagnostic_prompt import _TransactionDatabase
from home_study_agent.learning_rubric import RubricEvidenceService
from home_study_agent.rubric_calibration import RubricCalibrationService
from home_study_agent.rubric_calibration_evidence import source_snapshot
from test_rubric_calibration import corpus, flow, CHILD
from test_visual_rubric_identity import published, assessment


@pytest.mark.parametrize('change', ['protocol_revoke', 'human_retract', 'time_expiry'])
def test_same_connection_reads_recheck_mutable_authorities(corpus, change):
    corpus.freeze()
    aid, labels = corpus.score(gold=2)
    corpus.service.record_case(CHILD, corpus.pid, assessment_id=aid, blind_receipt_ids=labels,
        expected_revision=1, client_action_id='one-case')
    service = corpus.service
    reference = {'assessment_id': aid, 'blind_receipt_ids': labels}
    now = corpus.clock[0]
    with corpus.db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = service._owned(c, CHILD, corpus.pid)
        valid_case = service._case(c, row, reference, now)
        baseline = service._read(c, row, now)
        assert not baseline['score_qualified']
        assert 'boundary_samples_insufficient' in baseline['blockers']
        assert not {'qualification_revoked', 'assessment_retracted', 'protocol_expired'} & set(baseline['blockers'])
        assert service._case(c, row, reference, now) == valid_case
        assert service._read(c, row, now) == baseline
        with pytest.raises(KeyError):
            service._owned(c, 'child-middle-3', corpus.pid)
        changes = c.total_changes
        data_version = c.execute('PRAGMA data_version').fetchone()[0]

        if change == 'protocol_revoke':
            borrowed = RubricCalibrationService(_TransactionDatabase(c),
                authority_resolver=corpus.resolve, clock=lambda: now)
            revoked = borrowed.revoke(CHILD, corpus.pid, expected_revision=2, client_action_id='within-tx-revoke')
            assert revoked['current']['status'] == 'revoked'
            reason = 'qualification_revoked'
        elif change == 'human_retract':
            RubricEvidenceService(_TransactionDatabase(c)).review(CHILD, aid,
                expected_revision=2, client_action_id='within-tx-retract', reviewer_id='test-reviewer-1',
                reviewer_role='teacher', operation='retract', reason='Withdraw this original label')
            with pytest.raises(ValueError, match='ASSESSMENT_RETRACTED'):
                service._case(c, row, reference, now)
            reason = 'assessment_retracted'
        else:
            now += timedelta(days=31)
            assert c.total_changes == changes
            reason = 'protocol_expired'

        assert c.in_transaction, 'Real writers must stay in the borrowed transaction'
        assert c.execute('PRAGMA data_version').fetchone()[0] == data_version
        current = service._read(c, row, now)
        assert reason in current['blockers'], 'The earlier read cannot replace current authority checks'
        receipt = service.qualified_score_receipt(c, CHILD, aid, protocol_id=corpus.pid, as_of=now)
        assert not receipt['score_qualified']
        assert reason in receipt['blockers']
        c.rollback()

        # Same connection, next transaction: rollback must not retain a negative cache.
        c.execute('BEGIN')
        assert service._read(c, row, corpus.clock[0]) == baseline
        assert service._case(c, row, reference, corpus.clock[0]) == valid_case
        c.rollback()


def test_source_file_change_invalidates_receipt_dependencies_without_sql_write(tmp_path, monkeypatch):
    with published(tmp_path, monkeypatch) as case:
        rubrics, snap = assessment(case, monkeypatch)
        paths = list((case.settings.data_dir / 'media').rglob('*.enc'))
        assert len(paths) == 1, 'Use only the exact fixture-managed original image'
        path = paths[0]
        original = path.read_bytes()
        with case.db.connect() as c:
            c.execute('BEGIN')
            before = source_snapshot(c, 'child', snap['source_evidence_id'])
            assert rubrics.get('child', snap['assessment_id'], connection=c)['source_status'] == 'current'
            changes = c.total_changes
            version = c.execute('PRAGMA data_version').fetchone()[0]
            try:
                path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
                assert c.total_changes == changes
                assert c.execute('PRAGMA data_version').fetchone()[0] == version
                with pytest.raises(ValueError):
                    source_snapshot(c, 'child', snap['source_evidence_id'])
                current = rubrics.get('child', snap['assessment_id'], connection=c)
                assert current['source_status'] == 'changed'
                assert current['material']['visual']['image_availability'] == 'unavailable'
            finally:
                path.write_bytes(original)
            assert source_snapshot(c, 'child', snap['source_evidence_id']) == before
            assert rubrics.get('child', snap['assessment_id'], connection=c)['source_status'] == 'current'
            c.rollback()
