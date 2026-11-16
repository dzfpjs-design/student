import pytest

from home_study_agent.paper_printed_calibration import summarize


def cases(count=80, *, abstain=0, wrong=0):
    result = []
    for i in range(count * 2):
        gold = '0' if i < count else '2.5'
        predicted = None if i < abstain else ('2.5' if i < wrong else gold)
        result.append(dict(group_hash=f'{i:064x}', stratum='original',
                           gold={'point': gold}, predicted={'point': predicted}))
    return result


def test_original_decimal_scale_and_independent_group_bounds():
    metrics, blockers = summarize(cases(), {'point': '2.5'}, ['original'])
    assert blockers == []
    point = metrics['original:point']
    assert point['anchors'] == {'0': 80, '2.5': 80}
    assert point['maximum_score'] == '2.5'
    assert point['false_pass_upper'] < .05
    # Fifty samples alone do not prove a 5% error bound.
    assert 'error_bound_exceeded' in summarize(cases(50), {'point': '2.5'}, ['original'])[1]


def test_abstentions_are_in_coverage_denominator_and_overestimates_block():
    assert 'coverage_insufficient' in summarize(cases(abstain=9), {'point': '2.5'}, ['original'])[1]
    assert 'severe_overestimate' in summarize(cases(wrong=1), {'point': '2.5'}, ['original'])[1]


@pytest.mark.parametrize('mutation', ['duplicate', 'unknown_stratum', 'half_score', 'gold_abstain', 'boolean', 'missing_point'])
def test_invalid_calibration_cases_cannot_inflate_qualification(mutation):
    rows = cases()
    if mutation == 'duplicate': rows[1]['group_hash'] = rows[0]['group_hash']
    if mutation == 'unknown_stratum': rows[0]['stratum'] = 'other'
    if mutation == 'half_score': rows[0]['predicted']['point'] = '1.25'
    if mutation == 'gold_abstain': rows[0]['gold']['point'] = None
    if mutation == 'boolean': rows[0]['gold']['point'] = False
    if mutation == 'missing_point': rows[0]['gold'] = {}
    with pytest.raises(ValueError, match='RUBRIC_CALIBRATION_PRINTED_'):
        summarize(rows, {'point': '2.5'}, ['original'])


def prepared_calibration(tmp_path, monkeypatch, *, reported_model='fixture-model'):
    import asyncio
    import base64
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone
    import json
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from home_study_agent.paper_printed_calibration import (
        VERSION, PaperPrintedCalibrationService, candidate_snapshot,
    )
    from home_study_agent.paper_subjective_rubric import PaperSubjectiveRubricService
    from home_study_agent.paper_printed_score_candidate import PARSER_VERSION
    from home_study_agent.rubric_calibration_authority import import_receipt, resolve
    from home_study_agent.rubric_calibration_evidence import POLICY, digest, encoded
    from test_paper_printed_score_execution import Gateway, setup

    provenance = dict(version='model_scorer_provenance_v1', provider='fixture', requested_model='fixture-model',
                      endpoint_scope_sha256='a' * 64, system_prompt_sha256='b' * 64,
                      parameters_sha256='c' * 64, reported_model=reported_model, immutable_revision_verified=False)
    class ActualFixtureGateway(Gateway):
        async def complete(self, request):
            return replace(await super().complete(request), scorer_provenance=provenance)

    db, request, aid, gateway, executor = setup(tmp_path, ActualFixtureGateway())
    ctx = PaperSubjectiveRubricService(db).execution_context('child', aid)
    scorer = dict(provider='fixture', model='fixture-model', model_revision='fixture-model',
                  revision_receipt_id='immutable-fixture', prompt_hash='b' * 64, parameters_hash='c' * 64,
                  endpoint_scope_sha256='a' * 64, parser_version=PARSER_VERSION, criteria_hash=digest(ctx['criteria']))
    with db.connect() as c:
        spec = PaperSubjectiveRubricService(None).current('child', aid, connection=c)
        draft = json.loads(c.execute("SELECT evidence_json FROM paper_scoring_supply_event WHERE status='rubric_candidate'").fetchone()[0])
    scope = dict(subject=draft['subject'], grade=ctx['original_response']['grade'], task_family='printed_quantity', strata=['original'])
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    trust = dict(version='rubric_authority_trust_v1', issuers=[dict(key_id='test-only',
        public_key_pem=key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        kinds=['holdout', 'scorer_revision', 'printed_blind_label'], child_ids=['child'], expires_at=(now + timedelta(days=2)).isoformat())])
    trust_path = tmp_path / 'isolated-trust.json'
    trust_path.write_text(json.dumps(trust))
    monkeypatch.setenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE', str(trust_path))

    def publish(kind, payload):
        envelope = dict(version='rubric_authority_envelope_v1', key_id='test-only', kind=kind, child_id='child',
                        receipt_id=payload['receipt_id'], issued_at=datetime.now(timezone.utc).isoformat(),
                        expires_at=(now + timedelta(days=1)).isoformat(), payload=payload)
        signature = base64.b64encode(key.sign(encoded(envelope).encode())).decode()
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            result = import_receipt(c, child_id='child', envelope=envelope, signature=signature,
                                    client_action_id='publish-' + payload['receipt_id'])
            c.commit()
        return result

    holdout = dict(receipt_id='holdout', child_id='child', status='current', corpus_kind='observed_original',
                   blind_protocol_version='blind_labels_v1', provenance_receipt_hash='d' * 64,
                   scope_hash=digest(scope), frozen_at=now.isoformat(), development_group_hashes=[],
                   cases=[dict(evidence_id=aid, group_hash='e' * 64, stratum='original',
                               source_ids=[aid, spec['source_candidate_id'], ctx['original_response']['artifact_id']])])
    publish('holdout', holdout)
    publish('scorer_revision', dict(receipt_id='immutable-fixture', child_id='child', status='current',
        provider='fixture', model='fixture-model', model_revision='fixture-model', verification_method='signed_release_manifest',
        evidence_hash='f' * 64, endpoint_scope_sha256='a' * 64,
        verified_at=(now - timedelta(seconds=1)).isoformat(), expires_at=(now + timedelta(days=1)).isoformat()))
    execution = asyncio.run(executor.process_next())
    candidate_id = execution['candidate_artifact_id']
    with db.connect() as c:
        c.execute('BEGIN')
        candidate, context, fingerprint = candidate_snapshot(c, 'child', candidate_id)
        proof = resolve(c, child_id='child', kind='holdout', reference_id='holdout')
    phash = digest([VERSION, 'child', scorer, scope, proof, digest(POLICY)])
    def label(rid, reviewer='one', **changes):
        value = dict(receipt_id=rid, child_id='child', status='current', identity_authority='independent_reviewer_registry_v1',
            identity_receipt_hash='1' * 64, display_receipt_hash='2' * 64, submission_receipt_hash='3' * 64,
            mask_version='blind_no_predictions_or_peer_labels_v1', protocol_hash=phash,
            candidate_sha256=fingerprint, evidence_id=aid, source_version=context['spec_sha256'],
            reviewer_principal_id='principal-' + reviewer, reviewer_id='reviewer-' + reviewer,
            displayed_at=datetime.now(timezone.utc).isoformat(), submitted_at=datetime.now(timezone.utc).isoformat(),
            scores={p['point_id']: p['score'] for p in candidate['points']})
        value.update(changes)
        return value
    publish('printed_blind_label', label('first', 'one'))
    publish('printed_blind_label', label('second', 'two'))
    inputs = dict(scorer=scorer, scope=scope, holdout_receipt_id='holdout',
                  cases=[dict(candidate_id=candidate_id, blind_receipt_ids=['first', 'second'])])
    return db, PaperPrintedCalibrationService(db), inputs, publish, label


def test_real_signed_original_calibration_persists_without_grading(tmp_path, monkeypatch):
    from home_study_agent.database import Database
    from home_study_agent.paper_printed_calibration import PaperPrintedCalibrationService
    from home_study_agent.paper_scoring import PaperScoringService
    db, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    result = service.evaluate('child', **inputs)
    assert result['calibration_qualified'] is False and result['score_authority_granted'] is False
    assert 'boundary_samples_insufficient' in result['blockers']
    assert 'model_revision_unverified' not in result['blockers']
    assert {m['maximum_score'] for m in result['metrics'].values()} == {'2.5', '1'}
    assert service.evaluate('child', **inputs) == result
    restarted = PaperPrintedCalibrationService(Database(db.path, runtime_mode=db.runtime_mode))
    assert restarted.current('child', result['artifact_id']) == dict(status='current', **result)
    with db.connect() as c:
        request = c.execute('SELECT * FROM paper_scoring_request').fetchone()
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 0
    assert PaperScoringService(db).resolve(request['request_id'], 'child', 'not-qualified') is None
    projected = PaperScoringService(db).for_candidate(request['candidate_id'], 'child')['authority_supply']['printed_calibration']
    assert projected['artifact_id'] == result['artifact_id'] and projected['status'] == 'current'
    assert not projected['score_authority_granted'] and 'bindings' not in projected and 'metrics' not in projected


@pytest.mark.parametrize('mutation', ['same_reviewer', 'different_score', 'source', 'prediction', 'holdout_timing', 'scope', 'missing_case'])
def test_independent_calibration_rejects_binding_or_authority_failures(tmp_path, monkeypatch, mutation):
    from datetime import datetime, timedelta, timezone
    db, service, inputs, publish, label = prepared_calibration(tmp_path, monkeypatch)
    if mutation in {'same_reviewer', 'different_score', 'source'}:
        changes = {}
        if mutation == 'different_score': changes['scores'] = {k: '0' for k in label('x')['scores']}
        if mutation == 'source': changes['source_version'] = '0' * 64
        publish('printed_blind_label', label('third', 'one' if mutation == 'same_reviewer' else 'three', **changes))
        inputs['cases'][0]['blind_receipt_ids'][1] = 'third'
    elif mutation == 'prediction':
        with db.connect() as c:
            c.execute("UPDATE model_call SET context_manifest_json='{}' WHERE purpose='paper_printed_score_candidate'")
            c.commit()
    elif mutation == 'holdout_timing':
        with db.connect() as c:
            c.execute("UPDATE model_call SET created_at=? WHERE purpose='paper_printed_score_candidate'",
                      ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),))
            c.commit()
    elif mutation == 'scope': inputs['scope']['grade'] = 'other-grade'
    else: inputs['cases'] = []
    with pytest.raises(ValueError):
        service.evaluate('child', **inputs)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='paper_printed_calibration'").fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['page', 'label_revoke', 'revision_revoke', 'trust_removed'])
def test_live_projection_withdraws_old_report_without_rewriting_it(tmp_path, monkeypatch, mutation):
    from home_study_agent.rubric_calibration_authority import resolve, revoke
    from home_study_agent.rubric_calibration_evidence import digest
    db, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    result = service.evaluate('child', **inputs)
    with db.connect() as c:
        original = c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (result['artifact_id'],)).fetchone()[0]
        c.execute('BEGIN IMMEDIATE')
        if mutation == 'page': c.execute("UPDATE capture_artifact SET extracted_text='changed original' WHERE artifact_id='page'")
        elif mutation in {'label_revoke', 'revision_revoke'}:
            kind, rid = ('printed_blind_label', 'first') if mutation == 'label_revoke' else ('scorer_revision', 'immutable-fixture')
            proof = resolve(c, child_id='child', kind=kind, reference_id=rid)
            revoke(c, child_id='child', kind=kind, receipt_id=rid, expected_hash=proof['signed_authority']['envelope_hash'], client_action_id='revoke-proof')
        c.commit()
    if mutation == 'trust_removed': monkeypatch.delenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE')
    current = service.current('child', result['artifact_id'])
    assert current['status'] == 'withdrawn' and not current['calibration_qualified']
    with db.connect() as c:
        assert c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (result['artifact_id'],)).fetchone()[0] == original


def test_local_app_http_requires_parent_auth_and_retains_reports_after_restart(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    db, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    settings = Settings(data_dir=tmp_path, database_path=db.path, runtime_mode=db.runtime_mode, maintenance_enabled=False)
    root = '/v1/parent/children/child/rubric-calibration/printed'
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 40123)) as client:
        assert client.post(root + '/evaluate', json=inputs).status_code == 401
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/setup', json={'pin': '7352'}).json()['parent_token']
        response = client.post(root + '/evaluate', json=inputs)
        assert response.status_code == 200, response.text
        result = response.json()
        assert response.headers['cache-control'] == 'no-store'
        assert not result['calibration_qualified'] and not result['score_authority_granted']
        assert client.get(root + '/' + result['artifact_id']).json()['status'] == 'current'
        bad = {**inputs, 'scope': {**inputs['scope'], 'strata': None}}
        assert client.post(root + '/evaluate', json=bad).status_code == 409
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('127.0.0.1', 40123)) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock', json={'pin': '7352'}).json()['parent_token']
        assert client.get(root + '/' + result['artifact_id']).json()['status'] == 'current'
        assert client.get(root + '/' + result['artifact_id'] + '?debug=1').status_code == 422
    with TestClient(create_app(settings, model_gateway=NoCloud()), client=('192.0.2.1', 40123)) as client:
        client.headers['X-HSC-Parent'] = client.post('/v1/parent/unlock', json={'pin': '7352'}).json()['parent_token']
        assert client.get(root + '/' + result['artifact_id']).status_code == 403


def test_report_is_in_privacy_export_and_delete_does_not_revive_on_restart(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from home_study_agent.database import Database
    from home_study_agent.paper_printed_calibration import PaperPrintedCalibrationService
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    db, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    result = service.evaluate('child', **inputs)
    privacy = PrivacyLifecycleService(db, tmp_path, b'0' * 32)
    privacy.initialize()
    exported = json.loads(Path(privacy.export_child('child')['path']).read_text())
    assert any(row['artifact_id'] == result['artifact_id'] for row in exported['capture_artifacts'])
    privacy.delete_capture_artifact(result['artifact_id'], '确认删除', 'delete-calibration-report')
    with pytest.raises(ValueError, match='REPORT_UNKNOWN'):
        PaperPrintedCalibrationService(Database(db.path, runtime_mode=db.runtime_mode)).current('child', result['artifact_id'])


def test_reported_alias_is_not_an_immutable_version_proof(tmp_path, monkeypatch):
    _, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch, reported_model='moving-alias')
    result = service.evaluate('child', **inputs)
    assert 'model_revision_unverified' in result['blockers']
    assert not result['calibration_qualified'] and not result['score_authority_granted']


def test_same_report_concurrent_replay_creates_only_one_durable_artifact(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    db, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.evaluate('child', **inputs), range(2)))
    assert results[0] == results[1]
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='paper_printed_calibration'").fetchone()[0] == 1


def test_trust_change_before_commit_rolls_back_instead_of_caching_qualification(tmp_path, monkeypatch):
    db, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    checked = service._checked
    def revoke_during_read(*args, **kwargs):
        result = checked(*args, **kwargs)
        monkeypatch.delenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE')
        return result
    monkeypatch.setattr(service, '_checked', revoke_during_read)
    with pytest.raises(ValueError, match='TRUST_CONFIGURATION_CHANGED'):
        service.evaluate('child', **inputs)
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM capture_artifact WHERE artifact_type='paper_printed_calibration'").fetchone()[0] == 0


def test_report_corruption_cannot_be_hidden_by_idempotent_replay(tmp_path, monkeypatch):
    import json
    db, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    result = service.evaluate('child', **inputs)
    with db.connect() as c:
        row = json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (result['artifact_id'],)).fetchone()[0])
        row['evaluation']['calibration_qualified'] = True
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(row), result['artifact_id']))
        c.commit()
    with pytest.raises(ValueError, match='REPORT_CORRUPT'):
        service.evaluate('child', **inputs)
    with pytest.raises(ValueError, match='REPORT_CORRUPT'):
        service.current('child', result['artifact_id'])
    with pytest.raises(ValueError, match='REPORT_UNKNOWN'):
        service.current('another-child', result['artifact_id'])


def test_retrospective_holdout_cannot_hide_an_earlier_prediction(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    db, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    with db.connect() as c:
        row = dict(c.execute("SELECT * FROM model_call WHERE purpose='paper_printed_score_candidate'").fetchone())
        row['call_id'] = 'earlier-prediction'
        row['created_at'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        columns = ','.join(row)
        marks = ','.join('?' for _ in row)
        c.execute(f'INSERT INTO model_call({columns}) VALUES({marks})', tuple(row.values()))
        c.commit()
    with pytest.raises(ValueError, match='HOLDOUT_ALREADY_SCORED'):
        service.evaluate('child', **inputs)


def test_changed_policy_requires_new_independent_blind_binding(tmp_path, monkeypatch):
    from home_study_agent.rubric_calibration_evidence import POLICY
    _, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    monkeypatch.setitem(POLICY, 'coverage_min', .94)
    with pytest.raises(ValueError, match='BLIND_SOURCE_CHANGED'):
        service.evaluate('child', **inputs)


def test_borrowed_reads_use_one_snapshot_and_never_commit_the_callers_changes(tmp_path, monkeypatch):
    db, service, inputs, _, _ = prepared_calibration(tmp_path, monkeypatch)
    result = service.evaluate('child', **inputs)
    with db.connect() as c:
        assert service.current('child', result['artifact_id'], connection=c)['status'] == 'current'
        assert not c.in_transaction
        assert service.latest_for_candidate('child', inputs['cases'][0]['candidate_id'], connection=c)['status'] == 'current'
        assert not c.in_transaction
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE capture_artifact SET extracted_text='uncommitted original' WHERE artifact_id='page'")
        assert service.current('child', result['artifact_id'], connection=c)['status'] == 'withdrawn'
        assert c.in_transaction
        c.rollback()
        assert service.current('child', result['artifact_id'], connection=c)['status'] == 'current'
