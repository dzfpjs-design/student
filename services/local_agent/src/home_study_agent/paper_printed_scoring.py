"""Consume live original-scale calibration into the existing paper journal."""
from datetime import datetime, timezone
import json

from .paper_printed_calibration import (
    TYPE as CALIBRATION_TYPE, PaperPrintedCalibrationService, candidate_snapshot,
    check, check_provenance, execution_receipt, subject_for_request,
)
from .paper_printed_score_candidate import TYPE as CANDIDATE_TYPE, decimal
from .paper_subjective_rubric import TYPE as RUBRIC_TYPE, PaperSubjectiveRubricService
from .rubric_calibration_evidence import digest, timestamp
from .rubric_calibration_read import ReadScope

PREFIX = 'calibrated-printed-candidate:'
SCAN = 'paper_printed_scoring_scan_v1'


def _rule_identity(spec):
    return dict(question_sha256=spec['question_sha256'], source_text_sha256=spec['source_text_sha256'],
                criteria_hash=digest(spec['criteria']))


def _report_for_target(c, child_id, identity):
    # Archived scope is only an index. Even a withdrawn newest matching report
    # remains selected; never silently fall back to an older authorization.
    return c.execute("""SELECT artifact_id FROM capture_artifact a
        WHERE child_id=? AND artifact_type=? AND json_valid(details_json)
          AND EXISTS(SELECT 1 FROM json_each(CASE WHEN json_valid(a.details_json) THEN a.details_json ELSE '{}' END,
                     '$.evaluation.printed_application_scope') rule
            WHERE json_extract(CASE WHEN json_valid(rule.value) THEN rule.value ELSE '{}' END,'$.question_sha256')=?
              AND json_extract(CASE WHEN json_valid(rule.value) THEN rule.value ELSE '{}' END,'$.source_text_sha256')=?
              AND json_extract(CASE WHEN json_valid(rule.value) THEN rule.value ELSE '{}' END,'$.criteria_hash')=?)
        ORDER BY a.rowid DESC LIMIT 1""",
        (child_id, CALIBRATION_TYPE, identity['question_sha256'], identity['source_text_sha256'], identity['criteria_hash'])).fetchone()


def _authority(c, request, candidate_id, scope):
    child_id = request['child_id']
    candidate, ctx, fingerprint = candidate_snapshot(c, child_id, candidate_id)
    check(candidate['status'] == 'candidate_unqualified' and candidate['total_score'] is not None,
          'TARGET_ABSTAINED')
    check(candidate['request_id'] == request['request_id']
          and ctx['original_response']['session_id'] == request['session_id']
          and ctx['question'] == request['question_text'] and ctx['response'] == request['answer_text'], 'TARGET_SCOPE_CHANGED')
    spec = PaperSubjectiveRubricService(None).current(child_id, ctx['artifact_id'], connection=c)
    identity = _rule_identity(spec)
    report_row = _report_for_target(c, child_id, identity)
    if report_row is None:
        return None
    report = PaperPrintedCalibrationService(None).current(child_id, report_row[0], connection=c)
    check(report['status'] == 'current' and report['calibration_qualified'] is True, 'TARGET_CALIBRATION_WITHDRAWN')
    payload = json.loads(c.execute('SELECT details_json FROM capture_artifact WHERE artifact_id=?', (report_row[0],)).fetchone()[0])
    inputs = payload['request']
    holdout = scope.resolve(c, child_id=child_id, kind='holdout', reference_id=inputs['holdout_receipt_id'])
    check(holdout is not None and digest(holdout) == report['holdout_hash'], 'TARGET_HOLDOUT_CHANGED')
    rules = [rule for rule in holdout.get('printed_application_scope', [])
             if all(rule.get(k) == value for k, value in identity.items())]
    check(len(rules) == 1, 'TARGET_APPLICABILITY_REQUIRED')
    rule = rules[0]
    check(rule['stratum'] in report['scope']['strata']
          and rule['group_hash'] not in holdout.get('development_group_hashes', [])
          and all(rule['group_hash'] != case['group_hash']
                  and ctx['artifact_id'] != case['evidence_id']
                  and not {ctx['artifact_id'], spec['source_candidate_id'], ctx['original_response']['artifact_id']}
                          .intersection(case['source_ids']) for case in holdout['cases']), 'TARGET_HOLDOUT_LEAKAGE')
    check(subject_for_request(c, child_id, request['request_id']) == report['scope']['subject'], 'TARGET_SUBJECT_CHANGED')
    scorer = inputs['scorer']
    call, receipt, receipt_hash = execution_receipt(c, child_id, candidate, ctx)
    check(call['provider'] == scorer['provider'] and call['model'] == scorer['model']
          and timestamp(holdout['frozen_at']) < timestamp(call['created_at'])
              <= timestamp(call['completed_at']) <= datetime.now(timezone.utc), 'TARGET_SCORER_OR_TIMING')
    provenance = check_provenance(receipt, scorer)
    check(provenance.get('reported_model') == scorer['model_revision'], 'TARGET_MODEL_REVISION_CHANGED')
    revision = scope.resolve(c, child_id=child_id, kind='scorer_revision', reference_id=scorer['revision_receipt_id'])
    check(revision is not None and digest(revision) == report['revision_hash'], 'TARGET_REVISION_WITHDRAWN')
    passed = decimal(candidate['total_score']) == decimal(candidate['maximum_score'])
    snapshot = dict(type='calibrated_printed_points', printed_candidate_artifact_id=candidate_id,
        calibration_artifact_id=report_row[0], protocol_hash=report['protocol_hash'],
        application_rule=rule, candidate_sha256=fingerprint, receipt_sha256=receipt_hash,
        frozen_rubric_artifact_id=ctx['artifact_id'], frozen_rubric_sha256=ctx['spec_sha256'],
        original_response_version=ctx['original_response']['source_version'],
        points=candidate['points'], total_score=candidate['total_score'], maximum_score=candidate['maximum_score'],
        passed=passed, local_only=True, cloud_transmission_allowed=False)
    return dict(authority_type='agent_generated_rubric', authority_ref=PREFIX + candidate_id,
                correctness=int(passed), confidence=1.0, snapshot=snapshot)


def authority_for_request(c, request, *, candidate_id=None):
    if candidate_id is None:
        row = c.execute("""SELECT artifact_id FROM capture_artifact WHERE child_id=? AND artifact_type=?
            AND json_valid(details_json) AND json_extract(details_json,'$.request_id')=?
            ORDER BY rowid DESC LIMIT 1""", (request['child_id'], CANDIDATE_TYPE, request['request_id'])).fetchone()
        if row is None:
            return None
        candidate_id = row[0]
    scope = ReadScope(c, request['child_id'], None)
    try:
        try:
            return _authority(c, request, candidate_id, scope)
        finally:
            scope.finish()
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def reconcile(c, child_id, request_id=None):
    from .paper_scoring import PaperScoringService
    from .repository import utc_now
    query = """SELECT d.decision_id,d.authority_ref,d.authority_snapshot_json,r.*
        FROM paper_scoring_effective_decision d JOIN paper_scoring_request r USING(request_id)
        WHERE d.child_id=? AND d.effective_status='qualified' AND d.authority_ref LIKE ?"""
    params = [child_id, PREFIX + '%']
    if request_id:
        query += ' AND d.request_id=?'
        params.append(request_id)
    withdrawn = []
    for row in c.execute(query, params).fetchall():
        live = authority_for_request(c, row, candidate_id=row['authority_ref'][len(PREFIX):])
        try:
            valid = live is not None and live['snapshot'] == json.loads(row['authority_snapshot_json'])
        except (ValueError, TypeError):
            valid = False
        if valid:
            continue
        PaperScoringService._withdraw_invalid_decision(c, row['decision_id'], row['candidate_id'], child_id,
            'printed-score-withdraw:' + row['decision_id'], 'printed calibration or original evidence changed', utc_now())
        withdrawn.append(row['decision_id'])
    return withdrawn


class PrintedPaperScoringService:
    def __init__(self, database):
        self.database = database

    def process_batch(self, limit=8):
        from .paper_scoring import PaperScoringService
        check(type(limit) is int and 1 <= limit <= 32, 'BATCH_LIMIT_INVALID')
        # Reconciliation here may withdraw a previously approved score.
        with self.database.external_effect_guard(timeout=10), self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for child in c.execute("SELECT DISTINCT child_id FROM paper_scoring_effective_decision WHERE effective_status='qualified' AND authority_ref LIKE ?", (PREFIX + '%',)).fetchall():
                reconcile(c, child[0])
            previous = c.execute('SELECT summary_json FROM audit_event WHERE action=? ORDER BY rowid DESC LIMIT 1', (SCAN,)).fetchone()
            try:
                cursor = json.loads(previous[0]).get('last_request_id', '') if previous else ''
                if not isinstance(cursor, str): cursor = ''
            except (ValueError, TypeError, AttributeError):
                cursor = ''
            # Restrict background work to signed-scope-indexed targets; holdout
            # candidates must not starve eligible children's requests.
            rows = c.execute("""SELECT DISTINCT r.request_id,r.child_id FROM capture_artifact a
                JOIN capture_artifact rubric ON rubric.artifact_id=json_extract(CASE WHEN json_valid(a.details_json) THEN a.details_json ELSE '{}' END,'$.frozen_rubric_artifact_id')
                JOIN paper_scoring_request r ON r.request_id=json_extract(CASE WHEN json_valid(a.details_json) THEN a.details_json ELSE '{}' END,'$.request_id') AND r.child_id=a.child_id
                JOIN child_profile p ON p.child_id=r.child_id AND p.status='active'
                WHERE a.artifact_type=? AND rubric.artifact_type=?
                  AND json_valid(a.details_json) AND json_valid(rubric.details_json)
                  AND NOT EXISTS(SELECT 1 FROM paper_scoring_effective_decision d
                    WHERE d.request_id=r.request_id AND d.effective_status='qualified')
                  AND EXISTS(SELECT 1 FROM capture_artifact report,
                    json_each(CASE WHEN json_valid(report.details_json) THEN report.details_json ELSE '{}' END,'$.evaluation.printed_application_scope') rule
                    WHERE report.child_id=r.child_id AND report.artifact_type=? AND json_valid(report.details_json)
                      AND json_extract(CASE WHEN json_valid(rule.value) THEN rule.value ELSE '{}' END,'$.question_sha256')=json_extract(CASE WHEN json_valid(rubric.details_json) THEN rubric.details_json ELSE '{}' END,'$.question_sha256')
                      AND json_extract(CASE WHEN json_valid(rule.value) THEN rule.value ELSE '{}' END,'$.source_text_sha256')=json_extract(CASE WHEN json_valid(rubric.details_json) THEN rubric.details_json ELSE '{}' END,'$.source_text_sha256'))
                ORDER BY CASE WHEN r.request_id>? THEN 0 ELSE 1 END,r.request_id LIMIT ?""",
                (CANDIDATE_TYPE, RUBRIC_TYPE, CALIBRATION_TYPE, cursor, limit)).fetchall()
            actions = {}
            for row in rows:
                latest = c.execute("""SELECT a.artifact_id,rubric.details_json FROM capture_artifact a
                    JOIN capture_artifact rubric ON rubric.artifact_id=json_extract(CASE WHEN json_valid(a.details_json) THEN a.details_json ELSE '{}' END,'$.frozen_rubric_artifact_id')
                    WHERE a.child_id=? AND a.artifact_type=? AND json_valid(a.details_json)
                      AND json_extract(a.details_json,'$.request_id')=? ORDER BY a.rowid DESC LIMIT 1""",
                    (row['child_id'], CANDIDATE_TYPE, row['request_id'])).fetchone()
                try:
                    selected = _report_for_target(c, row['child_id'], _rule_identity(json.loads(latest['details_json'])))
                    actions[row['request_id']] = 'printed-auto-scoring:' + digest([row['request_id'], latest[0], selected[0] if selected else None])
                except (ValueError, TypeError, KeyError):
                    continue
            if rows and rows[-1]['request_id'] != cursor:
                from .repository import utc_now
                from uuid import uuid4
                # A queue checkpoint, never score authority. A crash skips no
                # evidence: the circular scan revisits every unqualified target.
                c.execute('INSERT INTO audit_event VALUES(?,?,?,?,?,?)', (str(uuid4()), 'local_agent', SCAN,
                    rows[-1]['child_id'], json.dumps(dict(last_request_id=rows[-1]['request_id'],
                        local_only=True, cloud_transmission_allowed=False)), utc_now()))
            c.commit()
        results = []
        for row in rows:
            if row['request_id'] not in actions:
                continue
            result = PaperScoringService(self.database).resolve(row['request_id'], row['child_id'],
                actions[row['request_id']])
            if result is not None:
                results.append(result)
        return results
