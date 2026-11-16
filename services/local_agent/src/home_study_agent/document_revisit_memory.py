"""Source-checked practice history, never calibrated mastery or cloud context."""
import json
import sqlite3

from . import document_goal_handoff as goal, document_revisit as revisit
from .agent_runtime import AgentRuntimeStore
from .document_revisit_feedback import read_saved

KIND = 'document_revisit'
EMPTY_FINGERPRINT = goal.digest([])


class _NamedRows:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        cursor = self.connection.cursor()
        if cursor.row_factory is not sqlite3.Row:
            cursor.row_factory = sqlite3.Row
        return cursor.execute(sql, parameters)


def report_memory(connection, child_id, session_id=None):
    """Borrow the caller's snapshot; no schema initialization or writes."""
    owned = not connection.in_transaction
    if owned:
        connection.execute('BEGIN')
    try:
        return _report_memory(connection, child_id, session_id)
    finally:
        if owned:
            connection.rollback()


def _report_memory(connection, child_id, session_id):
    c = _NamedRows(connection) if hasattr(connection, 'cursor') else connection
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'agent_commitment', 'agent_turn', 'voice_control_intent_request', 'tutor_turn'} <= tables:
        return {'items': [], 'fingerprint': EMPTY_FINGERPRINT}
    if 'deletion_job' in tables and c.execute("""SELECT 1 FROM deletion_job WHERE child_id=?
        AND scope IN ('learning','media','tutor','all_history') AND status!='completed' LIMIT 1""", (child_id,)).fetchone():
        return {'items': [], 'fingerprint': EMPTY_FINGERPRINT}
    items, seen = [], set()
    for row in c.execute("""SELECT commitment_id FROM agent_commitment WHERE child_id=?
        AND CASE WHEN json_valid(payload_json) THEN json_extract(payload_json,'$.origin') END=?
        ORDER BY commitment_id""", (child_id, revisit.ORIGIN)).fetchall():
        try:
            item, payload, _, _, confirmed, teacher = revisit.retained(c, row[0], child_id)
            responses = payload.get('practice', {}).get('responses', [])
            goal.require(isinstance(responses, list), 'REVISIT_HISTORY_INVALID')
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            continue
        for response in responses:
            try:
                goal.require(isinstance(response, dict), 'REVISIT_RESPONSE_INVALID')
                if response.get('turn_id') in seen:
                    continue
                if session_id is not None and response.get('session_id') != session_id:
                    continue
                turn = c.execute('SELECT * FROM agent_turn WHERE agent_turn_id=?', (response['turn_id'],)).fetchone()
                owner = c.execute('SELECT child_id FROM daily_session WHERE session_id=?', (response['session_id'],)).fetchone()
                goal.require(turn and owner and owner[0] == child_id
                    and (turn['child_id'], turn['session_id'], turn['transcript']) ==
                    (child_id, response['session_id'], response['text']), 'REVISIT_RESPONSE_CHANGED')
                action = json.loads(turn['action_json'])
                journal = c.execute('SELECT * FROM voice_control_intent_request WHERE request_key=?',
                    (action.get('document_revisit_request'),)).fetchone()
                receipt = json.loads(journal['execution_receipt_json']) if journal and journal['execution_receipt_json'] else {}
                goal.require(journal and journal['status'] == 'completed'
                    and (journal['child_id'], journal['session_id']) == (child_id, response['session_id'])
                    and receipt.get('agent_turn_id') == response['turn_id']
                    and receipt.get('result', {}).get('response_hash') == goal.digest(response)
                    and action.get('document_revisit_commitment') == item['commitment_id']
                    and response['kind'] in {'answer', 'help'} and isinstance(response['after_feedback'], bool),
                    'REVISIT_RESPONSE_CHANGED')
                voice = AgentRuntimeStore.resolve_captured_voice(c, session_id=response['session_id'], child_id=child_id,
                    voice_input_id=response['voice_input_id'], transcript=response['text'])
                goal.require(action.get('voice_input_binding') == AgentRuntimeStore.voice_capture_snapshot(voice),
                    'REVISIT_RESPONSE_CHANGED')
                entry = {'ref': response['turn_id'], 'commitment_id': item['commitment_id'],
                    'session_id': response['session_id'], 'occurred_at': response['created_at'],
                    'subject': teacher['subject'], 'question': confirmed['confirmed_text'],
                    'response': response['text'], 'response_kind': response['kind'],
                    'after_feedback': response['after_feedback'], 'feedback': None,
                    'mastery_update_allowed': False, 'cloud_transmission_allowed': False}
                feedbacks = payload.get('practice', {}).get('feedbacks', [])
                feedbacks = feedbacks if isinstance(feedbacks, list) else []
                record = next((f for f in feedbacks if isinstance(f, dict)
                    and f.get('response_turn_id') == response['turn_id']), None)
                if record:
                    try:
                        tutor = c.execute('SELECT * FROM tutor_turn WHERE turn_id=?', (record['tutor_turn_id'],)).fetchone()
                        goal.require(tutor and tutor['child_id'] == child_id and tutor['status'] == 'completed'
                            and tutor['safety_status'] == 'passed', 'REVISIT_FEEDBACK_CHANGED')
                        read_saved(c, tutor)
                        proof = json.loads(tutor['output_envelope_json'])['document_revisit']
                        goal.require(proof['response_kind'] == response['kind']
                            and proof['after_feedback'] == response['after_feedback']
                            and isinstance(proof['assessment']['hypothesis'], str)
                            and 1 <= len(proof['assessment']['hypothesis']) <= 500, 'REVISIT_FEEDBACK_CHANGED')
                        entry['feedback'] = {'tutor_turn_id': tutor['turn_id'], 'hypothesis': proof['assessment']['hypothesis'],
                            'source_hash': goal.digest(dict(tutor)), 'verification_status': 'needs_verification'}
                    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                        pass
                items.append(entry)
                seen.add(response['turn_id'])
            except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                continue
    items.sort(key=lambda x: (x['occurred_at'], x['ref']))
    return {'items': items, 'fingerprint': goal.digest(items)}


def describe(item):
    mode = '求助时说' if item['response_kind'] == 'help' else '提示后回答' if item['after_feedback'] else '当时回答'
    text = f"原题回顾（{item['subject']}），题目摘录：{item['question'][:140]}；{mode}（摘录）：{item['response'][:240]}。"
    if item['feedback']:
        text += '待验证的教学建议（摘录）：' + item['feedback']['hypothesis'][:200] + '。'
    else:
        text += '尚无有效点评记录。'
    return text + '这次记录不证明已经掌握或确定错因。'


def memory_current(c, row, items):
    try:
        proof = json.loads(row['observation_json'])
        current = next((item for item in items if item['ref'] == proof['ref']), None)
        sources = c.execute('''SELECT s.*,e.evidence FROM companion_memory_source s
            JOIN companion_memory_evidence e USING(source_id) WHERE e.memory_id=? AND e.evidence IS NOT NULL''',
            (row['memory_id'],)).fetchall()
        return bool(current and goal.digest(current) == proof['fingerprint'] and row['content'] == describe(current)
            and len(sources) == 1 and sources[0]['child_id'] == row['child_id']
            and sources[0]['session_id'] == current['session_id'] and sources[0]['turn_id'] == current['ref']
            and not sources[0]['tombstoned'] and sources[0]['evidence'] == current['response'])
    except (ValueError, TypeError, KeyError):
        return False


def observe(store, c, turn, now):
    """Use existing memory retention, suppression, erasure and source history."""
    from .companion_memory import _Claim, _timestamp
    for item in report_memory(c, turn['child_id'], turn['session_id'])['items']:
        occurred = _timestamp(item['occurred_at'])
        if not occurred or occurred > now:
            continue
        action_id = 'document-revisit-memory:' + item['ref']
        existing = c.execute('SELECT * FROM companion_memory_source WHERE action_id=?', (action_id,)).fetchone()
        goal.require(not existing or (existing['child_id'], existing['session_id'], existing['turn_id']) ==
            (turn['child_id'], item['session_id'], item['ref']), 'REVISIT_MEMORY_OWNER_CHANGED')
        if existing and existing['tombstoned']:
            continue
        fingerprint = goal.digest(item)
        source_id = existing['source_id'] if existing else store._source(c, turn['child_id'], item['session_id'],
            fingerprint, action_id, 'turn', occurred, item['ref'])
        claim = _Claim(describe(item), 'event', action_id, fingerprint, item['response'])
        store._save(c, turn['child_id'], [claim], source_id, occurred, now, False)
        c.execute('''UPDATE companion_memory SET observation_kind=?,observation_json=?,confidence=.3
            WHERE child_id=? AND semantic_key=? AND value_digest=?''', (KIND,
            json.dumps({'ref': item['ref'], 'fingerprint': fingerprint}), turn['child_id'],
            store._digest(c, turn['child_id'], action_id), store._digest(c, turn['child_id'], fingerprint)))
