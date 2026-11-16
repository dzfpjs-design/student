import json

import pytest

from home_study_agent.database import Database
from home_study_agent.paper_printed_score_candidate import (
    PaperPrintedScoreCandidateService, PURPOSE, execution_binding, parse,
)
from home_study_agent.paper_scoring import PaperScoringService
from home_study_agent.paper_subjective_rubric import PaperSubjectiveRubricService
from home_study_agent.repository import StudyRepository
from test_paper_response_origin import prepared_visual


def prepared_candidate(tmp_path):
    db, request, aid = prepared_visual(tmp_path, unmarked=True)
    ctx = PaperSubjectiveRubricService(db).execution_context('child', aid)
    points = [dict(point_id=p['point_id'], status='met', score=str(p['maximum_score']),
                   response_quote='80', reason='原作答中可见80') for p in ctx['criteria']]
    payload = json.dumps({'points': points}, ensure_ascii=False)
    repo = StudyRepository(db)
    call = repo.start_model_call(request['session_id'], 'child', PURPOSE, 'fixture', 'fixture-model', 8,
        'paper-printed-test-v1', {'paper_execution': execution_binding(ctx)}, 'medium', 'fixture', 'fixture', None)
    repo.finish_model_call(call, 'completed', 1, len(payload), provider='fixture', model='fixture-model',
                           verification_status='partially_verified', safety_status='passed')
    return db, request, aid, call, payload


def test_printed_candidate_survives_restart_but_is_never_score_authority(tmp_path):
    db, request, aid, call, payload = prepared_candidate(tmp_path)
    service = PaperPrintedScoreCandidateService(db)
    result = service.accept('child', aid, call, payload)
    assert result['total_score'] == '3.5' and result['maximum_score'] == '3.5'
    assert result['points'][0]['score'] == '2.5'
    assert result['score_authority_granted'] is False and result['independent_calibration_required'] is True
    assert result['cloud_transmission_allowed'] is False and result['model_revision_verified'] is False
    assert service.accept('child', aid, call, payload) == result
    assert PaperPrintedScoreCandidateService(Database(db.path, runtime_mode=db.runtime_mode)).current('child', result['artifact_id']) == result
    view = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')
    assert view['authority_supply']['printed_score_candidate']['artifact_id'] == result['artifact_id']
    assert view['decision_id'] is None and view['authority_supply']['status'] == 'calibration_pending'
    assert PaperScoringService(db).resolve(request['request_id'], 'child', 'no-self-calibration') is None
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='paper_printed_score_candidate'").fetchone()[0] == 1


@pytest.mark.parametrize('mutation', ['half_points', 'quote', 'extra_field', 'missing_point', 'duplicate_point', 'boolean', 'nan'])
def test_unsupported_model_points_are_rejected_without_writing(mutation, tmp_path):
    db, _, aid, call, payload = prepared_candidate(tmp_path)
    body = json.loads(payload)
    if mutation == 'half_points': body['points'][0]['score'] = '1.25'
    if mutation == 'quote': body['points'][0]['response_quote'] = '这段不在原作答中'
    if mutation == 'extra_field': body['confidence'] = .99
    if mutation == 'missing_point': body['points'].pop()
    if mutation == 'duplicate_point': body['points'][1] = body['points'][0]
    if mutation == 'boolean': body['points'][0]['score'] = True
    if mutation == 'nan': body['points'][0]['score'] = 'NaN'
    with pytest.raises(ValueError, match='PRINTED_'):
        PaperPrintedScoreCandidateService(db).accept('child', aid, call, json.dumps(body))
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='paper_printed_score_candidate'").fetchone()[0] == 0


def test_abstention_has_no_total_and_cannot_be_replaced_on_retry(tmp_path):
    db, _, aid, call, payload = prepared_candidate(tmp_path)
    body = json.loads(payload)
    body['points'][0].update(status='abstain', score=None, reason='当前作答不够核对依据')
    service = PaperPrintedScoreCandidateService(db)
    result = service.accept('child', aid, call, json.dumps(body))
    assert result['total_score'] is None and result['status'] == 'candidate_abstained'
    with pytest.raises(ValueError, match='PRINTED_CANDIDATE_CONFLICT'):
        service.accept('child', aid, call, payload)


@pytest.mark.parametrize('mutation', ['call_scope', 'call_failed', 'call_manifest', 'page_changed'])
def test_scope_call_and_original_are_rechecked_at_acceptance(mutation, tmp_path):
    db, _, aid, call, payload = prepared_candidate(tmp_path)
    with db.connect() as c:
        if mutation == 'call_scope': c.execute('UPDATE model_call SET child_id=NULL WHERE call_id=?', (call,))
        if mutation == 'call_failed': c.execute("UPDATE model_call SET status='failed' WHERE call_id=?", (call,))
        if mutation == 'call_manifest': c.execute("UPDATE model_call SET context_manifest_json='{}' WHERE call_id=?", (call,))
        if mutation == 'page_changed': c.execute("UPDATE capture_artifact SET extracted_text='changed original' WHERE artifact_id='page'")
        c.commit()
    with pytest.raises(ValueError):
        PaperPrintedScoreCandidateService(db).accept('child', aid, call, payload)


def test_changed_original_withdraws_candidate_projection_without_overwriting_audit(tmp_path):
    db, request, aid, call, payload = prepared_candidate(tmp_path)
    service = PaperPrintedScoreCandidateService(db)
    result = service.accept('child', aid, call, payload)
    with db.connect() as c:
        original = c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (result['artifact_id'],)).fetchone()[0]
        c.execute("UPDATE capture_artifact SET extracted_text='changed original' WHERE artifact_id='page'"); c.commit()
    assert service.current('child', result['artifact_id'])['status'] == 'candidate_withdrawn'
    view = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')
    assert view['authority_supply']['printed_score_candidate']['status'] == 'candidate_withdrawn'
    with db.connect() as c:
        assert c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (result['artifact_id'],)).fetchone()[0] == original
    with pytest.raises(ValueError, match='PRINTED_CANDIDATE_UNKNOWN'):
        service.current('another-child', result['artifact_id'])


def test_duplicate_json_keys_are_not_silently_accepted(tmp_path):
    db, _, aid, call, payload = prepared_candidate(tmp_path)
    with pytest.raises(ValueError, match='PRINTED_DUPLICATE_FIELD'):
        PaperPrintedScoreCandidateService(db).accept('child', aid, call, '{"points":[],"points":'+json.dumps(json.loads(payload)['points'])+'}')


def test_decimal_printed_scale_is_not_changed_by_binary_or_decimal_rounding():
    context = {'response': '80', 'criteria': [
        dict(point_id='one', criterion='列出准确的数量关系', maximum_score=.1, source_quote='列出准确的数量关系（0.1分）'),
        dict(point_id='two', criterion='计算结果与单位正确', maximum_score=.2, source_quote='计算结果与单位正确（0.2分）')]}
    content = json.dumps({'points': [dict(point_id=p['point_id'], status='met', score=str(p['maximum_score']),
        response_quote='80', reason='原答逐字依据') for p in context['criteria']]})
    assert parse(content, context)['total_score'] == '0.3'
    assert parse(content, context)['maximum_score'] == '0.3'
    with pytest.raises(ValueError, match='PRINTED_OUTPUT_INVALID'):
        parse('[' * 2000 + ']' * 2000, context)


def test_candidate_reads_borrow_one_transaction_and_export_delete_survive_restart(tmp_path):
    from pathlib import Path
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, _, aid, call, payload = prepared_candidate(tmp_path)
    result = PaperPrintedScoreCandidateService(db).accept('child', aid, call, payload)
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE model_call SET status='failed' WHERE call_id=?", (call,))
        assert PaperPrintedScoreCandidateService(None).current('child', result['artifact_id'], connection=c)['status'] == 'candidate_withdrawn'
        assert c.in_transaction
        c.rollback()
        assert PaperPrintedScoreCandidateService(None).current('child', result['artifact_id'], connection=c)['status'] == 'candidate_unqualified'
    privacy = PrivacyLifecycleService(db, tmp_path, b'0' * 32)
    privacy.initialize()
    exported = json.loads(Path(privacy.export_child('child')['path']).read_text('utf-8'))
    assert any(row['artifact_id'] == result['artifact_id'] for row in exported['capture_artifacts'])
    privacy.delete_capture_artifact(result['artifact_id'], '确认删除', 'delete-printed-candidate')
    with pytest.raises(ValueError, match='UNKNOWN'):
        PaperPrintedScoreCandidateService(Database(db.path, runtime_mode=db.runtime_mode)).current('child', result['artifact_id'])


def test_candidate_corruption_is_rejected_and_bad_output_can_retry_without_partial_commit(tmp_path):
    db, _, aid, call, payload = prepared_candidate(tmp_path)
    service = PaperPrintedScoreCandidateService(db)
    with pytest.raises(ValueError, match='PRINTED_'):
        service.accept('child', aid, call, '{"points":[]}')
    result = service.accept('child', aid, call, payload)
    with db.connect() as c:
        details = json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (result['artifact_id'],)).fetchone()[0])
        details['total_score'] = '0'
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), result['artifact_id'])); c.commit()
    with pytest.raises(ValueError, match='PRINTED_CANDIDATE_CORRUPT'):
        service.current('child', result['artifact_id'])
