"""Local origin proof for an explicitly requested unseen diagnostic.

The role model sees words and booleans. Question IDs and track come only from
the owned, already answered original; preparation uses the normal source gate.
"""
import json

from .learning_question_help import snapshot, validate


def target(c, session_id, child_id, frozen, *, turn_id=None):
    if not isinstance(frozen, dict):
        raise ValueError('INDEPENDENT_QUESTION_ORIGIN_REQUIRED')
    parent = c.execute('SELECT * FROM diagnostic_prompt WHERE prompt_id=? AND origin_session_id=? AND child_id=?',
        (frozen.get('prompt_id'),session_id,child_id)).fetchone()
    if not parent or parent['status']!='answered' or parent['item_version']!=frozen.get('item_version'):
        raise ValueError('INDEPENDENT_QUESTION_ORIGIN_REQUIRED')
    item=json.loads(parent['item_json'])
    track=item.get('track')
    if track not in {'school','pet','competition_math'}:
        raise ValueError('INDEPENDENT_QUESTION_TRACK_INVALID')
    subject,node_id=item.get('subject'),item.get('knowledge_node_id')
    if not isinstance(subject,str) or not subject or not isinstance(node_id,str) or not node_id:
        raise ValueError('INDEPENDENT_QUESTION_TRACK_INVALID')
    prepared = c.execute('SELECT * FROM diagnostic_prompt WHERE prepare_action_id=?',
        ('voice-diagnostic:'+turn_id,)).fetchone() if turn_id else None
    if prepared:
        request=json.loads(prepared['request_json'])
        turn=c.execute('SELECT created_at FROM agent_turn WHERE agent_turn_id=? AND session_id=? AND child_id=?',
            (turn_id,session_id,child_id)).fetchone()
        current=c.execute('SELECT prompt_id FROM diagnostic_prompt WHERE origin_session_id=? AND child_id=? ORDER BY rowid DESC LIMIT 1',
            (session_id,child_id)).fetchone()
        if (not turn or prepared['origin_session_id']!=session_id or prepared['child_id']!=child_id
                or prepared['prompt_id']==parent['prompt_id'] or prepared['created_at']<turn['created_at']
                or request.get('require_unexposed') is not True or request.get('track')!=track
                or request.get('subject')!=subject or request.get('knowledge_node_id')!=node_id
                or not current or current[0]!=prepared['prompt_id']):
            raise ValueError('INDEPENDENT_QUESTION_PREPARATION_CHANGED')
        # A canonical preparation has replaced the current prompt. Its origin
        # is now a historical proof, never permission to prepare another question.
        current_origin=snapshot(c,session_id,child_id,_historical_prompt=parent['prompt_id'])
        if (not current_origin or {k:v for k,v in current_origin.items() if k!='task_state'}!=
                {k:v for k,v in frozen.items() if k!='task_state'}
                or [v[0] for v in current_origin['task_state']]!=[v[0] for v in frozen['task_state']]):
            raise ValueError('INDEPENDENT_QUESTION_ORIGIN_CHANGED')
    else:
        validate(c,session_id,child_id,frozen)
        if c.execute("SELECT 1 FROM diagnostic_prompt WHERE origin_session_id=? AND child_id=? AND status IN ('waiting_answer','answer_saved')",
                (session_id,child_id)).fetchone():
            raise ValueError('INDEPENDENT_QUESTION_ORIGINAL_PENDING')
    return {'track':track,'subject':subject,'knowledge_node_id':node_id,'require_unexposed':True}
