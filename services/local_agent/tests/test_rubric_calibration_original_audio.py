"""Synthetic real SQLite/HTTP provenance; no model/Windows/qualification claim."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import pytest
import sqlite3
from fastapi.testclient import TestClient

from home_study_agent.database import Database
from home_study_agent.agent_runtime import AgentRuntimeStore
from home_study_agent.pet_speaking_candidate_jobs import SpeakingCandidateJobs, _Claim
from home_study_agent.pet_speaking_candidate_store import SpeakingModelCandidateStore
from home_study_agent.pet_multipa_candidate import prepare_input
from home_study_agent.rubric_calibration import RubricCalibrationService
from test_pet_multipa_reference_worker import make_read_aloud, synthetic_result, ASSETS
from test_pet_oral_supply_http import CHILD, OTHER, BASE, post


@pytest.fixture
def original_cpu_candidate(tmp_path, monkeypatch):
    # The original HTTP fixture supplies reviewed material, actual answer capture,
    # confirmation, assessment and queued job. Deployment is a named test substitute.
    v = make_read_aloud(tmp_path, monkeypatch, configured=True)
    settings = v['settings']
    db = Database(v['db'].path, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    runtime = AgentRuntimeStore(db)
    jobs = SpeakingCandidateJobs(db, runtime, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    claim = jobs._claim()
    assert claim and claim['state'] == 'claimed'
    with db.connect() as c:
        binding, content = prepare_input(c, v['sid'], media_id=claim['media_id'], assessment_id=claim['assessment_id'],
                                        media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    result = synthetic_result(content, binding['reference'])
    store = SpeakingModelCandidateStore(db, media_root=settings.data_dir, data_encryption_key=settings.data_encryption_key)
    saved = store.record(CHILD, v['sid'], binding=binding, result=result, asset_hashes=ASSETS,
                         client_action_id=claim['client_action_id'], job_claim=_Claim(claim))
    done = jobs.read(CHILD, claim['job_id'])
    assert done['state'] == 'completed' and done['candidate_id'] == saved['current']['candidate_id']
    from home_study_agent import pet_multipa_epoch as epoch
    monkeypatch.delitem(epoch._REVIEWED_DEPLOYMENTS, 'REFERENCE_TEST_ONLY')
    assert epoch._REVIEWED_DEPLOYMENTS == {}
    service = RubricCalibrationService(db); service.initialize()
    selector = dict(modality='original_audio', assessment_id=claim['assessment_id'], media_id=claim['media_id'],
                    candidate_id=done['candidate_id'], job_id=claim['job_id'])
    with db.connect() as c:
        subject = c.execute('SELECT k.subject FROM diagnostic_item i JOIN knowledge_node k USING(knowledge_node_id) WHERE i.diagnostic_item_id=?', (v['item_id'],)).fetchone()[0]
        grade = c.execute('SELECT grade FROM child_profile WHERE child_id=?', (CHILD,)).fetchone()[0]
    return dict(v=v, db=db, service=service, selector=selector, binding=binding, store=store,
                scope=dict(subject=subject, grade=grade, task_family='read_aloud', strata=['public_synthetic']))


def freeze(f, key='audio-freeze'):
    return f['service'].freeze(CHILD, scorer=f['selector'], scope=f['scope'],
        holdout_receipt_id='unverified-public-holdout', client_action_id=key)


def source(c, f, child=CHILD, **replace):
    from home_study_agent.rubric_calibration_evidence import audio_candidate_source
    selector = {k: v for k, v in f['selector'].items() if k != 'modality'}
    return audio_candidate_source(c, child, **{**selector, **replace})


def test_original_audio_pending_protocol_real_candidate_and_no_text_or_score_export(original_cpu_candidate, monkeypatch):
    f = original_cpu_candidate
    # RED on the unchanged service: exact audio selector is not a text SCORER_FIELDS object.
    from home_study_agent import rubric_calibration_evidence as evidence
    monkeypatch.setattr(evidence, 'assessment', lambda *a, **k: pytest.fail('AUDIO_MUST_NOT_BORROW_FEEDBACK_MODEL_CALL'))
    from home_study_agent import rubric_calibration as calibration
    monkeypatch.setattr(calibration, 'assessment', lambda *a, **k: pytest.fail('AUDIO_MUST_NOT_BORROW_IMPORTED_FEEDBACK_MODEL_CALL'))
    saved = freeze(f); view = saved['current']
    assert view['status'] == 'pending_unqualified' and view['score_qualified'] is False
    assert set(view['blockers']) >= {'original_audio_projection_missing', 'held_execution_attestation_missing', 'deployment_metadata_unreviewed'}
    original = f['v']['original']['stimulus_text']
    encoded = json.dumps(saved, ensure_ascii=False)
    assert original not in encoded and 'Ada visited a garden' not in encoded and str(f['db'].path.parent) not in encoded
    assert 'raw_numeric' not in view['source'] and 'model_call_id' not in encoded
    assert view['source']['historical_request_reconstructed'] is False and view['source']['held_execution_verified'] is False
    with f['db'].connect() as c:
        c.execute('BEGIN'); before = c.total_changes
        packet = source(c, f)
        assert packet == view['source'] and c.total_changes == before and c.in_transaction
        assert packet['reference_sha256'] == hashlib.sha256(original.encode()).hexdigest()
        assert packet['frame_range'] == [0, f['binding']['audio']['frames']]
        assert c.execute("SELECT count(*) FROM rubric_calibration_event WHERE operation='qualify'").fetchone()[0] == 0
    reopened = Database(f['db'].path, media_root=f['v']['settings'].data_dir, data_encryption_key=f['v']['settings'].data_encryption_key)
    assert RubricCalibrationService(reopened).read(CHILD, view['protocol_id']) == view
    assert freeze(f) == saved


@pytest.mark.parametrize('fault', ['cross_child', 'wrong_job', 'job_state', 'version', 'reference', 'raw_shape'])
def test_exact_original_audio_source_failures_are_closed_without_mutating_answers(original_cpu_candidate, fault):
    f = original_cpu_candidate
    with f['db'].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        before = [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')]
        if fault == 'job_state':
            with pytest.raises(sqlite3.IntegrityError, match='PET_SPEAKING_JOB_IDENTITY_OR_STATE'):
                c.execute("UPDATE pet_speaking_candidate_job SET state='blocked' WHERE job_id=?", (f['selector']['job_id'],))
            c.execute('DROP TRIGGER pet_speaking_job_identity')  # synthetic corrupt-history fault only
            c.execute("UPDATE pet_speaking_candidate_job SET state='blocked' WHERE job_id=?", (f['selector']['job_id'],))
        elif fault == 'version':
            c.execute("UPDATE diagnostic_prompt SET item_version='changed' WHERE prompt_id=?", (f['binding']['prompt_id'],))
        elif fault == 'reference':
            c.execute("UPDATE diagnostic_item SET stimulus_text='Different unreviewed original' WHERE diagnostic_item_id=?", (f['v']['item_id'],))
        elif fault == 'raw_shape':
            c.execute('DROP TRIGGER pet_speaking_model_candidate_no_update')
            c.execute("UPDATE pet_speaking_model_candidate SET raw_numeric_json=json_set(raw_numeric_json,'$.score',3) WHERE candidate_id=?", (f['selector']['candidate_id'],))
        changes = c.total_changes
        with pytest.raises(ValueError, match='RUBRIC_CALIBRATION_AUDIO_SOURCE_NOT_CURRENT'):
            source(c, f, OTHER if fault == 'cross_child' else CHILD, **({'job_id': 'unknown-job'} if fault == 'wrong_job' else {}))
        assert c.total_changes == changes and [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')] == before
        c.rollback()


def test_frozen_raw_tamper_and_withdrawal_never_revive_pending_protocol(original_cpu_candidate):
    f = original_cpu_candidate; saved = freeze(f); pid = saved['current']['protocol_id']
    with f['db'].connect() as c:
        c.execute('BEGIN IMMEDIATE'); c.execute('DROP TRIGGER pet_speaking_model_candidate_no_update')
        row = c.execute('SELECT raw_numeric_json FROM pet_speaking_model_candidate WHERE candidate_id=?', (f['selector']['candidate_id'],)).fetchone()
        numeric = json.loads(row[0]); numeric['sentence']['uttr_acc'] += .125
        from home_study_agent.pet_speaking_candidate_store import canonical
        c.execute('UPDATE pet_speaking_model_candidate SET raw_numeric_json=? WHERE candidate_id=?', (canonical(numeric), f['selector']['candidate_id']))
        # Valid numeric shape still cannot match the frozen original raw digest.
        view = f['service']._read(c, f['service']._owned(c, CHILD, pid), datetime.now(timezone.utc))
        assert view['status'] == 'invalidated' and not view['score_qualified']; c.rollback()
    f['store'].withdraw(CHILD, f['selector']['candidate_id'], expected_revision=1, client_action_id='withdraw-original-cpu')
    view = f['service'].read(CHILD, pid)
    assert view['status'] == 'invalidated' and 'original_audio_source_changed_or_unavailable' in view['blockers']
    reopened = Database(f['db'].path, media_root=f['v']['settings'].data_dir, data_encryption_key=f['v']['settings'].data_encryption_key)
    assert RubricCalibrationService(reopened).read(CHILD, pid) == view
    replay = freeze(f)
    assert replay['receipt'] == saved['receipt'] and replay['current'] == view
    with f['db'].connect() as c:
        assert c.execute('SELECT count(*) FROM rubric_calibration_event WHERE protocol_id=?', (pid,)).fetchone()[0] == 1


def test_original_audio_can_never_issue_certificate_via_fake_authority_or_json(original_cpu_candidate):
    f = original_cpu_candidate
    for extra in ['projection', 'installed', 'owner_epoch', 'scores', 'media_root']:
        with pytest.raises(ValueError, match='AUDIO_SELECTOR_REQUIRED'):
            f['service'].freeze(CHILD, scorer={**f['selector'], extra: 'caller-cannot-attest'}, scope=f['scope'], holdout_receipt_id='fake', client_action_id='fake-'+extra)
    saved = freeze(f); pid = saved['current']['protocol_id']
    f['service'].authority_resolver = lambda *a, **k: pytest.fail('NO_AUDIO_AUTHORITY_FALLBACK_TO_FEEDBACK_SCORER')
    evaluated = f['service'].evaluate(CHILD, pid, expected_revision=1, client_action_id='audio-evaluate')
    assert not evaluated['current']['score_qualified'] and evaluated['receipt']['operation'] == 'evaluate'
    with pytest.raises(ValueError, match='AUDIO_PROJECTION_UNAVAILABLE'):
        f['service'].record_case(CHILD, pid, assessment_id=f['selector']['assessment_id'], blind_receipt_ids=['one','two'], expected_revision=2, client_action_id='fake-case')
    with f['db'].connect() as c:
        c.execute('BEGIN')
        with pytest.raises(ValueError, match='PROTOCOL_NOT_QUALIFIED'):
            f['service'].prepare_score_binding(c, CHILD, protocol_id=pid, evidence_id=f['binding']['evidence_id'], as_of=datetime.now(timezone.utc))
        receipt = f['service'].qualified_score_receipt(c, CHILD, f['selector']['assessment_id'], protocol_id=pid, as_of=datetime.now(timezone.utc))
        assert receipt['score_qualified'] is False and receipt['status'] == 'reference'
        assert c.execute("SELECT count(*) FROM rubric_calibration_event WHERE operation='qualify'").fetchone()[0] == 0


def test_real_parent_loopback_freeze_read_and_source_withdrawal(original_cpu_candidate):
    f = original_cpu_candidate; v = f['v']; settings = v['settings']
    from home_study_agent.app import create_app
    path = f'/v1/parent/children/{CHILD}/rubric-calibration'
    with TestClient(create_app(settings, model_gateway=v['gateway']), headers={'X-HSC-Session': settings.session_token}, client=('127.0.0.1', 41912)) as client:
        body = dict(scorer=f['selector'], scope=f['scope'], holdout_receipt_id='unverified-http-holdout', client_action_id='audio-http-freeze')
        assert client.post(path+'/protocols/freeze', json=body).status_code == 401
        client.headers['X-HSC-Parent'] = post(client, '/v1/parent/unlock', {'pin':'7391'})['parent_token']
        result = client.post(path+'/protocols/freeze', json=body)
        assert result.status_code == 200, result.text
        view = result.json()['current']; pid = view['protocol_id']
        assert view['status'] == 'pending_unqualified' and not view['score_qualified']
        assert client.get(path+'/'+pid).json() == view
        post(client, BASE+'/'+v['draft']['candidate_id']+'/withdraw', dict(action_id='withdraw-audio-reference', expected_version=4, note='Public synthetic source withdrawal'))
        changed = client.get(path+'/'+pid).json()
        assert changed['status'] == 'invalidated' and not changed['score_qualified']
        assert client.get(f'/v1/parent/children/{OTHER}/rubric-calibration/'+pid).status_code == 404
    assert not v['gateway'].requests


def test_missing_job_schema_is_closed_not_a_private_sql_exception(original_cpu_candidate):
    f = original_cpu_candidate
    with f['db'].connect() as c:
        c.execute('BEGIN IMMEDIATE'); c.execute('DROP TABLE pet_speaking_candidate_job')
        with pytest.raises(ValueError, match='RUBRIC_CALIBRATION_AUDIO_SOURCE_NOT_CURRENT'):
            source(c, f)
        c.rollback()


def test_original_host_scope_transaction_and_physical_audio_are_required(original_cpu_candidate):
    from pathlib import Path
    f = original_cpu_candidate
    with f['db'].connect() as c:
        with pytest.raises(ValueError, match='AUDIO_READ_TRANSACTION_REQUIRED'):
            source(c, f)
    plain = sqlite3.connect(f['db'].path); plain.row_factory = sqlite3.Row
    try:
        plain.execute('BEGIN')
        with pytest.raises(ValueError, match='AUDIO_LOCAL_MEDIA_SCOPE_REQUIRED'):
            source(plain, f)
    finally:
        plain.rollback(); plain.close()
    saved = freeze(f); pid = saved['current']['protocol_id']
    with f['db'].connect() as c:
        from home_study_agent.pet_media_evidence import _source
        media = c.execute('SELECT * FROM pet_media_binding WHERE media_id=?', (f['selector']['media_id'],)).fetchone()
        original = _source(c, media)
        path = Path(original['local_path'])
        answer_before = [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')]
    prior = path.read_bytes()
    try:
        path.write_bytes(b'public synthetic corrupted encrypted original')
        view = f['service'].read(CHILD, pid)
        assert view['status'] == 'invalidated' and not view['score_qualified']
        with f['db'].connect() as c:
            assert [tuple(r) for r in c.execute('SELECT * FROM diagnostic_attempt')] == answer_before
    finally:
        path.write_bytes(prior)
