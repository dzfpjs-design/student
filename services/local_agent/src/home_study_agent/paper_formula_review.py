"""Versioned full-question corrections; neither a cloud grant nor a score."""
import json

from .local_supply import digest
from .paper_question_projection import project_question_lines

KIND = 'formula_question'


def _source(artifact):
    source = dict(artifact)
    details = json.loads(source['details_json'])
    if not isinstance(details, dict):
        raise ValueError('FORMULA_QUESTION_SOURCE_INVALID')
    # Continuous capture attaches its separately verified local visual metadata
    # later. All original OCR fields remain bound to this human correction.
    details.pop('visual_capture', None)
    source['details_json'] = json.dumps(details, ensure_ascii=False, sort_keys=True)
    return source


def _projection(artifact, region, question):
    source = _source(artifact)
    candidate = {'question_text': question, 'region_json': json.dumps(dict(zip(('x', 'y', 'width', 'height'), region)))}
    return project_question_lines(source, candidate, question, formula_review=True)


def prepare_questions(connection, artifact_id, child_id, session_id):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='capture_artifact'").fetchone():
        return []
    artifact = connection.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=? AND session_id=?',
                                  (artifact_id, child_id, session_id)).fetchone()
    if not artifact or artifact['artifact_type'] != 'document_ocr' or artifact['status'] != 'quality_warning':
        return []
    try:
        details = json.loads(artifact['details_json'])
        pipeline = details['local_pipeline']
        formulas = pipeline['formula_routing']['regions']
        blocks = pipeline['question_blocks']
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(blocks, list) or not isinstance(formulas, list):
        return []
    result = []
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get('bbox_xywh'), list) or len(block['bbox_xywh']) != 4:
            continue
        region, question = block['bbox_xywh'], block.get('text')
        try:
            projection = _projection(artifact, region, question)
            x, y, w, h = region
            matches = []
            for index, formula in enumerate(formulas):
                if not isinstance(formula, dict):
                    raise ValueError('FORMULA_REGION_INVALID')
                if formula.get('status') != 'manual_edit_required' or formula.get('review_eligible') is False:
                    continue
                fx, fy, fw, fh = formula['bbox_xywh']
                if x <= fx + fw / 2 <= x + w and y <= fy + fh / 2 <= y + h:
                    matches.append(index)
            if matches:
                result.append({'text': question, 'box': region, 'confidence': artifact['confidence'],
                    'source_binding': {'projection': projection, 'formula_indices': matches}})
        except (ValueError, TypeError, KeyError):
            continue
    # A region touching ambiguous question blocks must not certify either one.
    indices = [i for value in result for i in value['source_binding']['formula_indices']]
    return [value for value in result if all(indices.count(i) == 1 for i in value['source_binding']['formula_indices'])]


def resolve(connection, artifact, candidate):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='ocr_review_item'").fetchone():
        return None
    rows = connection.execute('''SELECT * FROM ocr_review_item WHERE artifact_id=? AND child_id=? AND session_id=?
        AND review_kind=? AND proposed_text=?''',
        (artifact['artifact_id'], artifact['child_id'], artifact['session_id'], KIND, candidate['question_text'])).fetchall()
    if not rows or not any(row['status'] == 'confirmed' for row in rows):
        return None
    if len(rows) != 1:
        raise ValueError('FORMULA_QUESTION_REVIEW_AMBIGUOUS')
    row = rows[0]
    region = json.loads(row['region_json'])
    if not isinstance(region, list) or len(region) != 4:
        raise ValueError('FORMULA_QUESTION_REVIEW_REGION_INVALID')
    if dict(zip(('x', 'y', 'width', 'height'), region)) != json.loads(candidate['region_json']):
        raise ValueError('FORMULA_QUESTION_REVIEW_REGION_CHANGED')
    prepared = connection.execute("SELECT payload_json FROM ocr_review_event WHERE artifact_id=? AND event_type='prepared'",
                                  (artifact['artifact_id'],)).fetchall()
    bindings = []
    for event in prepared:
        payload = json.loads(event[0])
        if not isinstance(payload, dict) or not isinstance(payload.get('full_formula_questions', {}), dict):
            raise ValueError('FORMULA_QUESTION_REVIEW_BINDING_INVALID')
        bindings.append(payload.get('full_formula_questions', {}).get(row['review_id']))
    bindings = [value for value in bindings if value is not None]
    current = next((value['source_binding'] for value in prepare_questions(connection, artifact['artifact_id'],
        artifact['child_id'], artifact['session_id']) if value['text'] == row['proposed_text'] and value['box'] == region), None)
    if len(bindings) != 1 or current is None or current != bindings[0]:
        raise ValueError('FORMULA_QUESTION_REVIEW_SOURCE_CHANGED')
    event = connection.execute('SELECT * FROM ocr_review_event WHERE review_id=? ORDER BY rowid DESC LIMIT 1',
                               (row['review_id'],)).fetchone()
    if (not event or event['event_type'] != 'corrected' or not row['corrected_text']
            or json.loads(event['payload_json']).get('corrected_text') != row['corrected_text']):
        raise ValueError('FORMULA_QUESTION_REVIEW_CONFIRMATION_REQUIRED')
    return {'question': row['corrected_text'], 'review_id': row['review_id'], 'event_id': event['event_id'],
        'version': digest({'review': dict(row), 'event': dict(event), 'source': current}),
        'source_projection': current['projection']}


def for_mistake(connection, mistake_id, child_id):
    from .mistake_source_revision import current
    revision = current(connection, mistake_id, child_id)
    if revision:
        return revision
    case = connection.execute("SELECT source_ref,prompt_summary FROM mistake_case WHERE mistake_id=? AND child_id=? AND source_type='document_camera'",
                              (mistake_id, child_id)).fetchone()
    if not case or not connection.execute("SELECT 1 FROM sqlite_master WHERE name='ocr_review_item'").fetchone():
        return None
    artifact_id, separator, candidate_id = (case['source_ref'] or '').partition('#')
    if not separator:
        return None
    rows = connection.execute('SELECT * FROM ocr_review_item WHERE artifact_id=? AND child_id=? AND review_kind=? AND proposed_text=?',
                             (artifact_id, child_id, KIND, case['prompt_summary'])).fetchall()
    artifact = connection.execute('SELECT * FROM capture_artifact WHERE artifact_id=? AND child_id=?', (artifact_id, child_id)).fetchone()
    if not rows:
        try:
            if artifact and 'formula_review_required' in json.loads(artifact['details_json'])['quality']['issues']:
                return {'status': 'pending', 'original_question': case['prompt_summary'],
                        'version': digest({'unreviewed_formula': artifact['details_json']})}
        except (ValueError, TypeError, KeyError):
            pass
        return None
    candidate = connection.execute('SELECT * FROM mistake_capture_candidate WHERE candidate_id=? AND child_id=? AND mistake_id=?',
                                  (candidate_id, child_id, mistake_id)).fetchone()
    status = 'pending'
    try:
        if not artifact or not candidate:
            raise ValueError('source missing')
        current = resolve(connection, artifact, candidate)
        if current:
            return {'status': 'confirmed', **current, 'original_question': case['prompt_summary']}
    except (ValueError, TypeError, KeyError):
        status = 'invalidated'
    return {'status': status, 'original_question': case['prompt_summary'],
            'version': digest({'reviews': [dict(row) for row in rows], 'status': status})}


def tutor_binding(connection, artifact, text, session_id=None):
    """Bind a separate tutoring confirmation to the current full-question edit."""
    from .mistake_source_revision import artifact_binding
    revised = artifact_binding(connection, artifact['artifact_id'], artifact['child_id'], text,
                               session_id or artifact['session_id'])
    if revised:
        return revised
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='ocr_review_item'").fetchone():
        return None
    normalize = lambda value: ' '.join((value or '').split())
    rows = connection.execute('''SELECT * FROM ocr_review_item WHERE artifact_id=? AND child_id=?
        AND session_id=? AND review_kind=?''',
        (artifact['artifact_id'], artifact['child_id'], artifact['session_id'], KIND)).fetchall()
    matches = [row for row in rows if normalize(text) in
               {normalize(row['proposed_text']), normalize(row['corrected_text'])}]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('FORMULA_QUESTION_REVIEW_AMBIGUOUS')
    row = matches[0]
    candidate = {'question_text': row['proposed_text'], 'region_json': json.dumps(
        dict(zip(('x', 'y', 'width', 'height'), json.loads(row['region_json']))))}
    current = resolve(connection, artifact, candidate)
    if not current or normalize(current['question']) != normalize(text):
        raise ValueError('FORMULA_QUESTION_REVIEW_REQUIRED')
    return {'review_id': current['review_id'], 'version': current['version']}
