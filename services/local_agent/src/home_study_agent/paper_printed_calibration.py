"""Original-scale, signed independent calibration over real paper candidates.

The local report is not a paper decision. Its live qualification must be
consumed separately; archived metrics never authorize a changed source.
"""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from .paper_printed_score_candidate import PaperPrintedScoreCandidateService, decimal, number, parse
from .paper_subjective_rubric import PaperSubjectiveRubricService
from .rubric_calibration_evidence import POLICY, digest, encoded, is_hash, require, timestamp, upper

VERSION = 'paper_printed_calibration_v1'
TYPE = 'paper_printed_calibration'
LOCAL = {'local_only': True, 'cloud_transmission_allowed': False}
SCORER_FIELDS = {'provider', 'model', 'model_revision', 'revision_receipt_id', 'prompt_hash',
                 'parameters_hash', 'endpoint_scope_sha256', 'parser_version', 'criteria_hash'}


def check(ok, code):
    require(ok, 'PRINTED_' + code)


def summarize(cases, maxima, strata):
    """One independent group per case, source-printed zero/full point anchors."""
    check(isinstance(maxima, dict) and 1 <= len(maxima) <= 8, 'POINTS_INVALID')
    limits = {key: number(decimal(value)) for key, value in maxima.items()}
    check(all(isinstance(k, str) and 0 < len(k) <= 200 and 0 < decimal(v) <= 20
              for k, v in limits.items()), 'POINTS_INVALID')
    check(isinstance(strata, list) and 1 <= len(strata) <= 20
          and all(isinstance(s, str) and 0 < len(s) <= 200 for s in strata)
          and len(set(strata)) == len(strata), 'STRATA_INVALID')
    check(isinstance(cases, list) and len(cases) <= 10000, 'CASES_INVALID')
    groups = set()
    for case in cases:
        check(isinstance(case, dict) and set(case) == {'group_hash', 'stratum', 'gold', 'predicted'}, 'CASE_FIELDS')
        check(is_hash(case['group_hash']) and case['group_hash'] not in groups
              and case['stratum'] in strata, 'GROUP_OR_STRATUM_INVALID')
        groups.add(case['group_hash'])
        for kind in ('gold', 'predicted'):
            values = case[kind]
            check(isinstance(values, dict) and set(values) == set(limits), 'POINT_COVERAGE')
            for key, value in values.items():
                if value is None and kind == 'predicted':
                    continue
                check(isinstance(value, str) and value in {'0', limits[key]}, 'ORIGINAL_SCALE_REQUIRED')
    metrics, blockers = {}, set()
    for stratum in strata:
        rows = [r for r in cases if r['stratum'] == stratum]
        for key, maximum in limits.items():
            anchors = {score: sum(r['gold'][key] == score for r in rows) for score in ('0', maximum)}
            scored = [r for r in rows if r['predicted'][key] is not None]
            negatives = [r for r in scored if r['gold'][key] == '0']
            positives = [r for r in scored if r['gold'][key] == maximum]
            fp = sum(r['predicted'][key] == maximum for r in negatives)
            fn = sum(r['predicted'][key] == '0' for r in positives)
            value = dict(maximum_score=maximum, anchors=anchors, count=len(rows), scored_count=len(scored),
                         false_pass=fp, false_reject=fn, false_pass_upper=upper(fp, len(negatives)),
                         false_reject_upper=upper(fn, len(positives)), severe_overestimate=fp,
                         coverage=len(scored) / len(rows) if rows else 0)
            metrics[stratum + ':' + key] = value
            if min(anchors.values()) < POLICY['min_per_anchor_per_stratum']:
                blockers.add('boundary_samples_insufficient')
            if value['false_pass_upper'] > POLICY['false_pass_upper_max'] or value['false_reject_upper'] > POLICY['false_reject_upper_max']:
                blockers.add('error_bound_exceeded')
            if fp > POLICY['severe_overestimate_max']:
                blockers.add('severe_overestimate')
            if value['coverage'] < POLICY['coverage_min']:
                blockers.add('coverage_insufficient')
    return metrics, sorted(blockers)


def candidate_snapshot(c, child_id, candidate_id):
    candidate = PaperPrintedScoreCandidateService(None).current(child_id, candidate_id, connection=c)
    check(candidate['status'] in {'candidate_unqualified', 'candidate_abstained'}, 'CANDIDATE_WITHDRAWN')
    ctx = PaperSubjectiveRubricService(None).execution_context(child_id, candidate['frozen_rubric_artifact_id'], connection=c)
    check(ctx['original_response']['modality'] == 'original_paper_text'
          and ctx['original_response']['original_written_answer_available'] is True, 'ORIGINAL_PAPER_REQUIRED')
    raw = c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=? AND child_id=?', (candidate_id, child_id)).fetchone()
    return candidate, ctx, digest(json.loads(raw[0]))


def execution_receipt(c, child_id, candidate, ctx):
    from .paper_printed_score_execution import PaperPrintedScoreExecutionService, WORK_TYPE, action_for, details_of
    call = c.execute('SELECT * FROM model_call WHERE call_id=? AND child_id=?',
                     (candidate['model_call']['call_id'], child_id)).fetchone()
    check(call is not None, 'CALL_AUDIT_CHANGED')
    manifest = json.loads(call['context_manifest_json'])
    job = c.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=? AND artifact_type=?',
                    (manifest.get('execution_job_id'), child_id, call['session_id'], WORK_TYPE)).fetchone()
    check(job is not None and job['status'] == 'completed', 'EXECUTION_REQUIRED')
    details = details_of(job)
    check(job['client_action_id'] == action_for(child_id, details), 'EXECUTION_CHANGED')
    receipt = details.get('receipt')
    check(isinstance(receipt, dict) and details.get('call_id') == call['call_id']
          and details.get('candidate_artifact_id') == candidate['artifact_id']
          and digest(receipt) == details.get('receipt_sha256') == manifest.get('response_receipt_sha256'), 'RECEIPT_CHANGED')
    check(all(receipt.get(k) == call[k] for k in ('provider', 'model', 'prompt_tokens', 'completion_tokens')), 'CALL_AUDIT_CHANGED')
    mapped = PaperPrintedScoreExecutionService._mapped(receipt, ctx)
    parsed = parse(mapped, ctx)
    check(digest(mapped) == manifest.get('model_output_sha256') and parsed['points'] == candidate['points'], 'PREDICTION_CHANGED')
    return call, receipt, details['receipt_sha256']


def check_provenance(receipt, scorer):
    provenance = receipt.get('scorer_provenance')
    check(isinstance(provenance, dict) and provenance.get('version') == 'model_scorer_provenance_v1'
          and provenance.get('provider') == scorer['provider'] and provenance.get('requested_model') == scorer['model']
          and provenance.get('system_prompt_sha256') == scorer['prompt_hash']
          and provenance.get('parameters_sha256') == scorer['parameters_hash']
          and provenance.get('endpoint_scope_sha256') == scorer['endpoint_scope_sha256'], 'ACTUAL_PROVENANCE_REQUIRED')
    return provenance


def subject_for_request(c, child_id, request_id):
    draft = c.execute("""SELECT e.evidence_json FROM paper_scoring_supply_event e
        JOIN paper_scoring_supply_obligation o USING(obligation_id)
        WHERE o.request_id=? AND o.child_id=? AND e.status='rubric_candidate' ORDER BY e.rowid DESC LIMIT 1""",
        (request_id, child_id)).fetchone()
    return json.loads(draft[0]).get('subject') if draft else None


class PaperPrintedCalibrationService:
    def __init__(self, database):
        self.database = database

    def _evaluate(self, c, child_id, request, now):
        from .rubric_calibration_read import ReadScope
        scope = ReadScope(c, child_id, None)
        try:
            return self._checked(c, child_id, request, now, scope.resolve)
        finally:
            scope.finish()

    def _checked(self, c, child_id, request, now, resolve):
        from .paper_printed_score_candidate import PARSER_VERSION
        check(isinstance(request, dict) and set(request) == {'scorer', 'scope', 'holdout_receipt_id', 'cases'}, 'REQUEST_FIELDS')
        scorer, scope = request['scorer'], request['scope']
        check(isinstance(scorer, dict) and set(scorer) == SCORER_FIELDS
              and all(isinstance(v, str) and 0 < len(v) <= 200 for v in scorer.values()), 'SCORER_FIELDS')
        check(all(is_hash(scorer[k]) for k in ('prompt_hash', 'parameters_hash', 'endpoint_scope_sha256', 'criteria_hash'))
              and scorer['parser_version'] == PARSER_VERSION, 'SCORER_CHANGED')
        check(isinstance(scope, dict) and set(scope) == {'subject', 'grade', 'task_family', 'strata'}
              and all(isinstance(scope[k], str) and 0 < len(scope[k]) <= 100 for k in ('subject', 'grade', 'task_family')),
              'SCOPE_FIELDS')
        check(isinstance(scope['strata'], list) and 1 <= len(scope['strata']) <= 20
              and all(isinstance(v, str) and 0 < len(v) <= 200 for v in scope['strata'])
              and len(set(scope['strata'])) == len(scope['strata']), 'STRATA_INVALID')
        child = c.execute("SELECT grade FROM child_profile WHERE child_id=? AND status='active'", (child_id,)).fetchone()
        check(child is not None and child[0] == scope['grade'], 'CHILD_SCOPE_CHANGED')
        holdout = resolve(c, child_id=child_id, kind='holdout', reference_id=request['holdout_receipt_id'])
        check(holdout is not None and holdout.get('corpus_kind') == 'observed_original'
              and holdout.get('blind_protocol_version') == 'blind_labels_v1'
              and holdout.get('scope_hash') == digest(scope) and is_hash(holdout.get('provenance_receipt_hash')),
              'HOLDOUT_UNVERIFIED')
        frozen = timestamp(holdout['frozen_at'])
        check(frozen <= now < frozen + timedelta(days=POLICY['max_valid_days']), 'HOLDOUT_EXPIRED')
        definitions = holdout['cases']
        check(len({v['evidence_id'] for v in definitions}) == len(definitions)
              and len({v['group_hash'] for v in definitions}) == len(definitions)
              and all(is_hash(v['group_hash']) and v['group_hash'] not in holdout.get('development_group_hashes', [])
                      and v['stratum'] in scope['strata'] for v in definitions), 'HOLDOUT_LEAKAGE')
        refs = request['cases']
        check(isinstance(refs, list) and len(refs) == len(definitions), 'HOLDOUT_INCOMPLETE')
        protocol_hash = digest([VERSION, child_id, scorer, scope, holdout, digest(POLICY)])
        revision = resolve(c, child_id=child_id, kind='scorer_revision', reference_id=scorer['revision_receipt_id'])
        revision_ok = bool(revision and all(revision.get(k) == scorer[k] for k in ('provider', 'model', 'model_revision', 'endpoint_scope_sha256'))
            and revision.get('verification_method') in {'provider_immutable_revision_attestation', 'signed_release_manifest'}
            and is_hash(revision.get('evidence_hash'))
            and timestamp(revision['verified_at']) <= frozen < now < timestamp(revision['expires_at']))
        cases, bindings, seen, maxima = [], [], set(), None
        for ref in refs:
            check(isinstance(ref, dict) and set(ref) == {'candidate_id', 'blind_receipt_ids'}, 'CASE_REFERENCE_FIELDS')
            check(isinstance(ref['candidate_id'], str) and 0 < len(ref['candidate_id']) <= 200, 'CASE_REFERENCE_FIELDS')
            candidate, ctx, fingerprint = candidate_snapshot(c, child_id, ref['candidate_id'])
            aid = ctx['artifact_id']
            definition = next((d for d in definitions if d['evidence_id'] == aid), None)
            check(definition is not None and aid not in seen, 'CASE_NOT_IN_HOLDOUT')
            seen.add(aid)
            spec = PaperSubjectiveRubricService(None).current(child_id, aid, connection=c)
            rubric_created = c.execute('SELECT created_at FROM capture_artifact WHERE artifact_id=?', (aid,)).fetchone()[0]
            check(timestamp(rubric_created) <= frozen, 'RUBRIC_AFTER_HOLDOUT')
            check(set(definition['source_ids']) == {aid, spec['source_candidate_id'], ctx['original_response']['artifact_id']}
                  and len(definition['source_ids']) == 3, 'HOLDOUT_SOURCE_IDS')
            check(subject_for_request(c, child_id, candidate['request_id']) == scope['subject'], 'SUBJECT_SCOPE_CHANGED')
            check(digest(ctx['criteria']) == scorer['criteria_hash'], 'CRITERIA_CHANGED')
            previous_calls = c.execute("""SELECT created_at FROM model_call WHERE child_id=?
                AND purpose='paper_printed_score_candidate' AND json_valid(context_manifest_json)
                AND (json_extract(context_manifest_json,'$.paper_execution.frozen_rubric_artifact_id')=?
                     OR json_extract(context_manifest_json,'$.paper_execution.original_response_version')=?)""",
                (child_id, aid, ctx['original_response']['source_version'])).fetchall()
            check(all(timestamp(row[0]) > frozen for row in previous_calls), 'HOLDOUT_ALREADY_SCORED')
            limits = {p['point_id']: p['maximum_score'] for p in candidate['points']}
            check(maxima is None or limits == maxima, 'SCALE_CHANGED')
            maxima = limits
            call, receipt, receipt_sha256 = execution_receipt(c, child_id, candidate, ctx)
            check(call['provider'] == scorer['provider'] and call['model'] == scorer['model']
                  and frozen < timestamp(call['created_at']) <= timestamp(call['completed_at']) <= now, 'SCORER_OR_HOLDOUT_TIMING')
            provenance = check_provenance(receipt, scorer)
            revision_ok = revision_ok and provenance.get('reported_model') == scorer['model_revision']
            labels, identities, reviewers, label_hashes = [], set(), set(), []
            ids = ref['blind_receipt_ids']
            check(isinstance(ids, list) and len(ids) == 2
                  and all(isinstance(rid, str) and 0 < len(rid) <= 200 for rid in ids)
                  and len(set(ids)) == 2, 'TWO_BLIND_LABELS_REQUIRED')
            for rid in ids:
                proof = resolve(c, child_id=child_id, kind='printed_blind_label', reference_id=rid)
                check(proof is not None and proof.get('identity_authority') == 'independent_reviewer_registry_v1'
                      and proof.get('mask_version') == 'blind_no_predictions_or_peer_labels_v1'
                      and all(is_hash(proof.get(k)) for k in ('identity_receipt_hash', 'display_receipt_hash', 'submission_receipt_hash')),
                      'BLIND_IDENTITY_UNVERIFIED')
                check(proof.get('protocol_hash') == protocol_hash and proof.get('evidence_id') == aid
                      and proof.get('source_version') == ctx['spec_sha256']
                      and proof.get('candidate_sha256') == fingerprint, 'BLIND_SOURCE_CHANGED')
                check(frozen <= timestamp(proof['displayed_at']) <= timestamp(proof['submitted_at']) <= now, 'BLIND_TIMING_INVALID')
                check(proof['reviewer_principal_id'] not in identities and proof['reviewer_id'] not in reviewers, 'REVIEWERS_NOT_INDEPENDENT')
                identities.add(proof['reviewer_principal_id']); reviewers.add(proof['reviewer_id'])
                labels.append(proof['scores']); label_hashes.append(digest(proof))
            check(labels[0] == labels[1], 'BLIND_LABELS_DISAGREE')
            cases.append(dict(group_hash=definition['group_hash'], stratum=definition['stratum'], gold=labels[0],
                              predicted={p['point_id']: p['score'] for p in candidate['points']}))
            bindings.append(dict(candidate_id=ref['candidate_id'], candidate_sha256=fingerprint,
                                 source_version=ctx['spec_sha256'], blind_hashes=label_hashes,
                                 receipt_sha256=receipt_sha256, session_id=ctx['original_response']['session_id']))
        metrics, blockers = summarize(cases, maxima, scope['strata'])
        if not revision_ok:
            blockers.append('model_revision_unverified')
        return dict(version=VERSION, protocol_hash=protocol_hash, policy_hash=digest(POLICY),
                    scope=scope, holdout_hash=digest(holdout), revision_hash=digest(revision),
                    bindings=bindings, metrics=metrics, blockers=sorted(set(blockers)),
                    calibration_qualified=not blockers, score_authority_granted=False,
                    **({'printed_application_scope': holdout['printed_application_scope']}
                       if 'printed_application_scope' in holdout else {}), **LOCAL)

    def evaluate(self, child_id, *, scorer, scope, holdout_receipt_id, cases):
        request = dict(scorer=scorer, scope=scope, holdout_receipt_id=holdout_receipt_id, cases=cases)
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            now = datetime.now(timezone.utc)
            result = self._evaluate(c, child_id, request, now)
            payload = dict(request=request, evaluation=result)
            action = 'paper-printed-calibration:' + digest([child_id, payload])
            old = c.execute('SELECT * FROM capture_artifact WHERE child_id=? AND artifact_type=? AND client_action_id=?',
                            (child_id, TYPE, action)).fetchone()
            if old:
                check(json.loads(old['details_json']) == payload and old['session_id'] == result['bindings'][0]['session_id'], 'REPORT_CORRUPT')
            aid = old['artifact_id'] if old else str(uuid4())
            if not old:
                c.execute("INSERT INTO capture_artifact VALUES(?,?,?,?,NULL,NULL,NULL,?,'completed',?,?)",
                          (aid, child_id, result['bindings'][0]['session_id'], TYPE, encoded(payload), now.isoformat(), action))
            c.commit()
            return dict(artifact_id=aid, **result)

    def current(self, child_id, artifact_id, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN')
                return self.current(child_id, artifact_id, connection=c)
        if not connection.in_transaction:
            connection.execute('BEGIN')
            try:
                return self.current(child_id, artifact_id, connection=connection)
            finally:
                connection.rollback()
        row = connection.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND artifact_type=?',
                                 (artifact_id, child_id, TYPE)).fetchone()
        check(row is not None, 'REPORT_UNKNOWN')
        payload = json.loads(row['details_json'])
        check(isinstance(payload, dict) and set(payload) == {'request', 'evaluation'}
              and row['client_action_id'] == 'paper-printed-calibration:' + digest([child_id, payload]), 'REPORT_CORRUPT')
        try:
            live = self._evaluate(connection, child_id, payload['request'], datetime.now(timezone.utc))
            check(live == payload['evaluation'] and row['session_id'] == live['bindings'][0]['session_id'], 'REPORT_DEPENDENCY_CHANGED')
        except (ValueError, KeyError, TypeError, AttributeError):
            return dict(artifact_id=artifact_id, status='withdrawn', calibration_qualified=False,
                        score_authority_granted=False, blockers=['calibration_dependency_changed'], **LOCAL)
        return dict(artifact_id=artifact_id, status='current', **live)

    def latest_for_candidate(self, child_id, candidate_id, *, connection):
        if not connection.in_transaction:
            connection.execute('BEGIN')
            try:
                return self.latest_for_candidate(child_id, candidate_id, connection=connection)
            finally:
                connection.rollback()
        row = connection.execute("""SELECT a.artifact_id FROM capture_artifact a
            WHERE a.child_id=? AND a.artifact_type=? AND json_valid(a.details_json)
              AND EXISTS(SELECT 1 FROM json_each(a.details_json,'$.request.cases') item
                         WHERE json_extract(item.value,'$.candidate_id')=?)
            ORDER BY a.rowid DESC LIMIT 1""", (child_id, TYPE, candidate_id)).fetchone()
        if row is None:
            from .paper_printed_scoring import _report_for_target, _rule_identity
            try:
                candidate, ctx, _ = candidate_snapshot(connection, child_id, candidate_id)
                spec = PaperSubjectiveRubricService(None).current(child_id, ctx['artifact_id'], connection=connection)
                row = _report_for_target(connection, child_id, _rule_identity(spec))
            except (ValueError, TypeError, KeyError):
                return None
            if row is None:
                return None
        current = self.current(child_id, row[0], connection=connection)
        return {key: current[key] for key in ('artifact_id', 'status', 'calibration_qualified',
                                             'score_authority_granted', 'blockers', 'local_only', 'cloud_transmission_allowed')}
