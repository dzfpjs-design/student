"""Local parent-reviewed target mappings, owned by the original commitment.

The mapping is not a score or an approval of generated questions. References
must retain their own qualified source and rubric. History shares the original
record's export/deletion boundary instead of creating an untracked second store.
"""
import json
from datetime import datetime, timezone

from . import document_goal_handoff as goal
from . import document_revisit as revisit
from .diagnostic_prompt import _version
from .document_independent_check import _qualified, original_version

KEY = 'independent_mapping'


def packet(c, child, commitment):
    goal.require(c.execute("SELECT 1 FROM child_profile WHERE child_id=? AND status='active'", (child,)).fetchone(), 'SOURCE_REVIEW_NOT_FOUND')
    found = c.execute('SELECT 1 FROM agent_commitment WHERE commitment_id=? AND child_id=?', (commitment, child)).fetchone()
    goal.require(found, 'SOURCE_REVIEW_NOT_FOUND')
    value = revisit.retained(c, commitment, child)
    item, payload, *_ = value
    request = payload.get('practice', {}).get('independent_request')
    goal.require(isinstance(request, dict), 'SOURCE_REVIEW_NOT_FOUND')
    turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=? AND child_id=?', (request.get('turn_id'), child)).fetchone()
    goal.require(turn and json.loads(turn['action_json']).get('document_independent_check') == request.get('binding')
        and request['binding']['original_version'] == original_version(value[3]), 'SOURCE_REVIEW_REQUEST_CHANGED')
    goal.require(item['status'] in {'pending','due'} and value[3]['phase'] == 'scheduled', 'SOURCE_REVIEW_REVOKED')
    if c.execute("SELECT 1 FROM deletion_job WHERE child_id=? AND scope IN ('learning','media','tutor','all_history') AND status!='completed'", (child,)).fetchone():
        raise goal.DocumentGoalError('SOURCE_REVIEW_DELETION_PENDING')
    return value


def reviewed_source(c, value):
    mapping = value[1].get(KEY)
    if mapping is None:
        return None, None
    current = mapping['current']
    goal.require(current['decision'] == 'approved' and current['original_version'] == original_version(value[3]), 'SOURCE_REVIEW_UNAPPROVED')
    reference = _qualified(c, value[0]['child_id'], current['item_id'])
    goal.require(reference and reference['subject'] == value[5]['subject']
        and _version(reference) == current['item_version'], 'SOURCE_REVIEW_REFERENCE_CHANGED')
    goal.require(current['original_quote'] in value[4]['confirmed_text']
        and current['reference_quote'] in target_text(reference), 'SOURCE_REVIEW_QUOTE_CHANGED')
    return reference, {'revision':mapping['revision'], 'action_id':current['action_id'], 'version':goal.digest(current)}


def target_text(reference):
    return reference['title']


def context_version(value):
    return goal.digest({'original':original_version(value[3]), 'mapping':value[1].get(KEY),
        'request':value[1]['practice']['independent_request']})


def detail(c, child, commitment, search=''):
    from .document_independent_check import plan
    value = packet(c, child, commitment)
    item, payload, root, state, confirmed, teacher = value
    refs = []
    for row in c.execute('''SELECT d.diagnostic_item_id FROM diagnostic_item d
        JOIN knowledge_node n USING(knowledge_node_id) WHERE d.status='active' AND n.subject=?
        AND (?='' OR instr(n.title,?)>0 OR instr(d.prompt,?)>0) ORDER BY n.title,d.diagnostic_item_id''',
        (teacher['subject'], search, search, search)):
        reference = _qualified(c, child, row[0])
        if not reference:
            continue
        refs.append(dict(item_id=row[0], item_version=_version(reference), node_id=reference['knowledge_node_id'],
            target=target_text(reference), question=reference['prompt']))
        if len(refs) == 201:
            break
    try:
        status = plan(c, value, payload['practice']['session_id'])['status']
    except (ValueError, KeyError, TypeError):
        status = 'mapping_required'
    return dict(child_id=child, commitment_id=commitment, version=context_version(value), status=status,
        original_question=confirmed['confirmed_text'], subject=teacher['subject'],
        mapping=payload.get(KEY, {}).get('current'), references=refs[:200], truncated=len(refs)>200, **goal.LOCAL)


def queue(c, child):
    for row in c.execute('''SELECT * FROM agent_commitment WHERE child_id=? AND status IN ('pending','due')
        AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.origin') END=?
        AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.practice.independent_request') END IS NOT NULL''',
        (child, revisit.ORIGIN)):
        raw = json.loads(row['payload_json'])
        if raw.get('practice', {}).get('phase') == 'checking_new_question':
            continue
        try:
            value = packet(c, child, row['commitment_id'])
            question = value[4]['confirmed_text']
            from .document_independent_check import plan
            try:
                status = plan(c, value, raw['practice']['session_id'])['status']
            except (ValueError, KeyError, TypeError):
                status = 'mapping_required'
        except (ValueError, KeyError, TypeError):
            question, status = '', 'source_changed'
        yield dict(commitment_id=row['commitment_id'], version=goal.digest(dict(row)), created_at=row['created_at'],
            question=question, status=status)


class DocumentSourceReview:
    def __init__(self, database):
        self.database = database

    def read(self, child, commitment, search=''):
        with self.database.connect() as c:
            c.execute('BEGIN')
            return detail(c, child, commitment, search)

    def receipt(self, child, commitment, action):
        with self.database.connect() as c:
            c.execute('BEGIN')
            value = packet(c, child, commitment)
            event = next((e for e in value[1].get(KEY, {}).get('events', []) if e['request']['action_id']==action), None)
            return dict(receipt=event['receipt'] if event else None, current=detail(c,child,commitment), **goal.LOCAL)

    def review(self, child, commitment, request):
        with self.database.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            value = packet(c, child, commitment)
            payload = value[1]
            mapping = payload.get(KEY, {'revision':0, 'events':[]})
            old = next((e for e in mapping['events'] if e['request']['action_id']==request['action_id']), None)
            if old:
                goal.require(old['request'] == request, 'SOURCE_REVIEW_ACTION_CONFLICT')
                return dict(receipt=old['receipt'], current=detail(c,child,commitment), replayed=True, **goal.LOCAL)
            goal.require(request['version'] == context_version(value), 'SOURCE_REVIEW_VERSION_CHANGED')
            goal.require(8 <= len(request['note'].strip()) <= 1500, 'SOURCE_REVIEW_REASON_REQUIRED')
            if request['decision'] == 'approve':
                reference = _qualified(c,child,request['item_id'])
                goal.require(reference and reference['subject'] == value[5]['subject']
                    and _version(reference) == request['item_version'], 'SOURCE_REVIEW_REFERENCE_CHANGED')
                goal.require(request['original_quote'].strip() and request['original_quote'] in value[4]['confirmed_text']
                    and request['reference_quote'].strip() and request['reference_quote'] in target_text(reference), 'SOURCE_REVIEW_QUOTE_CHANGED')
                decision = 'approved'
            else:
                goal.require(request['decision'] == 'revoke' and mapping.get('current'), 'SOURCE_REVIEW_DECISION_INVALID')
                decision = 'revoked'
            current = {**{k:request[k] for k in ('item_id','item_version','original_quote','reference_quote','note','action_id')},
                'decision':decision, 'original_version':original_version(value[3]), 'source':'parent_ui',
                'created_at':datetime.now(timezone.utc).isoformat()}
            receipt = dict(child_id=child, commitment_id=commitment, revision=mapping['revision']+1,
                action_id=request['action_id'], decision=decision, record_version=goal.digest(current), **goal.LOCAL)
            payload[KEY] = dict(revision=receipt['revision'], current=current,
                events=[*mapping['events'], {'request':request, 'receipt':receipt}])
            c.execute('UPDATE agent_commitment SET payload_json=? WHERE commitment_id=? AND child_id=?',
                (json.dumps(payload,ensure_ascii=False),commitment,child))
            response = dict(receipt=receipt, current=detail(c,child,commitment), replayed=False, **goal.LOCAL)
            c.commit()
            return response
