"""Isolated protocol tests; synthetic fixtures never constitute deployed calibration."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from uuid import uuid4

import pytest

from home_study_agent.database import Database
from home_study_agent.rubric_calibration import (
    RubricCalibrationService, initialize, quarantine_after_restore,
    export_rows, delete_for_child, invalidate_sources, digest,
)
from test_mistake_gate_reliability import flow, CHILD
from home_study_agent import learning_state
from home_study_agent import learning_rubric
from home_study_agent.learning_rubric import RubricEvidenceService, rubric_dimensions, RUBRIC_VERSION
from home_study_agent.rubric_calibration_evidence import source_snapshot, summarize_cases


class Corpus:
    """Synthetic host adapter over real SQLite journals; NEVER a production corpus.

    The authority table is test-only. Positive cases simulate a future identity
    registry, not the current single family principal and not calibrated quality.
    """
    def __init__(self, flow, monkeypatch, count=1):
        self.db, self.learning, self.sid, self.clock = flow
        clock = self.clock
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
        monkeypatch.setattr(learning_rubric, 'datetime', Clock)
        self.rubrics = RubricEvidenceService(self.db)
        self.rubrics.initialize()
        self.definitions = rubric_dimensions('数学')
        self.scorer = {**scorer(), 'model_revision': 'immutable-test-revision', 'rubric_hash': digest(self.definitions)}
        self.scope = {**scope(), 'strata': ['ordinary']}
        self.service = RubricCalibrationService(self.db, authority_resolver=self.resolve, clock=lambda: self.clock[0])
        self.service.initialize()
        with self.db.connect() as c:
            c.execute('CREATE TABLE test_calibration_authority (id TEXT,child_id TEXT,kind TEXT,payload TEXT,PRIMARY KEY(id,kind))')
            c.commit()
        self.item_ids = ['calibration-synthetic-' + str(i) for i in range(count + 1)]
        items = [(iid, 'node-p4-math-arithmetic', 'Isolated synthetic process task ' + str(i), 'text',
                  {'local_rule': 'local_exact_v1', 'exact_answers': ['90']}, 'Synthetic fixture', 1)
                 for i, iid in enumerate(self.item_ids)]
        monkeypatch.setattr(learning_state, 'ITEMS', (*learning_state.ITEMS, *items))
        self.learning.seed_catalog()
        self.answers = [self.learning.submit_diagnostic(self.sid, iid, 'Synthetic response ' + str(i), 'answer-' + str(i))
                        for i, iid in enumerate(self.item_ids)]
        self.holdout = dict(receipt_id='holdout', child_id=CHILD, status='current', corpus_kind='observed_original',
            blind_protocol_version='blind_labels_v1', provenance_receipt_hash='a'*64, scope_hash=digest(self.scope),
            frozen_at=self.clock[0].isoformat(), development_group_hashes=[],
            cases=[dict(evidence_id=a['evidence_id'], group_hash=digest(['test-original', i]), stratum='ordinary', source_ids=[])
                   for i, a in enumerate(self.answers[:-1])])
        self.save('holdout', self.holdout)
        self.revision = dict(receipt_id='revision-receipt', child_id=CHILD, status='current',
            provider=self.scorer['provider'], model=self.scorer['model'], model_revision=self.scorer['model_revision'],
            verification_method='signed_release_manifest', evidence_hash='b'*64,
            endpoint_scope_sha256='c'*64,
            verified_at=self.clock[0].isoformat(), expires_at=(self.clock[0]+timedelta(days=60)).isoformat())
        self.save('scorer_revision', self.revision)
        self.frozen = None
        self.objective = None

    def save(self, kind, value):
        with self.db.connect() as c:
            c.execute('INSERT OR REPLACE INTO test_calibration_authority VALUES (?,?,?,?)',
                      (value['receipt_id'], value['child_id'], kind, json.dumps(value)))
            c.commit()

    @staticmethod
    def resolve(c, *, child_id, kind, reference_id):
        row = c.execute('SELECT payload FROM test_calibration_authority WHERE id=? AND child_id=? AND kind=?',
                        (reference_id, child_id, kind)).fetchone()
        return json.loads(row[0]) if row else None

    def freeze(self):
        self.frozen = self.service.freeze(CHILD, scorer=self.scorer, scope=self.scope,
                                         holdout_receipt_id='holdout', client_action_id='freeze')
        return self.frozen

    @property
    def pid(self):
        return self.frozen['current']['protocol_id']

    def score(self, index=0, score=2, *, gold=None, binding=True, provenance_override=None):
        self.clock[0] += timedelta(seconds=1)
        now = self.clock[0].isoformat()
        evidence_id = self.answers[index]['evidence_id']
        call, objective, turn, plan, step, aid = [str(uuid4()) for _ in range(6)]
        with self.db.connect() as c:
            answer = c.execute('SELECT submitted_answer FROM diagnostic_attempt WHERE evidence_id=?', (evidence_id,)).fetchone()[0]
            question = c.execute('SELECT prompt FROM diagnostic_item WHERE diagnostic_item_id=?', (self.item_ids[index],)).fetchone()[0]
            criteria = ['A reasoned solution']
            if self.objective is None:
                c.execute("INSERT INTO agent_objective VALUES (?,?,?,'learning',?,?,'diagnose','active',?,?,NULL,NULL)",
                          (objective, CHILD, self.sid, 'Synthetic calibration', json.dumps(criteria), now, now))
                self.objective = objective
            else:
                objective = self.objective
            c.execute('''INSERT INTO agent_turn(agent_turn_id,child_id,session_id,transcript,intent,response_text,
                action_json,orchestrator_version,created_at,client_action_id) VALUES (?,?,?,?,'learning.evaluate_response','','{}','test',?,?)''',
                      (turn, CHILD, self.sid, answer, now, turn))
            c.execute("INSERT INTO agent_plan VALUES (?,?,?,?,?,'test','{}','{}','planned',?,?)", (plan, objective, CHILD, self.sid, turn, now, now))
            args = dict(response=answer, question_asked=question, subject='数学', evidence_id=evidence_id)
            c.execute("INSERT INTO agent_step VALUES (?,?,0,'learning.evaluate_response','1','local_model','medium',?,'[]','planned',?,NULL,NULL,?,?)",
                      (step, plan, json.dumps(args), step, now, now))
            c.execute('''INSERT INTO model_call(call_id,request_id,session_id,child_id,purpose,provider,model,status,input_chars,
                prompt_version,context_manifest_json,risk_level,provider_preference,policy_version,verification_status,safety_status,created_at)
                VALUES (?,?,?,?,'learning_outcome_evaluation',?,?,'completed',0,'test','{}','medium','isolated','test','partially_verified','passed',?)''',
                (call, call, self.sid, CHILD, self.scorer['provider'], self.scorer['model'], now))
            dims = [{**d, 'score': score, 'response_quotes': [answer], 'source_quotes': [], 'rationale': 'Synthetic fixture'} for d in self.definitions]
            payload = dict(rubric_version=RUBRIC_VERSION, profile='math_process', source_kind='model_candidate', objective_id=objective,
                session_id=self.sid, subject='数学', question=question, response=answer, criteria=criteria, dimensions=dims,
                responded_at=now, assessment_modality='text_only', automatic_quality_status='not_calibrated',
                source_evidence_id=evidence_id, source_evidence_version_hash=self.rubrics._diagnostic_source_version(c,CHILD,evidence_id),
                material=self.rubrics._material(c,CHILD,question,'数学',evidence_id), independence_eligible=False)
            if binding:
                payload['calibration_binding'] = dict(protocol_id=self.pid, protocol_hash=self.frozen['current']['protocol_hash'],
                    scorer_hash=digest(self.scorer), source_version=source_snapshot(c,CHILD,evidence_id)['version'])
            self.provenance = dict(version='model_scorer_provenance_v1', provider=self.scorer['provider'], requested_model=self.scorer['model'],
                reported_model=self.scorer['model'], provider_system_fingerprint=None, endpoint_scope_sha256='c'*64,
                system_prompt_sha256=self.scorer['prompt_hash'], parameters_sha256=self.scorer['parameters_hash'],
                immutable_revision_verified=False, local_only=True, cloud_transmission_allowed=False)
            self.provenance.update(provenance_override or {})
            payload['scorer_provenance'] = self.provenance
            c.execute('INSERT INTO rubric_assessment VALUES (?,?,?,?,?,?,?)',
                      (aid, step, CHILD, call, json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',',':')), digest(payload), now))
            c.commit()
        self.save('assessment_scorer', dict(receipt_id=call, child_id=CHILD, status='current', scorer_hash=digest(self.scorer),
            protocol_hash=self.frozen['current']['protocol_hash'], scope_hash=digest(self.scope), stratum='ordinary',
            model_revision=self.scorer['model_revision'], provenance_hash=digest(self.provenance)))
        if gold is None:
            return aid, []
        ids = []
        for j in range(2):
            label = self.rubrics.review(CHILD, aid, expected_revision=j, client_action_id=aid+str(j), reviewer_id='test-reviewer-'+str(j),
                reviewer_role='teacher', operation='approve', reason='Isolated blind test label', independent=False, hint_level=1,
                dimensions=[dict(dimension_id=d['dimension_id'],score=gold,response_quotes=[answer],source_quotes=[],rationale='Synthetic label') for d in dims],
                criterion_evidence=[dict(criterion=v,response_quote=answer) for v in criteria])
            with self.db.connect() as c:
                event = dict(c.execute('SELECT * FROM rubric_review_event WHERE event_id=?', (label['event_id'],)).fetchone())
                version = source_snapshot(c,CHILD,evidence_id)['version']
                fingerprint = c.execute('SELECT fingerprint FROM rubric_assessment WHERE assessment_id=?', (aid,)).fetchone()[0]
            rid = 'blind-'+aid+str(j)
            proof = dict(receipt_id=rid, child_id=CHILD, status='current', identity_authority='independent_reviewer_registry_v1',
                identity_receipt_hash=digest(['test-identity',j]), display_receipt_hash=digest(['test-display',rid]),
                submission_receipt_hash=digest(['test-submission',rid]), mask_version='blind_no_predictions_or_peer_labels_v1',
                protocol_hash=self.frozen['current']['protocol_hash'], assessment_fingerprint=fingerprint,
                evidence_id=evidence_id, source_version=version, reviewer_principal_id='registry-'+str(j),
                reviewer_id=event['reviewer_id'], review_event_id=event['event_id'], review_event_hash=digest(event),
                displayed_at=self.frozen['receipt']['created_at'], submitted_at=event['created_at'])
            self.save('blind_label', proof)
            ids.append(rid)
        return aid, ids


@pytest.fixture
def corpus(flow, monkeypatch):
    return Corpus(flow, monkeypatch)


def scorer():
    return dict(provider='isolated', model='model-alias', model_revision=None,
        revision_receipt_id='revision-receipt', prompt_hash='1'*64,
        profile_id='math_process', profile_version='open_answer_rubric_v1',
        rubric_hash='2'*64, parser_version='parser-v1', projection_version='projection-v2',
        parameters_hash='3'*64, modality='typed_text')


def scope():
    return dict(subject='数学', grade='四年级', task_family='mathematical_process',
                strata=['ordinary', 'boundary'])


@pytest.fixture
def protocol(flow):
    db, _, _, clock = flow
    service = RubricCalibrationService(db, clock=lambda: clock[0])
    service.initialize()
    args = dict(scorer=scorer(), scope=scope(), holdout_receipt_id='no-real-holdout', client_action_id='freeze')
    return db, service, clock, args


def test_initialize_is_borrowed_and_does_not_commit(flow):
    db = flow[0]
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        initialize(c)
        c.rollback()
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='rubric_calibration_protocol'").fetchone()


def test_missing_blind_identity_holdout_and_revision_are_explicitly_insufficient(protocol):
    db, service, _, args = protocol
    frozen = service.freeze(CHILD, **args)
    view = frozen['current']
    assert view['status'] == 'insufficient'
    assert {'holdout_unverified', 'model_revision_unverified'} <= set(view['blockers'])
    assert not view['score_qualified']
    assert view['requirements']['policy']['version'] == 'rubric_calibration_policy_v1'
    assert view['requirements']['blind_identity'] == 'independent_authenticated_reviewers_required'
    assert 'question' not in str(export_rows_for(db, CHILD))


def export_rows_for(db, child):
    with db.connect() as c:
        return export_rows(c, child)


def test_protocol_replay_cas_revoke_and_restart_do_not_regrant(protocol):
    db, service, clock, args = protocol
    first = service.freeze(CHILD, **args)
    pid = first['current']['protocol_id']
    evaluated = service.evaluate(CHILD, pid, expected_revision=1, client_action_id='evaluate')
    assert evaluated['current']['status'] == 'insufficient'
    with pytest.raises(ValueError, match='REVISION_CONFLICT'):
        service.revoke(CHILD, pid, expected_revision=1, client_action_id='stale')
    service.revoke(CHILD, pid, expected_revision=2, client_action_id='revoke')
    reopened = RubricCalibrationService(Database(db.path), clock=lambda: clock[0])
    retry = reopened.freeze(CHILD, **args)
    assert retry['receipt'] == first['receipt']
    assert retry['current']['status'] == 'revoked'
    changed = deepcopy(args)
    changed['scorer']['model'] = 'different-model'
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        reopened.freeze(CHILD, **changed)


def test_cross_child_read_command_and_delete_are_exact(protocol):
    db, service, _, args = protocol
    first = service.freeze(CHILD, **args)
    pid = first['current']['protocol_id']
    with pytest.raises(KeyError):
        service.read('child-middle-3', pid)
    with pytest.raises(KeyError):
        service.revoke('child-middle-3', pid, expected_revision=1, client_action_id='revoke')
    with db.connect() as c:
        assert delete_for_child(c, 'child-middle-3') == 0
        assert len(export_rows(c, CHILD)['protocols']) == 1
        assert delete_for_child(c, CHILD) > 0
        c.commit()
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(KeyError):
        service.read(CHILD, pid)


def test_restore_appends_quarantine_without_rewriting_authority(protocol):
    db, service, clock, args = protocol
    first = service.freeze(CHILD, **args)
    pid = first['current']['protocol_id']
    with db.connect() as c:
        before = dict(c.execute('SELECT * FROM rubric_calibration_protocol').fetchone())
        c.execute('BEGIN IMMEDIATE')
        assert quarantine_after_restore(c, 'restore-1') == 1
        assert quarantine_after_restore(c, 'restore-1') == 0
        c.commit()
        assert dict(c.execute('SELECT * FROM rubric_calibration_protocol').fetchone()) == before
    restarted = RubricCalibrationService(Database(db.path), clock=lambda: clock[0])
    current = restarted.freeze(CHILD, **args)['current']
    assert current['status'] == 'quarantined'
    assert 'restore_revalidation_required' in current['blockers']
    with pytest.raises(ValueError, match='PROTOCOL_INACTIVE'):
        restarted.evaluate(CHILD, pid, expected_revision=current['revision'], client_action_id='again')


def test_no_caller_thresholds_or_forged_status_can_lower_policy(protocol):
    _, service, _, args = protocol
    for bad in ({'min_samples': 1}, {'qualified': True}):
        with pytest.raises(TypeError):
            service.freeze(CHILD, **args, **bad)
    args['scorer']['arbitrary_metadata'] = {'immutable': True}
    with pytest.raises(ValueError, match='SCORER_FIELDS'):
        service.freeze(CHILD, **args)


def test_actual_answer_and_two_blind_review_events_are_not_enough_without_sample_coverage(corpus):
    corpus.freeze()
    aid, labels = corpus.score(gold=2)
    recorded = corpus.service.record_case(CHILD, corpus.pid, assessment_id=aid, blind_receipt_ids=labels,
                                          expected_revision=1, client_action_id='case')
    assert recorded['current']['status'] == 'insufficient'
    assert 'boundary_samples_insufficient' in recorded['current']['blockers']
    assert corpus.service.record_case(CHILD, corpus.pid, assessment_id=aid, blind_receipt_ids=labels,
                                      expected_revision=1, client_action_id='case')['receipt'] == recorded['receipt']
    with corpus.db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_assessment').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM rubric_review_event').fetchone()[0] == 2


@pytest.mark.parametrize('fault', ['synthetic','development_overlap','same_group'])
def test_unverified_or_leaked_holdout_never_freezes_as_qualified(corpus, fault):
    if fault == 'synthetic':
        corpus.holdout['corpus_kind'] = 'synthetic'
    elif fault == 'development_overlap':
        corpus.holdout['development_group_hashes'] = [corpus.holdout['cases'][0]['group_hash']]
    else:
        corpus.holdout['cases'].append(deepcopy(corpus.holdout['cases'][0]))
    corpus.save('holdout',corpus.holdout)
    with pytest.raises(ValueError, match='HOLDOUT'):
        corpus.freeze()
    with corpus.db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_calibration_protocol').fetchone()[0] == 0


@pytest.mark.parametrize('fault', ['family_principal','same_principal','ordinary_review','scorer_alias','source','legacy_assessment'])
def test_real_journal_does_not_make_arbitrary_identity_or_source_metadata_trusted(corpus, fault):
    corpus.freeze()
    aid, labels = corpus.score(gold=2, binding=fault!='legacy_assessment')
    with corpus.db.connect() as c:
        if fault == 'source':
            c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (corpus.item_ids[0],))
        elif fault == 'scorer_alias':
            c.execute("DELETE FROM test_calibration_authority WHERE kind='assessment_scorer'")
        else:
            proof = corpus.resolve(c, child_id=CHILD, kind='blind_label', reference_id=labels[1])
            if fault=='family_principal': proof['identity_authority']='family_parent_auth'
            elif fault=='same_principal': proof['reviewer_principal_id']='registry-0'
            elif fault=='ordinary_review': proof.pop('display_receipt_hash')
            c.execute('UPDATE test_calibration_authority SET payload=? WHERE id=?', (json.dumps(proof), labels[1]))
        c.commit()
    with pytest.raises(ValueError):
        corpus.service.record_case(CHILD, corpus.pid, assessment_id=aid, blind_receipt_ids=labels,
                                   expected_revision=1, client_action_id='case')


def test_statistics_use_each_dimension_boundary_and_abstentions_not_mean_confidence():
    cases = [dict(stratum='standard', gold={'a': i//50, 'b': i//50}, predicted={'a': i//50, 'b': i//50}) for i in range(200)]
    metrics, blockers = summarize_cases(cases, ['a','b'], ['standard'])
    assert blockers == [] and metrics['standard:a']['false_pass_upper'] < .05
    cases[0]['predicted']['b'] = 3
    assert 'severe_overestimate' in summarize_cases(cases, ['a','b'], ['standard'])[1]
    cases[0]['predicted']['b'] = 0
    for case in cases[:20]: case['predicted']['b'] = None
    assert 'coverage_insufficient' in summarize_cases(cases, ['a','b'], ['standard'])[1]
    assert 'boundary_samples_insufficient' in summarize_cases(cases, ['a','b'], ['standard','missing-layer'])[1]


def test_actual_transport_parameter_mismatch_cannot_be_laundered_by_scorer_reference(corpus):
    corpus.freeze()
    aid, labels = corpus.score(gold=2, provenance_override={'parameters_sha256':'0'*64})
    with pytest.raises(ValueError, match='PROVENANCE'):
        corpus.service.record_case(CHILD,corpus.pid,assessment_id=aid,blind_receipt_ids=labels,
                                   expected_revision=1,client_action_id='case')


def test_read_only_empty_operational_requirements_do_not_create_protocols(protocol):
    db, service, _, _ = protocol
    with db.connect() as c:
        before = c.execute('SELECT COUNT(*) FROM rubric_calibration_event').fetchone()[0]
    view = service.list_for_child(CHILD)
    assert view['items'] == [] and view['requirements']['production_authority_available'] is False
    with db.connect() as c:
        assert c.execute('SELECT COUNT(*) FROM rubric_calibration_event').fetchone()[0] == before


def test_uncalibrated_protocol_cannot_mint_dispatch_binding(corpus):
    corpus.freeze()
    with corpus.db.connect() as c:
        with pytest.raises(ValueError,match='NOT_QUALIFIED'):
            corpus.service.prepare_score_binding(c,CHILD,protocol_id=corpus.pid,
                evidence_id=corpus.answers[-1]['evidence_id'],as_of=corpus.clock[0])


def test_precise_source_hash_and_append_only_invalidation_borrow_transaction(corpus):
    from home_study_agent.knowledge_base import KnowledgeBaseService
    source = KnowledgeBaseService(corpus.db).import_source(CHILD,dict(source_type='school_material',
        license_status='family_owned_index',parent_reviewed=True,title='Synthetic holdout source',
        subject='数学',grade_band='四年级',content_text='Synthetic source, not a calibration dataset'))
    corpus.holdout['cases'][0]['source_ids'] = [source['source_id']]
    corpus.save('holdout',corpus.holdout)
    corpus.freeze()
    with corpus.db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        assert invalidate_sources(c,'child-middle-3',[source['source_id']],erasure_id='erase') == 0
        assert invalidate_sources(c,CHILD,['unrelated-source'],erasure_id='erase') == 0
        assert invalidate_sources(c,CHILD,[source['source_id']],erasure_id='erase') == 1
        c.rollback()
    assert corpus.service.read(CHILD,corpus.pid)['status'] != 'revoked'
    with corpus.db.connect() as c:
        c.execute("UPDATE knowledge_source SET title='changed source' WHERE source_id=?", (source['source_id'],))
        c.commit()
    assert 'source_changed' in corpus.service.read(CHILD,corpus.pid)['blockers']
    with corpus.db.connect() as c:
        assert invalidate_sources(c,CHILD,[source['source_id']],erasure_id='erase') == 1
        assert invalidate_sources(c,CHILD,[source['source_id']],erasure_id='erase') == 0
        c.commit()
    assert corpus.service.read(CHILD,corpus.pid)['status'] == 'revoked'


def test_concurrent_cas_has_one_winner_and_storage_failure_rolls_back(protocol):
    from concurrent.futures import ThreadPoolExecutor
    db, service, _, args = protocol
    pid = service.freeze(CHILD,**args)['current']['protocol_id']
    def attempt(i):
        try:
            return service.evaluate(CHILD,pid,expected_revision=1,client_action_id='eval-'+str(i))
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt,range(2)))
    assert sum(isinstance(v,dict) for v in results) == 1
    assert 'RUBRIC_CALIBRATION_REVISION_CONFLICT' in results
    with db.connect() as c:
        c.execute("CREATE TRIGGER fail_calibration BEFORE INSERT ON rubric_calibration_event BEGIN SELECT RAISE(ABORT,'fixture fault'); END")
        c.commit()
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError,match='fixture fault'):
        service.revoke(CHILD,pid,expected_revision=2,client_action_id='revoke')
    assert service.read(CHILD,pid)['revision'] == 2
    with db.connect() as c:
        c.execute('DROP TRIGGER fail_calibration')
        c.commit()
    assert service.revoke(CHILD,pid,expected_revision=2,client_action_id='revoke')['current']['status'] == 'revoked'


@pytest.mark.parametrize('fault', ['review_retracted','holdout_changed','revision_expired','protocol_expired'])
def test_existing_case_rechecks_counterevidence_and_current_authorities(corpus,fault):
    corpus.freeze()
    aid, labels = corpus.score(gold=2)
    corpus.service.record_case(CHILD,corpus.pid,assessment_id=aid,blind_receipt_ids=labels,expected_revision=1,client_action_id='case')
    if fault=='review_retracted':
        corpus.rubrics.review(CHILD,aid,expected_revision=2,client_action_id='retract-label',reviewer_id='test-reviewer-1',
                             reviewer_role='teacher',operation='retract',reason='Counterevidence')
    elif fault=='holdout_changed':
        corpus.holdout['status']='withdrawn'
        corpus.save('holdout',corpus.holdout)
    elif fault=='revision_expired':
        corpus.revision['expires_at']=corpus.clock[0].isoformat()
        corpus.save('scorer_revision',corpus.revision)
    else:
        corpus.clock[0] += timedelta(days=31)
    result=corpus.service.read(CHILD,corpus.pid)
    assert not result['score_qualified']
    reasons={'review_retracted':'assessment_retracted','holdout_changed':'holdout_unverified',
             'revision_expired':'model_revision_unverified','protocol_expired':'protocol_expired'}
    assert reasons[fault] in result['blockers']


def test_full_fixed_policy_qualifies_exact_new_assessment_not_mastery_and_withdraws(flow, monkeypatch):
    corpus = Corpus(flow,monkeypatch,count=200)
    corpus.freeze()
    for i in range(200):
        aid, labels = corpus.score(i,score=i//50,gold=i//50)
        corpus.service.record_case(CHILD,corpus.pid,assessment_id=aid,blind_receipt_ids=labels,
            expected_revision=i+1,client_action_id='case-'+str(i))
    result = corpus.service.evaluate(CHILD,corpus.pid,expected_revision=201,client_action_id='evaluate')
    assert result['current']['status'] == 'qualified'
    with corpus.db.connect() as c:
        packet=corpus.service.prepare_score_binding(c,CHILD,protocol_id=corpus.pid,
            evidence_id=corpus.answers[200]['evidence_id'],as_of=corpus.clock[0])
        assert corpus.service.revalidate_score_binding(c,CHILD,packet,phase='dispatch',as_of=corpus.clock[0]) == packet
        forged={**packet,'source_evidence_id':corpus.answers[0]['evidence_id']}
        with pytest.raises(ValueError):
            corpus.service.revalidate_score_binding(c,CHILD,forged,phase='dispatch',as_of=corpus.clock[0])
    fresh, _ = corpus.score(200,score=3)
    reopened = RubricCalibrationService(Database(corpus.db.path),authority_resolver=corpus.resolve,clock=lambda:corpus.clock[0])
    with corpus.db.connect() as c:
        receipt = reopened.qualified_score_receipt(c,CHILD,fresh,protocol_id=corpus.pid,as_of=corpus.clock[0])
        assert receipt['score_qualified'] and receipt['passed'] and not receipt['independence_qualified']
        assert receipt['source_evidence_id'] == corpus.answers[200]['evidence_id']
        assert reopened.revalidate_score_binding(c,CHILD,packet,phase='recover',as_of=corpus.clock[0],assessment_id=fresh)['score_qualified']
        assert c.execute('SELECT COUNT(*) FROM diagnostic_attempt').fetchone()[0] == 201
        c.execute("UPDATE diagnostic_item SET status='retired' WHERE diagnostic_item_id=?", (corpus.item_ids[0],))
        c.commit()
    assert reopened.read(CHILD,corpus.pid)['status'] == 'invalidated'
    with corpus.db.connect() as c:
        assert not reopened.qualified_score_receipt(c,CHILD,fresh,protocol_id=corpus.pid,as_of=corpus.clock[0])['score_qualified']
