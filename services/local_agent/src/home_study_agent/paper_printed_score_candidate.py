"""Source-bound printed-point candidates, never a grading certificate."""
from decimal import Decimal, localcontext
import json
import re
from uuid import uuid4

from .paper_subjective_rubric import PaperSubjectiveRubricService, POINT, digest
from .repository import utc_now

VERSION = 'paper_printed_score_candidate_v1'
PARSER_VERSION = 'paper_printed_points_parser_v1'
PURPOSE = 'paper_printed_score_candidate'
TYPE = 'paper_printed_score_candidate'


def require(condition, code):
    if not condition:
        raise ValueError('PRINTED_' + code)


def execution_binding(context):
    return dict(version=VERSION, parser_version=PARSER_VERSION,
                frozen_rubric_artifact_id=context['artifact_id'],
                frozen_rubric_sha256=context['spec_sha256'],
                original_response_version=context['original_response']['source_version'])


def decimal(value):
    require(isinstance(value, str) and len(value) <= 32
            and re.fullmatch(r'\d+(?:\.\d+)?', value) is not None, 'SCORE_INVALID')
    return Decimal(value)


def number(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'DUPLICATE_FIELD')
        value[key] = item
    return value


def parse(content, context):
    require(isinstance(content, str) and 0 < len(content) <= 24000, 'OUTPUT_INVALID')
    try:
        body = json.loads(content, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValueError('PRINTED_OUTPUT_INVALID') from error
    require(isinstance(body, dict) and set(body) == {'points'}, 'OUTPUT_FIELDS')
    raw_points = body['points']
    criteria = context['criteria']
    require(isinstance(raw_points, list) and len(raw_points) == len(criteria) <= 8, 'POINT_COUNT')
    by_id = {}
    for point in raw_points:
        require(isinstance(point, dict) and set(point) == {'point_id', 'status', 'score', 'response_quote', 'reason'}, 'POINT_FIELDS')
        pid = point['point_id']
        require(isinstance(pid, str) and pid not in by_id, 'POINT_ID')
        require(isinstance(point['reason'], str) and 1 <= len(point['reason'].strip()) <= 300, 'POINT_REASON')
        quote = point['response_quote']
        require(isinstance(quote, str) and len(quote) <= 1000
                and (not quote or quote in context['response']), 'RESPONSE_QUOTE')
        by_id[pid] = point
    require(set(by_id) == {point['point_id'] for point in criteria}, 'POINT_ID')
    points, scores, maxima, abstained = [], [], [], False
    for criterion in criteria:
        printed = POINT.fullmatch(criterion['source_quote'])
        require(printed is not None and printed[1] == criterion['criterion'], 'SOURCE_POINTS_INVALID')
        limit = decimal(printed[2])
        require(0 < limit <= 20 and float(limit) == criterion['maximum_score'], 'SOURCE_POINTS_INVALID')
        point = by_id[criterion['point_id']]
        status = point['status']
        require(status in ('met', 'not_met', 'abstain'), 'POINT_STATUS')
        if status == 'abstain':
            require(point['score'] is None, 'ABSTENTION_SCORE')
            score, abstained = None, True
        else:
            score = decimal(point['score'])
            require(score == (limit if status == 'met' else Decimal(0)), 'UNPRINTED_PARTIAL_SCORE')
            require(status != 'met' or bool(point['response_quote'].strip()), 'RESPONSE_QUOTE')
            scores.append(score)
        maxima.append(limit)
        points.append({**point, 'score': number(score) if score is not None else None,
                       'maximum_score': number(limit)})
    with localcontext() as arithmetic:
        arithmetic.prec = 64
        total, maximum = sum(scores, Decimal(0)), sum(maxima, Decimal(0))
    require(maximum <= 100, 'SOURCE_POINTS_INVALID')
    return dict(points=points, total_score=None if abstained else number(total), maximum_score=number(maximum),
                status='candidate_abstained' if abstained else 'candidate_unqualified')


def model_call(c, child_id, context, call_id):
    row = c.execute('SELECT * FROM model_call WHERE call_id=? AND child_id=?', (call_id, child_id)).fetchone()
    require(row is not None and row['session_id'] == context['original_response']['session_id']
            and row['purpose'] == PURPOSE and row['status'] == 'completed' and row['safety_status'] == 'passed', 'MODEL_CALL_INVALID')
    require(all(isinstance(row[key], str) and 0 < len(row[key]) <= 200
                for key in ('provider', 'model', 'prompt_version')), 'MODEL_CALL_INVALID')
    try:
        manifest = json.loads(row['context_manifest_json'])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError('PRINTED_MODEL_CALL_INVALID') from error
    require(isinstance(manifest, dict) and manifest.get('paper_execution') == execution_binding(context), 'MODEL_BINDING_CHANGED')
    provenance = {key: row[key] for key in ('call_id', 'provider', 'model', 'prompt_version', 'verification_status', 'safety_status')}
    if 'model_output_sha256' in manifest:
        provenance['output_sha256'] = manifest['model_output_sha256']
    return provenance


class PaperPrintedScoreCandidateService:
    def __init__(self, database):
        self.database = database

    def accept(self, child_id, rubric_artifact_id, call_id, content, *, connection=None):
        if connection is None:
            with self.database.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                result = self.accept(child_id, rubric_artifact_id, call_id, content, connection=c)
                c.commit()
                return result
        c = connection
        require(c.in_transaction, 'TRANSACTION_REQUIRED')
        context = PaperSubjectiveRubricService(None).execution_context(child_id, rubric_artifact_id, connection=c)
        provenance = model_call(c, child_id, context, call_id)
        if 'output_sha256' in provenance:
            require(provenance['output_sha256'] == digest(content), 'OUTPUT_BINDING_CHANGED')
        parsed = parse(content, context)
        payload = dict(request_id=context['original_response']['request_id'], **execution_binding(context),
            **parsed, model_call=provenance, model_revision_verified=False,
            score_authority_granted=False, independent_calibration_required=True,
            local_only=True, cloud_transmission_allowed=False)
        # Call + exact frozen execution is the retry identity, not the model's
        # generated score or a mutable view of the paper.
        action = 'printed-score-candidate:' + digest([child_id, rubric_artifact_id, call_id])
        existing = c.execute('SELECT * FROM capture_artifact WHERE client_action_id LIKE ?', (action + ':%',)).fetchone()
        if existing:
            require(existing['child_id'] == child_id and json.loads(existing['details_json']) == payload, 'CANDIDATE_CONFLICT')
            return self.current(child_id, existing['artifact_id'], connection=c)
        artifact_id = str(uuid4())
        c.execute("""INSERT INTO capture_artifact VALUES(?,?,?,?,NULL,NULL,NULL,?,'completed',?,?)""",
            (artifact_id, child_id, context['original_response']['session_id'], TYPE,
             json.dumps(payload, ensure_ascii=False, allow_nan=False), utc_now(), action + ':' + digest(payload)))
        return self.current(child_id, artifact_id, connection=c)

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
        require(row is not None, 'CANDIDATE_UNKNOWN')
        payload = json.loads(row['details_json'])
        require(isinstance(payload, dict) and payload.get('version') == VERSION, 'CANDIDATE_CORRUPT')
        require(isinstance(payload.get('model_call'), dict) and isinstance(payload.get('frozen_rubric_artifact_id'), str)
                and isinstance(payload['model_call'].get('call_id'), str), 'CANDIDATE_CORRUPT')
        expected_action = 'printed-score-candidate:' + digest([child_id, payload['frozen_rubric_artifact_id'], payload['model_call']['call_id']]) + ':' + digest(payload)
        require(row['client_action_id'] == expected_action, 'CANDIDATE_CORRUPT')
        status, reason = payload['status'], None
        try:
            ctx = PaperSubjectiveRubricService(None).execution_context(child_id, payload['frozen_rubric_artifact_id'], connection=connection)
            require(payload['request_id'] == ctx['original_response']['request_id']
                    and row['session_id'] == ctx['original_response']['session_id']
                    and all(payload[key] == value for key, value in execution_binding(ctx).items()), 'CANDIDATE_BINDING_CHANGED')
            require(model_call(connection, child_id, ctx, payload['model_call']['call_id']) == payload['model_call'], 'MODEL_BINDING_CHANGED')
            normalized = parse(json.dumps({'points': [{key: point[key] for key in ('point_id', 'status', 'score', 'response_quote', 'reason')}
                                     for point in payload['points']]}), ctx)
            require(all(payload[key] == value for key, value in normalized.items())
                    and payload['score_authority_granted'] is False and payload['independent_calibration_required'] is True
                    and payload['cloud_transmission_allowed'] is False and payload['local_only'] is True
                    and payload['model_revision_verified'] is False, 'CANDIDATE_CORRUPT')
        except (ValueError, KeyError, TypeError) as error:
            status, reason = 'candidate_withdrawn', str(error) if isinstance(error, ValueError) else 'PRINTED_CANDIDATE_CORRUPT'
        return {**payload, 'artifact_id': artifact_id, 'status': status, 'reason_code': reason,
                'score_authority_granted': False, 'independent_calibration_required': True,
                'local_only': True, 'cloud_transmission_allowed': False}

    def latest_for_rubric(self, child_id, rubric_artifact_id, *, connection):
        row = connection.execute("""SELECT artifact_id FROM capture_artifact WHERE child_id=? AND artifact_type=?
            AND json_valid(details_json) AND json_extract(details_json,'$.frozen_rubric_artifact_id')=?
            ORDER BY rowid DESC LIMIT 1""", (child_id, TYPE, rubric_artifact_id)).fetchone()
        return self.current(child_id, row['artifact_id'], connection=connection) if row else None
