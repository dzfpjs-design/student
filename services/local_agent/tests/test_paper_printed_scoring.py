import pytest

from home_study_agent.paper_printed_scoring import PrintedPaperScoringService


def test_uncalibrated_candidates_never_become_decisions(tmp_path):
    import asyncio
    from test_paper_printed_score_execution import setup
    db, _, _, _, executor = setup(tmp_path)
    assert asyncio.run(executor.process_next())['status'] == 'completed'
    assert PrintedPaperScoringService(db).process_batch() == []
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 0


@pytest.fixture(scope='module')
def signed_corpus(tmp_path_factory):
    """Synthetic only: full unchanged error-bound policy, no fake qualification."""
    import asyncio
    import base64
    from datetime import datetime, timedelta, timezone
    import json
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from home_study_agent.model_gateway import ModelResult
    from home_study_agent.paper_printed_calibration import VERSION, PaperPrintedCalibrationService, candidate_snapshot
    from home_study_agent.paper_printed_score_candidate import PARSER_VERSION
    from home_study_agent.paper_printed_score_execution import PaperPrintedScoreExecutionService
    from home_study_agent.paper_scoring import PaperScoringService
    from home_study_agent.paper_scoring_source_match import PaperScoringSourceMatchService
    from home_study_agent.paper_scoring_supply import PaperScoringAuthorityDraftService
    from home_study_agent.paper_subjective_rubric import PaperSubjectiveRubricService
    from home_study_agent.privacy_lifecycle import PrivacyLifecycleService
    from home_study_agent.repository import StudyRepository, utc_now
    from home_study_agent.rubric_calibration_authority import import_receipt, resolve
    from home_study_agent.rubric_calibration_evidence import POLICY, digest, encoded
    from test_answer_attempt_ledger import setup_case
    from test_paper_scoring_source_match import add_local_page

    root = tmp_path_factory.mktemp('printed-original-corpus')
    db, monitor = setup_case(root)
    privacy = PrivacyLifecycleService(db, root)
    privacy.initialize()
    privacy.update_preferences({'cloud_model_text_allowed': True})
    repository, rubric = StudyRepository(db), PaperSubjectiveRubricService(db)
    with db.connect() as c:
        template = json.loads(c.execute("SELECT details_json FROM capture_artifact WHERE artifact_id='page'").fetchone()[0])
    source_ids, questions = [], []
    for i in range(163):
        question = f'{i + 1}. 计算 (24+{i + 6})×3，只回答一个整数，练习编号 exercise{i:04d}。'
        questions.append(question)
        text = question + '\n评分标准：\n1. 列出准确的数量关系（2.5分）\n2. 计算结果与单位正确（1分）'
        source_ids.append(add_local_page(db, text=text))
        page = json.loads(json.dumps(template))
        page['local_pipeline']['question_blocks'][0]['text'] = question
        page['local_pipeline']['wrong_question_candidates'][0].update(text=question, mark_kind='unverified_mark')
        page['cloud_vision_fallback']['blocks'] = [b for b in page['cloud_vision_fallback']['blocks'] if b['source'] != 'grading_mark']
        page['cloud_vision_fallback']['blocks'][0]['text'] = question
        answer = '0' if i < 80 else str((24 + i + 6) * 3)
        if i == 160: answer = 'partial'
        if i == 162: answer = 'abstain'
        page['cloud_vision_fallback']['blocks'][1]['text'] = answer
        with db.connect() as c:
            c.execute("INSERT INTO capture_artifact VALUES(?,?,?,'document_ocr',NULL,?,.4,?,'quality_warning',?,?)",
                      ('original-' + str(i), 'child', 'study', '本地文字不可依赖', json.dumps(page, ensure_ascii=False), utc_now(), 'page-' + str(i)))
            c.commit()
        observed = monitor.record_revision('study', 'original-' + str(i), 'signature-' + str(i), 'revision-' + str(i))
        assert observed['observed_answer_evidence_count'] == 1, (i, observed)

    class Draft:
        configured = True
        probe = ''
        async def complete(self, request):
            return ModelResult(json.dumps(dict(subject='数学', answer_mode='subjective', task_points=['计算依据'],
                source_search_terms=[self.probe], confidence=.99, requires_external_source=True)), 'fixture', 'fixture', 1, 8, 8)

    draft = Draft()
    draft_service = PaperScoringAuthorityDraftService(db, draft, repository, lambda: True)
    matcher = PaperScoringSourceMatchService(db)
    requests, specs = [], []
    for i, question in enumerate(questions):
        with db.connect() as c:
            candidate = c.execute('SELECT * FROM mistake_capture_candidate WHERE artifact_id=?', ('original-' + str(i),)).fetchone()
            visual = c.execute('SELECT * FROM observed_answer_evidence WHERE artifact_id=?', ('original-' + str(i),)).fetchone()
        request = PaperScoringService(db).request(candidate_id=candidate['candidate_id'], child_id='child', session_id='study',
            question_text=question, answer_text=visual['answer_text'], answer_source='visual_unverified',
            visual_answer_evidence_id=visual['visual_answer_evidence_id'], fact_source='qualified_visual_observation',
            fact_confidence=.94, client_action_id='request-' + str(i))
        draft.probe = f'exercise{i:04d}'
        drafted = asyncio.run(draft_service.process_next())
        assert drafted['status'] == 'completed'
        assert matcher.process_next()['status'] == 'candidate'
        with db.connect() as c:
            obligation = c.execute('SELECT obligation_id FROM paper_scoring_supply_obligation WHERE request_id=?', (request['request_id'],)).fetchone()[0]
        sources = matcher.valid_candidates(obligation)
        assert any(v['resource_id'] == source_ids[i][0] for v in sources), i
        source = next(v for v in sources if v['resource_id'] == source_ids[i][0])
        specs.append(rubric.freeze('child', request['request_id'], source))
        requests.append(request)

    now = datetime.now(timezone.utc)
    key = Ed25519PrivateKey.generate()
    trust_path = root / 'isolated-trust.json'
    trust_path.write_text(json.dumps(dict(version='rubric_authority_trust_v1', issuers=[dict(key_id='synthetic-only',
        public_key_pem=key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        kinds=['holdout', 'scorer_revision', 'printed_blind_label'], child_ids=['child'], expires_at=(now + timedelta(days=2)).isoformat())])))

    def publish(database, kind, payload):
        envelope = dict(version='rubric_authority_envelope_v1', key_id='synthetic-only', kind=kind, child_id='child',
            receipt_id=payload['receipt_id'], issued_at=datetime.now(timezone.utc).isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat(), payload=payload)
        signature = base64.b64encode(key.sign(encoded(envelope).encode())).decode()
        with database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            result = import_receipt(c, child_id='child', envelope=envelope, signature=signature, client_action_id='publish-' + payload['receipt_id'])
            c.commit()
        return result

    scorer = dict(provider='fixture', model='fixture-model', model_revision='fixture-model', revision_receipt_id='revision',
        prompt_hash='b' * 64, parameters_hash='c' * 64, endpoint_scope_sha256='a' * 64,
        parser_version=PARSER_VERSION, criteria_hash=digest(specs[0]['criteria']))
    scope = dict(subject='数学', grade='初三', task_family='printed_quantity', strata=['original'])
    holdout = dict(receipt_id='holdout', child_id='child', status='current', corpus_kind='observed_original',
        blind_protocol_version='blind_labels_v1', provenance_receipt_hash='d' * 64, scope_hash=digest(scope),
        frozen_at=now.isoformat(), development_group_hashes=[], cases=[dict(evidence_id=spec['artifact_id'],
            group_hash=digest(['independent-synthetic-group', i]), stratum='original',
            source_ids=[spec['artifact_id'], spec['source_candidate_id'], spec['original_response']['artifact_id']])
            for i, spec in enumerate(specs[:160])], printed_application_scope=[dict(
            question_sha256=spec['question_sha256'], source_text_sha256=spec['source_text_sha256'], criteria_hash=digest(spec['criteria']),
            group_hash=digest(['target-synthetic-group', i]), stratum='original') for i, spec in enumerate(specs[160:])])
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE', str(trust_path))
        publish(db, 'holdout', holdout)
        publish(db, 'scorer_revision', dict(receipt_id='revision', child_id='child', status='current', provider='fixture',
            model='fixture-model', model_revision='fixture-model', verification_method='signed_release_manifest',
            evidence_hash='f' * 64, endpoint_scope_sha256='a' * 64, verified_at=(now - timedelta(seconds=1)).isoformat(),
            expires_at=(now + timedelta(days=1)).isoformat()))
        provenance = dict(version='model_scorer_provenance_v1', provider='fixture', requested_model='fixture-model',
            reported_model='fixture-model', system_prompt_sha256='b' * 64, parameters_sha256='c' * 64,
            endpoint_scope_sha256='a' * 64, immutable_revision_verified=False)
        class Scorer:
            configured = True
            calls = 0
            async def complete(self, request):
                self.calls += 1
                request.pre_dispatch()
                request.pre_dispatch.authorize_physical({'provider': 'fixture', 'requested_model': 'fixture-model'})
                body = json.loads(request.messages[1]['content'])
                points = []
                for i, point in enumerate(body['criteria']):
                    abstained = body['student_answer'] == 'abstain'
                    met = body['student_answer'] != '0' and not (body['student_answer'] == 'partial' and i == 1)
                    points.append(dict(point_id=point['point_id'], status='abstain' if abstained else ('met' if met else 'not_met'),
                        score=None if abstained else (point['maximum_score'] if met else '0'),
                        response_quote=body['student_answer'], reason='合成原作答评分依据'))
                return ModelResult(json.dumps(dict(points=points), ensure_ascii=False), 'fixture', 'fixture-model', 1, 10, 10,
                                   scorer_provenance=provenance)
        gateway = Scorer()
        executor = PaperPrintedScoreExecutionService(db, gateway, repository, lambda: True)
        candidates = {}
        for _ in range(163):
            result = asyncio.run(executor.process_next())
            assert result['status'] == 'completed', result
            with db.connect() as c:
                candidate, ctx, fingerprint = candidate_snapshot(c, 'child', result['candidate_artifact_id'])
            candidates[ctx['artifact_id']] = (candidate, ctx, fingerprint)
        assert gateway.calls == 163
        with db.connect() as c:
            signed_holdout = resolve(c, child_id='child', kind='holdout', reference_id='holdout')
        protocol = digest([VERSION, 'child', scorer, scope, signed_holdout, digest(POLICY)])
        refs = []
        for i, spec in enumerate(specs[:160]):
            candidate, ctx, fingerprint = candidates[spec['artifact_id']]
            rids = []
            for reviewer in ('one', 'two'):
                rid = f'label-{i}-{reviewer}'
                publish(db, 'printed_blind_label', dict(receipt_id=rid, child_id='child', status='current',
                    identity_authority='independent_reviewer_registry_v1', identity_receipt_hash='1' * 64,
                    display_receipt_hash='2' * 64, submission_receipt_hash='3' * 64,
                    mask_version='blind_no_predictions_or_peer_labels_v1', protocol_hash=protocol,
                    candidate_sha256=fingerprint, evidence_id=ctx['artifact_id'], source_version=ctx['spec_sha256'],
                    reviewer_principal_id='principal-' + reviewer, reviewer_id='reviewer-' + reviewer,
                    displayed_at=datetime.now(timezone.utc).isoformat(), submitted_at=datetime.now(timezone.utc).isoformat(),
                    scores={p['point_id']: ('0' if i < 80 else p['maximum_score']) for p in candidate['points']}))
                rids.append(rid)
            refs.append(dict(candidate_id=candidate['artifact_id'], blind_receipt_ids=rids))
        inputs = dict(scorer=scorer, scope=scope, holdout_receipt_id='holdout', cases=refs)
        report = PaperPrintedCalibrationService(db).evaluate('child', **inputs)
        assert report['calibration_qualified'] and not report['score_authority_granted'], report['blockers']
    return dict(db=db, trust_path=trust_path, report=report, inputs=inputs, requests=requests, specs=specs,
                candidates=candidates, publish=publish, holdout=holdout)


@pytest.fixture
def corpus(signed_corpus, tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing
    from home_study_agent.database import Database
    path = tmp_path / 'study.db'
    with signed_corpus['db'].connect() as source, closing(sqlite3.connect(path)) as destination:
        source.backup(destination)
        assert destination.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    monkeypatch.setenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE', str(signed_corpus['trust_path']))
    return {**signed_corpus, 'db': Database(path, runtime_mode='trial')}


def test_original_target_scores_use_existing_journal_and_survive_restart(corpus):
    from home_study_agent.database import Database
    from home_study_agent.paper_scoring import PaperScoringService
    db = corpus['db']
    results = PrintedPaperScoringService(db).process_batch()
    assert len(results) == 2
    for index, expected, total in ((160, 0, '2.5'), (161, 1, '3.5')):
        request = corpus['requests'][index]
        decision = next(row for row in results if row['request_id'] == request['request_id'])
        assert decision['correctness'] == expected and decision['status'] == 'qualified'
        assert decision['authority_snapshot']['total_score'] == total
        assert decision['authority_snapshot']['maximum_score'] == '3.5'
        assert 'assessment_id' not in decision['authority_snapshot']
        restart = PaperScoringService(Database(db.path, runtime_mode='trial'))
        view = restart.for_candidate(request['candidate_id'], 'child')
        assert view['resolved_correctness'] == expected
        assert view['authority_supply']['printed_calibration']['artifact_id'] == corpus['report']['artifact_id']
        assert view['authority_supply']['printed_calibration']['score_authority_granted'] is False
        retried = restart.resolve(request['request_id'], 'child', 'retry-target')
        assert retried['decision_id'] == decision['decision_id'] and retried['authority_snapshot'] == decision['authority_snapshot']
    assert PrintedPaperScoringService(db).process_batch() == []
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM paper_subjective_score_binding').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='rubric_assessment'").fetchone() is None
    abstained = corpus['requests'][162]
    assert PaperScoringService(db).for_candidate(abstained['candidate_id'], 'child')['decision_id'] is None
    for request in corpus['requests'][:160]:
        assert PaperScoringService(db).resolve(request['request_id'], 'child', 'holdout-must-not-grade') is None


def revoke_revision(corpus):
    from home_study_agent.rubric_calibration_authority import resolve, revoke
    with corpus['db'].connect() as c:
        c.execute('BEGIN IMMEDIATE')
        proof = resolve(c, child_id='child', kind='scorer_revision', reference_id='revision')
        revoke(c, child_id='child', kind='scorer_revision', receipt_id='revision',
               expected_hash=proof['signed_authority']['envelope_hash'], client_action_id='withdraw-revision')
        c.commit()


@pytest.mark.parametrize('mutation', ['page', 'blind_label', 'revision', 'trust', 'calibration_delete', 'execution_receipt'])
def test_live_dependency_loss_withdraws_but_preserves_score_journal(corpus, monkeypatch, mutation):
    import json
    from home_study_agent.paper_scoring import PaperScoringService
    from home_study_agent.rubric_calibration_authority import resolve, revoke
    db = corpus['db']
    results = PrintedPaperScoringService(db).process_batch()
    decision = next(row for row in results if row['request_id'] == corpus['requests'][160]['request_id'])
    with db.connect() as c:
        original = c.execute('SELECT authority_snapshot_json FROM paper_scoring_decision WHERE decision_id=?', (decision['decision_id'],)).fetchone()[0]
        c.execute('BEGIN IMMEDIATE')
        if mutation == 'page':
            c.execute("UPDATE capture_artifact SET extracted_text='changed original' WHERE artifact_id='original-160'")
        elif mutation in {'blind_label', 'revision'}:
            kind, rid = ('printed_blind_label', 'label-0-one') if mutation == 'blind_label' else ('scorer_revision', 'revision')
            proof = resolve(c, child_id='child', kind=kind, reference_id=rid)
            revoke(c, child_id='child', kind=kind, receipt_id=rid, expected_hash=proof['signed_authority']['envelope_hash'], client_action_id='revoke-proof')
        elif mutation == 'calibration_delete':
            c.execute('DELETE FROM capture_artifact WHERE artifact_id=?', (corpus['report']['artifact_id'],))
        elif mutation == 'execution_receipt':
            candidate = corpus['candidates'][corpus['specs'][160]['artifact_id']][0]
            manifest = json.loads(c.execute('SELECT context_manifest_json FROM model_call WHERE call_id=?', (candidate['model_call']['call_id'],)).fetchone()[0])
            c.execute("UPDATE capture_artifact SET details_json='{}' WHERE artifact_id=?", (manifest['execution_job_id'],))
        c.commit()
    if mutation == 'trust': monkeypatch.delenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE')
    assert PrintedPaperScoringService(db).process_batch() == []
    assert PaperScoringService(db).for_candidate(corpus['requests'][160]['candidate_id'], 'child')['decision_id'] is None
    with db.connect() as c:
        assert c.execute('SELECT authority_snapshot_json FROM paper_scoring_decision WHERE decision_id=?', (decision['decision_id'],)).fetchone()[0] == original
        assert c.execute('SELECT effective_status FROM paper_scoring_effective_decision WHERE decision_id=?', (decision['decision_id'],)).fetchone()[0] == 'withdrawn'
        assert c.execute('SELECT COUNT(*) FROM learning_evidence').fetchone()[0] == 0


def pending_dialogue(corpus, index):
    import json
    from home_study_agent.repository import utc_now
    request = corpus['requests'][index]
    with corpus['db'].connect() as c:
        node = c.execute("SELECT knowledge_node_id,title FROM knowledge_node WHERE subject='数学' AND status='active' ORDER BY rowid LIMIT 1").fetchone()
        state = dict(artifact_id='original-' + str(index), question_text=request['question_text'], submitted_answer=request['answer_text'],
            answer_source='visual_unverified', visual_answer_evidence_id=request['visual_answer_evidence_id'], text_confirmed=True,
            automatic_fact_source='qualified_visual_observation', subject='数学', knowledge_node_id=node[0], knowledge_title=node[1],
            scoring_request_id=request['request_id'], scoring_request_status='pending_authority')
        c.execute("INSERT INTO mistake_dialogue VALUES('study','child',?,NULL,'scoring_pending',?,?)",
                  (request['candidate_id'], json.dumps(state, ensure_ascii=False), utc_now()))
        c.commit()


@pytest.mark.parametrize('index,expected', [(160, 'mistake_created'), (161, 'correct_verification_scheduled')])
def test_qualified_original_flow_reconciles_and_revocation_withdraws_derivatives(corpus, index, expected):
    from home_study_agent.mistake_dialogue import MistakeDialogueService
    from home_study_agent.answer_attempt_ledger import AnswerAttemptLedger
    pending_dialogue(corpus, index)
    PrintedPaperScoringService(corpus['db']).process_batch()
    result = MistakeDialogueService(corpus['db']).reconcile_pending_scoring()
    assert len(result) == 1 and result[0]['outcome'] == expected
    assert MistakeDialogueService(corpus['db']).reconcile_pending_scoring() == []
    with corpus['db'].connect() as c:
        # Scoring correctness alone must not assert independent mastery.
        from home_study_agent.learning_obligations import project_mastery_evidence
        from home_study_agent.repository import utc_now
        node_id = c.execute("SELECT json_extract(state_json,'$.knowledge_node_id') FROM mistake_dialogue WHERE session_id='study'").fetchone()[0]
        projection = project_mastery_evidence(c, 'child', node_id, as_of=utc_now())
        assert projection['effective_evidence'] == []
        assert c.execute('SELECT COUNT(*) FROM mastery_state').fetchone()[0] == 0
    revoke_revision(corpus)
    AnswerAttemptLedger(corpus['db']).list_for_child('child')
    with corpus['db'].connect() as c:
        if index == 160:
            assert c.execute('SELECT status FROM mistake_case').fetchone()[0] == 'excluded'
        else:
            assert c.execute('SELECT status FROM paper_correct_verification_event ORDER BY rowid DESC LIMIT 1').fetchone()[0] == 'withdrawn'


def test_target_recipe_change_cannot_borrow_holdout_calibration(corpus):
    import json
    from home_study_agent.rubric_calibration_evidence import digest
    candidate = corpus['candidates'][corpus['specs'][160]['artifact_id']][0]
    with corpus['db'].connect() as c:
        call_id = candidate['model_call']['call_id']
        manifest = json.loads(c.execute('SELECT context_manifest_json FROM model_call WHERE call_id=?', (call_id,)).fetchone()[0])
        job = c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (manifest['execution_job_id'],)).fetchone()
        details = json.loads(job[0])
        details['receipt']['scorer_provenance']['parameters_sha256'] = '9' * 64
        details['receipt_sha256'] = manifest['response_receipt_sha256'] = digest(details['receipt'])
        c.execute('UPDATE model_call SET context_manifest_json=? WHERE call_id=?', (json.dumps(manifest), call_id))
        c.execute('UPDATE capture_artifact SET details_json=? WHERE artifact_id=?', (json.dumps(details), manifest['execution_job_id']))
        c.commit()
    results = PrintedPaperScoringService(corpus['db']).process_batch()
    assert {row['request_id'] for row in results} == {corpus['requests'][161]['request_id']}


def test_newest_corrupt_matching_report_does_not_fall_back(corpus):
    import json
    from home_study_agent.repository import utc_now
    from home_study_agent.rubric_calibration_evidence import digest, encoded
    with corpus['db'].connect() as c:
        payload = json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (corpus['report']['artifact_id'],)).fetchone()[0])
        payload['evaluation']['calibration_qualified'] = False
        c.execute("INSERT INTO capture_artifact VALUES('newest-invalid','child','study',?,NULL,NULL,NULL,?,'completed',?,?)",
                  ('paper_printed_calibration', encoded(payload), utc_now(), 'paper-printed-calibration:' + digest(['child', payload])))
        c.commit()
    assert PrintedPaperScoringService(corpus['db']).process_batch() == []


def test_concurrent_background_passes_do_not_duplicate_decisions(corpus):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: PrintedPaperScoringService(corpus['db']).process_batch(), range(2)))
    with corpus['db'].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 2
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision_revision').fetchone()[0] == 0


def test_real_app_maintenance_consumes_without_child_button(corpus, tmp_path):
    import time
    from fastapi.testclient import TestClient
    from home_study_agent.app import create_app
    from home_study_agent.config import Settings
    from test_companion_activity_host import NoCloud
    settings = Settings(data_dir=tmp_path, database_path=corpus['db'].path, runtime_mode='trial',
                        maintenance_enabled=True, maintenance_interval_seconds=.05)
    with TestClient(create_app(settings, model_gateway=NoCloud())):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with corpus['db'].connect() as c:
                count = c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0]
            if count == 2: break
            time.sleep(.05)
        assert count == 2


def test_requalified_reports_append_revisions_without_reusing_action(corpus):
    from copy import deepcopy
    from home_study_agent.paper_printed_calibration import PaperPrintedCalibrationService
    from home_study_agent.rubric_calibration_authority import resolve
    db = corpus['db']
    first = PrintedPaperScoringService(db).process_batch()
    root_ids = {row['decision_id'] for row in first}
    inputs = deepcopy(corpus['inputs'])
    for sequence in (1, 2):
        with db.connect() as c:
            proof = resolve(c, child_id='child', kind='printed_blind_label', reference_id='label-0-two')
        label = {k: v for k, v in proof.items() if k != 'signed_authority'}
        label['receipt_id'] = 'replacement-' + str(sequence)
        corpus['publish'](db, 'printed_blind_label', label)
        inputs['cases'][0]['blind_receipt_ids'][1] = label['receipt_id']
        report = PaperPrintedCalibrationService(db).evaluate('child', **inputs)
        assert report['calibration_qualified']
        revised = PrintedPaperScoringService(db).process_batch()
        assert len(revised) == 2
        assert not root_ids.intersection({row['decision_id'] for row in revised})
        assert PrintedPaperScoringService(db).process_batch() == []
        with db.connect() as c:
            rows = c.execute('SELECT revision_sequence FROM paper_scoring_decision_revision').fetchall()
            assert len(rows) == sequence * 2 and max(row[0] for row in rows) == sequence
            assert c.execute("SELECT COUNT(*) FROM paper_scoring_effective_decision WHERE effective_status='qualified'").fetchone()[0] == 2


@pytest.mark.parametrize('mutation', ['bad_hash', 'holdout_group', 'duplicate_group', 'duplicate_identity', 'unknown_field', 'boolean_scope'])
def test_signed_scope_contract_rejects_ambiguous_or_leaking_rules(corpus, mutation):
    from copy import deepcopy
    payload = deepcopy(corpus['holdout'])
    payload['receipt_id'] = 'invalid-scope'
    rules = payload['printed_application_scope']
    if mutation == 'bad_hash': rules[0]['question_sha256'] = 'invalid'
    elif mutation == 'holdout_group': rules[0]['group_hash'] = payload['cases'][0]['group_hash']
    elif mutation == 'duplicate_group': rules[1]['group_hash'] = rules[0]['group_hash']
    elif mutation == 'duplicate_identity': rules.append(deepcopy(rules[0]))
    elif mutation == 'unknown_field': rules[0]['fake_score'] = '100'
    else: payload['printed_application_scope'] = True
    with pytest.raises(ValueError, match='RUBRIC_CALIBRATION_PRINTED_SCOPE'):
        corpus['publish'](corpus['db'], 'holdout', payload)


def test_unrelated_corrupt_artifacts_do_not_stop_eligible_targets(corpus):
    from home_study_agent.repository import utc_now
    with corpus['db'].connect() as c:
        for i, kind in enumerate(('paper_printed_calibration', 'paper_printed_score_candidate')):
            c.execute("INSERT INTO capture_artifact VALUES(?, 'child','study',?,NULL,NULL,NULL,'broken json','completed',?,?)",
                      ('corrupt-' + str(i), kind, utc_now(), 'corrupt-action-' + str(i)))
        c.commit()
    assert len(PrintedPaperScoringService(corpus['db']).process_batch()) == 2


def test_borrowed_transaction_source_loss_withdraws_without_committing_caller(corpus):
    from home_study_agent.paper_printed_scoring import authority_for_request
    db = corpus['db']
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        request = c.execute('SELECT * FROM paper_scoring_request WHERE request_id=?', (corpus['requests'][160]['request_id'],)).fetchone()
        assert authority_for_request(c, request) is not None
        c.execute("UPDATE capture_artifact SET extracted_text='not committed' WHERE artifact_id='original-160'")
        assert authority_for_request(c, request) is None
        assert c.in_transaction
        c.rollback()
        c.execute('BEGIN')
        assert authority_for_request(c, request) is not None


def test_trust_loss_during_target_consumption_never_grants_authority(corpus, monkeypatch):
    from home_study_agent import paper_printed_scoring as module
    from home_study_agent.paper_scoring import PaperScoringService
    original = module.check_provenance
    def changing(receipt, scorer):
        result = original(receipt, scorer)
        monkeypatch.delenv('HSC_RUBRIC_CALIBRATION_TRUST_FILE')
        return result
    monkeypatch.setattr(module, 'check_provenance', changing)
    request = corpus['requests'][160]
    assert PaperScoringService(corpus['db']).resolve(request['request_id'], 'child', 'trust-lost-at-acceptance') is None
    with corpus['db'].connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 0


def test_bounded_background_scan_survives_restart_and_abstention_does_not_starve(corpus):
    found = []
    for _ in range(3):
        found.extend(PrintedPaperScoringService(corpus['db']).process_batch(1))
    assert {row['request_id'] for row in found} == {request['request_id'] for request in corpus['requests'][160:162]}
    with corpus['db'].connect() as c:
        assert c.execute("SELECT COUNT(*) FROM audit_event WHERE action='paper_printed_scoring_scan_v1'").fetchone()[0] >= 1


def test_stale_worker_action_cannot_collide_across_requalified_generations(corpus):
    from copy import deepcopy
    from home_study_agent.paper_printed_calibration import PaperPrintedCalibrationService
    from home_study_agent.paper_scoring import PaperScoringService
    from home_study_agent.rubric_calibration_authority import resolve
    db, request = corpus['db'], corpus['requests'][160]
    scoring = PaperScoringService(db)
    first = scoring.resolve(request['request_id'], 'child', 'printed-auto-scoring:stale-worker')
    assert first['status'] == 'qualified'
    inputs = deepcopy(corpus['inputs'])
    for sequence in (1, 2):
        with db.connect() as c:
            proof = resolve(c, child_id='child', kind='printed_blind_label', reference_id='label-0-two')
        label = {k: v for k, v in proof.items() if k != 'signed_authority'}
        label['receipt_id'] = 'new-generation-' + str(sequence)
        corpus['publish'](db, 'printed_blind_label', label)
        inputs['cases'][0]['blind_receipt_ids'][1] = label['receipt_id']
        assert PaperPrintedCalibrationService(db).evaluate('child', **inputs)['calibration_qualified']
        revised = scoring.resolve(request['request_id'], 'child', 'printed-auto-scoring:stale-worker')
        with db.connect() as c:
            assert c.execute('SELECT revision_sequence FROM paper_scoring_decision_revision WHERE revision_id=?',
                             (revised['decision_id'],)).fetchone()[0] == sequence
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM paper_scoring_decision_revision').fetchone()[0] == 2
